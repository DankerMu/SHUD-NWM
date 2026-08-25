from __future__ import annotations

import contextlib
import errno
import hashlib
import itertools
import json
import logging
import os
import stat
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

import pytest

from packages.common import safe_fs
from services.orchestrator import chain_repository_state as chain_repository_state_module
from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator import scheduler_candidates as scheduler_candidates_module
from services.orchestrator import scheduler_discovery as scheduler_discovery_module
from services.orchestrator import scheduler_generation as scheduler_generation_module
from services.orchestrator import scheduler_state_decision as scheduler_state_decision_module
from services.orchestrator import scheduler_state_failure as scheduler_state_failure_module
from services.orchestrator import scheduler_state_manual_retry as scheduler_state_manual_retry_module
from services.orchestrator.chain import SlurmClientError
from services.orchestrator.chain_repository_state import (
    DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
    DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
)
from services.orchestrator.chain_types import OrchestratorError
from services.orchestrator.file_orchestration_journal import (
    FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
    FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
    FileJournalRetryService,
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.retry import RetryConfig, RetryError, RetryNotFoundError
from services.orchestrator.scheduler import ProductionScheduler, ProductionSchedulerConfig
from services.orchestrator.scheduler_state_types import CandidateStateDecision
from tests.test_production_scheduler import (
    FakeAdapter,
    FakeRegistry,
    RawCandidateStateRepository,
    _config,
    _dt,
    _model,
    _scheduler_candidate_fixture,
    _set_db_free_scheduler_env,
    _write_db_free_file_provider_fixtures,
    _write_db_free_raw_manifest_fixture,
)
from tests.test_retry import (
    _RETRY_MODULE_LOGGER,
    _UNKNOWN_ERROR_CODE,
    _auto_retry_skipped_warnings,
    _spec_non_transient_error_codes,
    _unknown_error_code_warning,
)
from workers.data_adapters.base import CycleDiscovery, cycle_id_for, format_cycle_time


@pytest.mark.parametrize("fault", ["directory_fsync", "parent_identity"])
def test_file_reservation_durability_uncertainty_blocks_gateway_before_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from services.orchestrator.chain import M3_STAGES, CycleOrchestrationContext
    from tests.test_orchestration_chain import FakeCycleSlurmClient, _basins, _orchestrator

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository._ensure_root_unlocked()
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    cycle_time = _dt("2026-06-28T00:00:00Z")
    basins = orchestrator._normalize_cycle_basins(_basins(2), "gfs", cycle_time)
    context = CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=cycle_time,
        cycle_id="gfs_2026062800",
        run_id="cycle_gfs_2026062800",
        all_basins=basins,
        active_basins=list(basins),
    )

    if fault == "directory_fsync":
        real_fsync = safe_fs.os.fsync

        def fail_directory_fsync(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "injected directory fsync failure")
            real_fsync(fd)

        monkeypatch.setattr(safe_fs.os, "fsync", fail_directory_fsync)
    else:
        real_verify = safe_fs._verify_fd_matches_path
        real_fsync = safe_fs.os.fsync
        directory_synced = False

        def record_directory_fsync(fd: int) -> None:
            nonlocal directory_synced
            real_fsync(fd)
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                directory_synced = True

        def fail_post_replace_parent_identity(fd: int, path: Path) -> None:
            if directory_synced:
                raise safe_fs.SafeFilesystemError("injected parent identity change")
            real_verify(fd, path)

        monkeypatch.setattr(safe_fs.os, "fsync", record_directory_fsync)
        monkeypatch.setattr(safe_fs, "_verify_fd_matches_path", fail_post_replace_parent_identity)

    with pytest.raises(OrchestratorError) as caught:
        orchestrator._submit_and_wait_cycle_stage(M3_STAGES[2], context)
    assert caught.value.error_code == "FILE_JOURNAL_WRITE_FAILED"
    assert client.submissions == []

    monkeypatch.undo()
    reopened = FileOrchestrationJournalRepository(repository.root)
    rows = reopened.query_pipeline_jobs_by_cycle("gfs_2026062800")
    assert all(row.get("slurm_job_id") in (None, "") for row in rows)
    assert all(row.get("status") not in {"submission_failed", "failed"} for row in rows)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in rows),
        encoding="utf-8",
    )


def _open_fd_count_or_skip() -> int:
    for fd_dir in (Path("/proc/self/fd"), Path("/dev/fd")):
        try:
            if fd_dir.exists():
                return len(os.listdir(fd_dir))
        except OSError:
            continue
    pytest.skip("open fd directory is not available on this platform")


def _candidate_state(
    repository: FileOrchestrationJournalRepository,
    *,
    cycle_time: datetime,
    source_id: str = "gfs",
    model_id: str = "model_a",
    job_limit: int = DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
    event_limit: int = DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
) -> dict[str, Any] | None:
    return repository.candidate_state(
        source_id=source_id,
        cycle_time=cycle_time,
        model_id=model_id,
        run_id=f"fcst_{source_id}_{format_cycle_time(cycle_time)}_{model_id}",
        forcing_version_id=f"forc_{source_id}_{format_cycle_time(cycle_time)}_{model_id}",
        candidate_id=f"{source_id}:{cycle_time.isoformat()}:{model_id}:forecast_{source_id}_deterministic",
        job_limit=job_limit,
        event_limit=event_limit,
    )


def _latest_view(
    *,
    source_id: str = "gfs",
    cycle_time: datetime,
    model_id: str = "model_a",
    hydro_status: str | None = None,
    jobs: list[Mapping[str, Any]] | None = None,
    events: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    cycle_id = cycle_id_for(source_id, cycle_time)
    return {
        "schema_version": FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
        "generated_at": "2026-06-28T00:00:00Z",
        "source_id": source_id,
        "cycle_time": cycle_time.isoformat(),
        "model_id": model_id,
        "model_context": _model_context(model_id),
        "forcing_version": {
            "forcing_version_id": f"forc_{source_id}_{format_cycle_time(cycle_time)}_{model_id}",
            "forcing_package_uri": "s3://nhms/forcing/package.tar",
            "source_id": source_id,
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "max_lead_hours": 3,
            "forcing_package_manifest_uri": "s3://nhms/forcing/manifest.json",
            "forcing_package_manifest_checksum": "sha256:forcing",
        },
        "forecast_cycle": {
            "cycle_id": cycle_id,
            "source_id": source_id,
            "cycle_time": cycle_time.isoformat(),
            "status": "raw_complete",
            "manifest_uri": "s3://nhms/raw/gfs/manifest.json",
        },
        "hydro_run": (
            {
                "run_id": f"fcst_{source_id}_{format_cycle_time(cycle_time)}_{model_id}",
                "source_id": source_id,
                "cycle_time": cycle_time.isoformat(),
                "model_id": model_id,
                "status": hydro_status,
                "output_uri": "s3://nhms/runs/out",
                "updated_at": "2026-06-28T00:02:00Z",
            }
            if hydro_status is not None
            else None
        ),
        "pipeline_jobs": [dict(job) for job in (jobs or [])],
        "pipeline_events": [dict(event) for event in (events or [])],
        "replay": {"latest_sequence": len(jobs or []) + len(events or []), "record_count": len(jobs or [])},
    }


def _model_context(model_id: str = "model_a") -> dict[str, Any]:
    return {
        "model_id": model_id,
        "basin_id": "basin_a",
        "basin_version_id": "basin_version_a",
        "river_network_version_id": "river_network_a",
        "segment_count": 7,
        "output_segment_count": 5,
        "model_package_uri": "s3://nhms/models/model_a.tar",
        "model_package_checksum": "sha256:model",
    }


def _active_job(cycle_time: datetime, *, model_id: str = "model_a") -> dict[str, Any]:
    return {
        "job_id": f"job_cycle_gfs_{format_cycle_time(cycle_time)}_forecast",
        "idempotency_key": f"cycle_gfs_{format_cycle_time(cycle_time)}:forecast",
        "run_id": f"cycle_gfs_{format_cycle_time(cycle_time)}",
        "cycle_id": cycle_id_for("gfs", cycle_time),
        "job_type": "run_shud_forecast_array",
        "slurm_job_id": "3001",
        "model_id": model_id,
        "status": "queued",
        "stage": "forecast",
        "submitted_at": "2026-06-28T00:01:00Z",
        "created_at": "2026-06-28T00:00:00Z",
        "runtime_roots": {"workspace_root": "/secret/workspace", "object_store_root": "/secret/object-store"},
    }


def _enable_db_free_nfs_raw_manifest(
    monkeypatch: pytest.MonkeyPatch,
    roots: Mapping[str, Path],
    *,
    cycle_time: datetime,
) -> None:
    _write_db_free_raw_manifest_fixture(roots, cycle_time=cycle_time)
    monkeypatch.setenv("NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT", str(roots["object_store_root"]))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")


def _source_job(
    cycle_time: datetime,
    *,
    source_id: str,
    job_id: str,
    stage: str = "forecast",
    model_id: str = "model_a",
) -> dict[str, Any]:
    job = _active_job(cycle_time, model_id=model_id)
    cycle_stamp = format_cycle_time(cycle_time)
    job.update(
        {
            "job_id": job_id,
            "run_id": f"fcst_{source_id}_{cycle_stamp}_{model_id}",
            "cycle_id": cycle_id_for(source_id, cycle_time),
            "source_id": source_id,
            "stage": stage,
            "idempotency_key": f"{source_id}:{cycle_id_for(source_id, cycle_time)}:{model_id}:{stage}:{job_id}",
        }
    )
    return job


class _FailingSlurmGatewayClient:
    def __init__(
        self,
        *,
        status_error_code: str | None = None,
        cancel_error_code: str | None = None,
    ) -> None:
        self.status_error_code = status_error_code
        self.cancel_error_code = cancel_error_code

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        if self.status_error_code is not None:
            raise SlurmClientError(self.status_error_code, "Slurm status sync failed.", {"job_id": job_id})
        return {"job_id": job_id, "state": "PENDING", "status": "queued"}

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        if self.cancel_error_code is not None:
            raise SlurmClientError(self.cancel_error_code, "Slurm cancellation failed.", {"job_id": job_id})
        return {"job_id": job_id, "status": "cancelled", "replacement_submitted": False}


def _journal_record(
    *,
    record_type: str,
    source_id: str,
    cycle_time: datetime,
    payload: Mapping[str, Any],
    sequence: int = 1,
    model_id: str | None = "model_a",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
        "sequence": sequence,
        "record_type": record_type,
        "source_id": source_id,
        "cycle_time": cycle_time.isoformat(),
        "payload": dict(payload),
    }
    if model_id is not None:
        record["model_id"] = model_id
    return record


def _direct_model_context_record(model_id: str = "model_a") -> dict[str, Any]:
    return {
        "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
        "record_type": "model_context",
        "model_id": model_id,
        "payload": _model_context(model_id),
    }


def _direct_forcing_context_record(
    *,
    source_id: str = "gfs",
    cycle_time: datetime,
    model_id: str = "model_a",
    payload_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "forcing_version_id": f"forc_{source_id}_{format_cycle_time(cycle_time)}_{model_id}",
        "forcing_package_uri": "s3://nhms/forcing/direct.tar",
        "source_id": source_id,
        "cycle_time": cycle_time.isoformat(),
        "model_id": model_id,
        "max_lead_hours": 9,
    }
    payload.update(payload_overrides or {})
    return _journal_record(
        record_type="forcing_version",
        source_id=source_id,
        cycle_time=cycle_time,
        model_id=model_id,
        payload=payload,
    )


def test_file_orchestration_journal_read_contract_active_completed_and_contexts(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    active_job = _active_job(cycle_time)
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="created", jobs=[active_job]),
    )
    _write_json(journal_root / "models/model_a.json", _direct_model_context_record())
    _write_json(
        journal_root / "forcing/gfs/2026062800/model_a.json",
        _direct_forcing_context_record(cycle_time=cycle_time),
    )

    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_orchestration(source_id="gfs", cycle_time=cycle_time) is True
    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False
    active = repository.active_slurm_jobs(source_id="gfs", cycle_time=cycle_time, model_id="model_a")
    assert active[0]["slurm_job_id"] == "3001"
    assert active[0]["runtime_roots"] == {"workspace_root": "[local-path]", "object_store_root": "[local-path]"}
    assert repository.query_candidate_state(active_job["idempotency_key"])["job_id"] == active_job["job_id"]
    assert repository.get_pipeline_job(active_job["job_id"])["slurm_job_id"] == "3001"
    assert repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))[0]["job_id"] == active_job["job_id"]
    assert repository.query_pipeline_jobs_by_run(active_job["run_id"])[0]["job_id"] == active_job["job_id"]
    assert repository.query_pipeline_job_by_slurm_id("3001")["job_id"] == active_job["job_id"]

    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_a",
        run_id="fcst_gfs_2026062800_model_a",
        forcing_version_id="forc_gfs_2026062800_model_a",
        candidate_id="gfs:2026-06-28T00:00:00Z:model_a:forecast_gfs_deterministic",
    )
    assert state is not None
    assert state["pipeline_status"] == "queued"
    assert state["hydro_status"] == "created"
    assert state["pipeline_jobs_total"] == 1

    model = repository.load_model_context("model_a")
    assert model.model_id == "model_a"
    assert model.output_segment_count == 5
    forcing = repository.find_forcing_context(source_id="gfs", cycle_time=cycle_time, model_id="model_a")
    assert forcing.forcing_version_id == "forc_gfs_2026062800_model_a"
    assert forcing.max_lead_hours == 3

    _write_json(
        journal_root / "latest/gfs/2026062800/model_b.json",
        _latest_view(cycle_time=cycle_time, model_id="model_b", hydro_status="complete"),
    )
    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_b") is True


def test_file_orchestration_journal_active_slurm_jobs_ignores_local_jobs(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    local_job = _active_job(cycle_time)
    local_job["job_id"] = "job_local_publish"
    local_job["job_type"] = "publish_tiles"
    local_job["stage"] = "publish"
    local_job["slurm_job_id"] = "local"
    real_job = _active_job(cycle_time)
    real_job["job_id"] = "job_forcing"
    real_job["stage"] = "forcing"
    real_job["slurm_job_id"] = "3001"
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="created", jobs=[local_job, real_job]),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    active = repository.active_slurm_jobs(source_id="gfs", cycle_time=cycle_time, model_id="model_a")

    assert [job["slurm_job_id"] for job in active] == ["3001"]


def test_file_orchestration_journal_compute_terminal_ignores_legacy_publish_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    state_save = _active_job(cycle_time)
    state_save.update(
        {
            "job_id": "job_cycle_gfs_2026062800_model_a_state_save_qc",
            "job_type": "save_state_snapshot_array",
            "stage": "state_save_qc",
            "status": "succeeded",
            "slurm_job_id": "3002",
            "finished_at": "2026-06-28T00:04:00Z",
        }
    )
    legacy_publish = _active_job(cycle_time)
    legacy_publish.update(
        {
            "job_id": "job_cycle_gfs_2026062800_model_a_publish",
            "job_type": "publish_tiles",
            "stage": "publish",
            "status": "pending",
            "slurm_job_id": None,
            "created_at": "2026-06-28T00:05:00Z",
        }
    )
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[state_save, legacy_publish]),
    )
    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", "forecast_state_save_qc")
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_orchestration(source_id="gfs", cycle_time=cycle_time) is False
    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False
    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "succeeded"
    assert state["stage"] == "state_save_qc"
    assert [job["stage"] for job in state["pipeline_jobs"]] == ["state_save_qc"]


def test_file_orchestration_journal_ignores_unsubmitted_retry_placeholder_in_active_gate(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    retry_placeholder = _active_job(cycle_time)
    retry_placeholder.update(
        {
            "job_id": "job_cycle_gfs_2026062800_model_a_forcing_retry_1_retry_2",
            "job_type": "produce_forcing_array",
            "stage": "forcing",
            "status": "pending",
            "slurm_job_id": None,
            "array_task_id": None,
            "submitted_at": None,
            "candidate_id": None,
            "idempotency_key": None,
            "retry_count": 2,
        }
    )
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[retry_placeholder]),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_orchestration(source_id="gfs", cycle_time=cycle_time) is False
    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False


def test_file_orchestration_journal_treats_reservation_lost_as_terminal(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    lost = _active_job(cycle_time)
    lost.update(
        {
            "status": "reservation_lost",
            "slurm_job_id": None,
            "submitted_at": None,
            "error_code": "SLURM_RESERVATION_LOST",
        }
    )
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[lost]),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_orchestration(source_id="gfs", cycle_time=cycle_time) is False
    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False


def test_file_orchestration_journal_terminal_state_save_overrides_hydro_created(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    state_save = _active_job(cycle_time)
    state_save.update(
        {
            "job_id": "job_cycle_gfs_2026062800_model_a_state_save_qc",
            "job_type": "save_state_snapshot_array",
            "stage": "state_save_qc",
            "status": "succeeded",
            "slurm_job_id": "3002",
            "finished_at": "2026-06-28T00:04:00Z",
        }
    )
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="created", jobs=[state_save]),
    )
    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", "forecast_state_save_qc")
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False
    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True


def test_file_orchestration_journal_canonical_source_alias_reads_canonical_paths(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    active_job = _active_job(cycle_time)
    latest = _latest_view(cycle_time=cycle_time, hydro_status="complete", jobs=[active_job])
    latest["forcing_version"] = None
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    _write_json(
        journal_root / "forcing/gfs/2026062800/model_a.json",
        _direct_forcing_context_record(cycle_time=cycle_time),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_orchestration(source_id="GFS", cycle_time=cycle_time) is True
    assert repository.has_active_pipeline(source_id="GFS", cycle_time=cycle_time, model_id="model_a") is True
    assert repository.has_completed_pipeline(source_id="GFS", cycle_time=cycle_time, model_id="model_a") is True
    assert repository.active_slurm_jobs(source_id="GFS", cycle_time=cycle_time, model_id="model_a")[0][
        "slurm_job_id"
    ] == "3001"

    state = _candidate_state(repository, source_id="GFS", cycle_time=cycle_time)
    assert state is not None
    assert state["candidate_id"].startswith("gfs:")
    assert state["run_id"] == "fcst_gfs_2026062800_model_a"
    assert state["forcing_version_id"] == "forc_gfs_2026062800_model_a"
    assert state["pipeline_status"] == "queued"

    forcing = repository.find_forcing_context(source_id="GFS", cycle_time=cycle_time, model_id="model_a")
    assert forcing.forcing_version_id == "forc_gfs_2026062800_model_a"
    assert forcing.max_lead_hours == 9


def test_candidate_state_applies_the_direct_file_forcing_fallback_the_context_read_has(
    tmp_path: Path,
) -> None:
    # B7/AC-3 (#1203, round-1 revision): with no forcing_version row, the
    # candidate-state read must recover the same journal direct-file provenance
    # ``find_forcing_context`` already recovers, marked with its tier so an
    # operator can tell them apart.  The CONSISTENCY evidence is the forcing
    # version IDENTITY: the two reads are not required to agree on the uri, because
    # this read is the public one and the public-read redaction boundary withholds
    # every s3-shaped uri behind a placeholder.  The placeholder assertion below
    # documents that boundary; it is not a "the uris agree" claim.
    from services.orchestrator.scheduler_init_state_match import EVIDENCE_REDACTION_PLACEHOLDERS

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    latest = _latest_view(cycle_time=cycle_time, hydro_status="created", jobs=[_active_job(cycle_time)])
    latest["forcing_version"] = None
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    _write_json(
        journal_root / "forcing/gfs/2026062800/model_a.json",
        _direct_forcing_context_record(cycle_time=cycle_time),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    forcing = repository.find_forcing_context(source_id="gfs", cycle_time=cycle_time, model_id="model_a")
    state = _candidate_state(repository, cycle_time=cycle_time)

    assert state is not None
    # The agreement pin: same recovered forcing version identity from both reads.
    assert state["forcing_version"]["forcing_version_id"] == forcing.forcing_version_id
    assert forcing.forcing_version_id == "forc_gfs_2026062800_model_a"
    assert state["forcing_version"]["forcing_version_source"] == "direct"
    # The boundary pin: the recorded ``s3://`` uri reaches this read withheld, as
    # the canonical redaction placeholder that downstream decision logic treats as
    # "not probeable" rather than as a package reference.
    assert forcing.forcing_package_uri == "s3://nhms/forcing/direct.tar"
    recovered_uri = state["forcing_version"]["forcing_package_uri"]
    assert recovered_uri == "[object-uri]"
    assert recovered_uri in EVIDENCE_REDACTION_PLACEHOLDERS


def test_candidate_state_withholds_a_recorded_copyback_source_uri_behind_the_placeholder(
    tmp_path: Path,
) -> None:
    # #1367 premise pin: the geometry the copyback withheld-reference guard exists
    # for is produced by this read, not simulated.  An s3-shaped
    # ``copyback_source_uri`` recorded on a job crosses the public-read redaction
    # boundary and reaches the scheduler as ``[object-uri]``, which is exactly what
    # the guard's alias resolution then hands the copyback leg.
    from services.orchestrator.scheduler_init_state_match import EVIDENCE_REDACTION_PLACEHOLDERS

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    recorded_uri = "s3://nhms/runs/fcst_gfs_2026062800_model_a/output/summary.json"
    job["details"] = {"copyback_source_uri": recorded_uri}
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="failed", jobs=[job]),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    state = _candidate_state(repository, cycle_time=cycle_time)

    assert state is not None
    recovered_uri = state["pipeline_jobs"][0]["details"]["copyback_source_uri"]
    assert recovered_uri == "[object-uri]"
    assert recovered_uri in EVIDENCE_REDACTION_PLACEHOLDERS
    assert recorded_uri not in json.dumps(state, sort_keys=True)
    # The guard resolves the withheld value, not the recorded one.
    assert (
        scheduler_state_failure_module._first_artifact_uri(
            state,
            ("copyback_source_uri", "copyback_source", "copyback_source_path", "copyback_uri"),
        )
        == "[object-uri]"
    )


def test_candidate_state_marks_the_journal_row_forcing_provenance_tier(tmp_path: Path) -> None:
    # B7/B8: a row-tier hit is marked ``journal`` and the recovered row is a copy.
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="created", jobs=[_active_job(cycle_time)]),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    state = _candidate_state(repository, cycle_time=cycle_time)

    assert state is not None
    assert state["forcing_version"]["forcing_version_source"] == "journal"
    assert state["forcing_version"]["forcing_version_id"] == "forc_gfs_2026062800_model_a"
    rows = repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id="model_a")
    assert "forcing_version_source" not in rows.forcing_version


def test_candidate_state_keeps_null_forcing_provenance_when_both_journal_tiers_are_empty(
    tmp_path: Path,
) -> None:
    # B7: honest null -- no fabricated record when neither journal tier witnesses.
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    latest = _latest_view(cycle_time=cycle_time, hydro_status="created", jobs=[_active_job(cycle_time)])
    latest["forcing_version"] = None
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)

    state = _candidate_state(repository, cycle_time=cycle_time)

    assert state is not None
    assert state["forcing_version"] is None
    assert repository.find_forcing_context(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_a",
    ).forcing_version_id is None


def test_candidate_state_degrades_a_corrupt_direct_forcing_file_instead_of_failing_the_pass(
    tmp_path: Path,
) -> None:
    # B7 (design D1 error-semantics ruling): the two read paths deliberately
    # diverge on a corrupt direct file.  ``find_forcing_context`` is an explicit
    # query and still raises; ``candidate_state`` is a bulk per-candidate
    # derivation, so one corrupt file must not fail the whole scheduler pass.
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    latest = _latest_view(cycle_time=cycle_time, hydro_status="created", jobs=[_active_job(cycle_time)])
    latest["forcing_version"] = None
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    corrupt = _direct_forcing_context_record(cycle_time=cycle_time)
    corrupt["record_type"] = "model_context"
    _write_json(journal_root / "forcing/gfs/2026062800/model_a.json", corrupt)
    repository = FileOrchestrationJournalRepository(journal_root)

    state = _candidate_state(repository, cycle_time=cycle_time)

    assert state is not None
    assert state["forcing_version"] is None
    assert state["pipeline_status"] == "queued"
    with pytest.raises(OrchestratorError) as error:
        repository.find_forcing_context(source_id="gfs", cycle_time=cycle_time, model_id="model_a")
    assert error.value.error_code == "FILE_JOURNAL_READ_BLOCKED"
    assert "file_journal_record_type_mismatch" in error.value.message


def test_file_orchestration_journal_source_scoped_read_handles_lowercase_ifs_history(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_stamp = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    active_job = _active_job(cycle_time)
    active_job.update(
        {
            "job_id": f"job_cycle_ifs_{cycle_stamp}_forecast",
            "idempotency_key": f"cycle_ifs_{cycle_stamp}:forecast",
            "run_id": f"fcst_ifs_{cycle_stamp}_model_a",
            "cycle_id": cycle_id_for("IFS", cycle_time),
            "source_id": "IFS",
        }
    )
    latest = _latest_view(source_id="IFS", cycle_time=cycle_time, hydro_status=None, jobs=[active_job])
    latest["forcing_version"] = None
    _write_json(journal_root / "latest/ifs/2026062800/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="IFS", cycle_time=cycle_time, model_id="model_a") is True
    active = repository.active_slurm_jobs(source_id="IFS", cycle_time=cycle_time, model_id="model_a")
    assert active[0]["source_id"] == "IFS"
    assert active[0]["slurm_job_id"] == "3001"
    state = _candidate_state(repository, source_id="IFS", cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "queued"
    assert state["pipeline_jobs"][0]["source_id"] == "IFS"


def test_file_orchestration_journal_source_scoped_completed_read_handles_lowercase_era5_history(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_stamp = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    terminal_job = _active_job(cycle_time)
    terminal_job.update(
        {
            "job_id": f"job_cycle_era5_{cycle_stamp}_state_save_qc",
            "idempotency_key": f"cycle_era5_{cycle_stamp}:state_save_qc",
            "run_id": f"fcst_era5_{cycle_stamp}_model_a",
            "cycle_id": cycle_id_for("ERA5", cycle_time),
            "source_id": "ERA5",
            "stage": "state_save_qc",
            "status": "succeeded",
            "finished_at": "2026-06-28T00:05:00Z",
        }
    )
    latest = _latest_view(source_id="ERA5", cycle_time=cycle_time, hydro_status=None, jobs=[terminal_job])
    latest["forcing_version"] = None
    _write_json(journal_root / "latest/era5/2026062800/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_completed_pipeline(source_id="ERA5", cycle_time=cycle_time, model_id="model_a") is True
    state = _candidate_state(repository, source_id="ERA5", cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "succeeded"
    assert state["pipeline_jobs"][0]["source_id"] == "ERA5"


def test_file_orchestration_journal_accepted_row_source_alias_matches_canonical_callers(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    job["source_id"] = "GFS"
    latest = _latest_view(source_id="GFS", cycle_time=cycle_time, hydro_status="created", jobs=[job])
    assert latest["hydro_run"] is not None
    latest["hydro_run"]["run_id"] = "fcst_gfs_2026062800_model_a"
    latest["forcing_version"]["forcing_version_id"] = "forc_gfs_2026062800_model_a"
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    assert repository.active_slurm_jobs(source_id="gfs", cycle_time=cycle_time, model_id="model_a")[0][
        "job_id"
    ] == job["job_id"]
    state = _candidate_state(repository, source_id="gfs", cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "queued"


def test_file_orchestration_journal_json_over_limit_fails_closed(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="complete"),
    )

    repository = FileOrchestrationJournalRepository(journal_root, max_bytes=32)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "running"
    assert state["file_journal"]["reason"] == "file_journal_byte_limit_exceeded"
    assert state["file_journal"]["field"] == "latest/gfs/2026062800/model_a.json"


def test_file_orchestration_journal_jsonl_over_limit_fails_closed(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_jsonl(
        journal_root / "journal/gfs/2026062800.jsonl",
        [
            {
                "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
                "sequence": 1,
                "record_type": "pipeline_job",
                "source_id": "gfs",
                "cycle_time": cycle_time.isoformat(),
                "payload": _active_job(cycle_time),
            }
        ],
    )

    repository = FileOrchestrationJournalRepository(journal_root, max_bytes=32)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "running"
    assert state["file_journal"]["reason"] == "file_journal_byte_limit_exceeded"
    assert state["file_journal"]["field"] == "journal/gfs/2026062800.jsonl"


def test_file_orchestration_journal_jsonl_record_count_limit_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    monkeypatch.setattr(journal_module, "MAX_FILE_JOURNAL_RECORDS", 1)
    _write_jsonl(
        journal_root / "journal/gfs/2026062800.jsonl",
        [
            _journal_record(
                record_type="pipeline_job",
                source_id="gfs",
                cycle_time=cycle_time,
                payload=_active_job(cycle_time),
                sequence=1,
            ),
            _journal_record(
                record_type="pipeline_job",
                source_id="gfs",
                cycle_time=cycle_time,
                payload=_active_job(cycle_time),
                sequence=2,
            ),
        ],
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    state = _candidate_state(repository, cycle_time=cycle_time)
    query = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))

    assert state is not None
    assert state["file_journal"]["reason"] == "file_journal_record_limit_exceeded"
    assert query[0]["error_code"] == "file_journal_record_limit_exceeded"


def test_file_orchestration_journal_unknown_record_type_blocks_candidate_and_query_state(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_jsonl(
        journal_root / "journal/gfs/2026062800.jsonl",
        [
            _journal_record(
                record_type="unsupported_state",
                source_id="gfs",
                cycle_time=cycle_time,
                payload={"model_id": "model_a"},
                sequence=1,
                model_id="model_a",
            )
        ],
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    state = _candidate_state(repository, cycle_time=cycle_time)
    query = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))

    assert state is not None
    assert state["file_journal"]["reason"] == "file_journal_unknown_record_type"
    assert query[0]["error_code"] == "file_journal_unknown_record_type"


def test_file_orchestration_journal_public_outputs_are_recursively_sanitized(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    job["log_uri"] = "s3://nhms/logs/job.out"
    job["details"] = {
        "log_uri": "file:///secret/job.log",
        "nested": {
            "workspace_root": "/secret/workspace",
            "artifacts": [{"output_uri": "s3://nhms/runs/out.nc"}],
            "raw_note": "s3://nhms/raw-note",
            "local_note": "/secret/local-note",
        },
        "status": "kept",
    }
    event = {
        "event_id": 1,
        "entity_type": "pipeline_job",
        "entity_id": job["job_id"],
        "event_type": "submission",
        "status_to": "queued",
        "created_at": "2026-06-28T00:01:01Z",
        "details": {"slurm": {"log_uri": "https://logs.example.test/job.out", "scratch_root": "/secret/scratch"}},
    }
    latest = _latest_view(cycle_time=cycle_time, hydro_status="created", jobs=[job], events=[event])
    assert latest["hydro_run"] is not None
    latest["hydro_run"]["log_uri"] = "/secret/hydro.log"
    latest["forcing_version"]["details"] = {
        "forcing_package_uri": "s3://nhms/forcing/nested.tar",
        "runtime_root": "/secret/runtime",
    }
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)

    repository = FileOrchestrationJournalRepository(journal_root)

    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["candidate_id"] == "gfs:2026-06-28T00:00:00+00:00:model_a:forecast_gfs_deterministic"
    assert state["pipeline_status"] == "queued"
    assert state["stage"] == "forecast"
    assert state["hydro_run"]["output_uri"] == "[object-uri]"
    assert state["hydro_run"]["log_uri"] == "[local-path]"
    assert state["output_uri"] == "[object-uri]"
    assert state["forcing_version"]["forcing_package_uri"] == "[object-uri]"
    assert state["forcing_version"]["details"]["runtime_root"] == "[local-path]"
    assert state["pipeline_jobs"][0]["job_id"] == job["job_id"]
    assert state["pipeline_jobs"][0]["log_uri"] == "[object-uri]"
    assert state["pipeline_jobs"][0]["details"]["nested"]["workspace_root"] == "[local-path]"
    assert state["pipeline_jobs"][0]["details"]["nested"]["artifacts"][0]["output_uri"] == "[object-uri]"
    assert state["pipeline_jobs"][0]["details"]["nested"]["raw_note"] == "[object-uri]"
    assert state["pipeline_jobs"][0]["details"]["nested"]["local_note"] == "[local-path]"
    assert state["pipeline_events"][0]["details"]["slurm"]["log_uri"] == "[uri]"
    assert state["pipeline_events"][0]["details"]["slurm"]["scratch_root"] == "[local-path]"

    queried = repository.query_candidate_state(job["idempotency_key"])
    assert queried is not None
    assert queried["job_id"] == job["job_id"]
    assert queried["status"] == "queued"
    assert queried["stage"] == "forecast"
    assert queried["log_uri"] == "[object-uri]"
    assert queried["details"]["log_uri"] == "[uri]"

    serialized = json.dumps({"state": state, "queried": queried}, sort_keys=True)
    assert "s3://nhms" not in serialized
    assert "/secret" not in serialized
    assert "file://" not in serialized


def test_file_orchestration_journal_malformed_forcing_context_blocks_public_context_read(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / "forcing/gfs/2026062800/model_a.json",
        _direct_forcing_context_record(cycle_time=cycle_time, payload_overrides={"max_lead_hours": {"not": "scalar"}}),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    with pytest.raises(OrchestratorError) as error:
        repository.find_forcing_context(source_id="gfs", cycle_time=cycle_time, model_id="model_a")

    assert error.value.error_code == "FILE_JOURNAL_READ_BLOCKED"
    assert "file_journal_invalid_field" in error.value.message


def test_file_orchestration_journal_direct_context_records_are_schema_bound(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(journal_root / "models/model_a.json", {"payload": _model_context()})
    _write_json(
        journal_root / "forcing/gfs/2026062800/model_a.json",
        {
            "payload": {
                "forcing_version_id": "forc_gfs_2026062800_model_a",
                "forcing_package_uri": "s3://nhms/forcing/schema-less.tar",
                "source_id": "gfs",
                "cycle_time": cycle_time.isoformat(),
                "model_id": "model_a",
            }
        },
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    with pytest.raises(OrchestratorError) as model_error:
        repository.load_model_context("model_a")
    with pytest.raises(OrchestratorError) as forcing_error:
        repository.find_forcing_context(source_id="gfs", cycle_time=cycle_time, model_id="model_a")

    assert model_error.value.error_code == "FILE_JOURNAL_READ_BLOCKED"
    assert "file_journal_schema_mismatch" in model_error.value.message
    assert forcing_error.value.error_code == "FILE_JOURNAL_READ_BLOCKED"
    assert "file_journal_schema_mismatch" in forcing_error.value.message


@pytest.mark.parametrize("missing_field", ["source_id", "cycle_time", "model_id", "forcing_version_id"])
def test_file_orchestration_journal_direct_forcing_requires_content_identity(
    tmp_path: Path,
    missing_field: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    record = _direct_forcing_context_record(cycle_time=cycle_time)
    del record["payload"][missing_field]
    _write_json(journal_root / "forcing/gfs/2026062800/model_a.json", record)

    with pytest.raises(OrchestratorError) as error:
        FileOrchestrationJournalRepository(journal_root).find_forcing_context(
            source_id="gfs",
            cycle_time=cycle_time,
            model_id="model_a",
        )

    assert error.value.error_code == "FILE_JOURNAL_READ_BLOCKED"
    assert "file_journal_missing_identity" in error.value.message


def test_file_orchestration_journal_valid_direct_context_records_are_read(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(journal_root / "models/model_a.json", _direct_model_context_record())
    _write_json(
        journal_root / "forcing/gfs/2026062800/model_a.json",
        _direct_forcing_context_record(cycle_time=cycle_time),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    model = repository.load_model_context("model_a")
    forcing = repository.find_forcing_context(source_id="gfs", cycle_time=cycle_time, model_id="model_a")

    assert model.model_id == "model_a"
    assert model.segment_count == 7
    assert forcing.forcing_version_id == "forc_gfs_2026062800_model_a"
    assert forcing.max_lead_hours == 9


def test_file_orchestration_journal_forcing_context_reads_db_lineage_json_fallback(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / "forcing/gfs/2026062800/model_a.json",
        _direct_forcing_context_record(
            cycle_time=cycle_time,
            payload_overrides={
                "max_lead_hours": None,
                "forcing_package_manifest_uri": None,
                "forcing_package_manifest_checksum": None,
                "lineage_json": {
                    "max_lead_hours": 72,
                    "forcing_package_manifest_uri": "s3://nhms/forcing/gfs/model_a/forcing_package.json",
                    "forcing_package_manifest_checksum": "sha256:forcing-package",
                },
            },
        ),
    )

    forcing = FileOrchestrationJournalRepository(journal_root).find_forcing_context(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_a",
    )

    assert forcing.max_lead_hours == 72
    assert forcing.forcing_package_manifest_uri == "s3://nhms/forcing/gfs/model_a/forcing_package.json"
    assert forcing.forcing_package_manifest_checksum == "sha256:forcing-package"


def _pipeline_reservation_record(
    cycle_time: datetime,
    *,
    job_id: str = "job_cycle_gfs_2026062800_forecast",
    status: str = "reserved",
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "run_id": f"fcst_gfs_{format_cycle_time(cycle_time)}_model_a",
        "cycle_id": cycle_id_for("gfs", cycle_time),
        "job_type": "run_shud_forecast_array",
        "model_id": "model_a",
        "status": status,
        "stage": "forecast",
        "idempotency_key": f"gfs:{cycle_id_for('gfs', cycle_time)}:basin_a:forecast",
        "candidate_id": "candidate_a",
    }


def _reserve_pipeline_job_process(
    journal_root: str,
    cycle_time_text: str,
    job_id: str,
    idempotency_key: str,
    hold_before_append: bool,
    ready_queue: Any,
    release_event: Any,
    result_queue: Any,
) -> None:
    try:
        cycle_time = _dt(cycle_time_text)
        repository = FileOrchestrationJournalRepository(Path(journal_root))
        record = _pipeline_reservation_record(cycle_time, job_id=job_id)
        record["idempotency_key"] = idempotency_key
        if hold_before_append:
            append = repository._append_journal_record_unlocked

            def blocking_append(*args: Any, **kwargs: Any) -> None:
                ready_queue.put({"status": "holding", "job_id": job_id})
                if not release_event.wait(10):
                    raise TimeoutError("timed out waiting to release overlapping journal writer")
                append(*args, **kwargs)

            setattr(repository, "_append_journal_record_unlocked", blocking_append)
        written = repository.reserve_pipeline_job(record)
        result_queue.put({"ok": True, "job_id": None if written is None else written["job_id"]})
    except BaseException as error:
        result_queue.put({"ok": False, "error": repr(error)})


def test_file_orchestration_journal_lifecycle_writes_materialize_latest_and_replay(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)

    forecast = repository.ensure_forecast_cycle(source_id="gfs", cycle_time=cycle_time)
    updated_cycle = repository.update_forecast_cycle_status(
        source_id="gfs",
        cycle_time=cycle_time,
        status="forecast_running",
    )
    run = repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "fcst_gfs_2026062800_model_a",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "forcing": {"forcing_version_id": "forc_gfs_2026062800_model_a"},
            "outputs": {
                "run_manifest_uri": "s3://nhms/manifests/run.json",
                "output_uri": "s3://nhms/runs/output",
                "log_uri": "s3://nhms/logs/run.log",
            },
        },
    )
    completed = repository.update_hydro_run_status(run["run_id"], "succeeded", slurm_job_id="3001")

    latest = json.loads((journal_root / "latest/gfs/2026062800/model_a.json").read_text(encoding="utf-8"))
    reloaded = FileOrchestrationJournalRepository(journal_root)

    assert forecast["status"] == "discovered"
    assert updated_cycle["status"] == "forecast_running"
    assert run["status"] == "created"
    assert completed["status"] == "succeeded"
    assert latest["hydro_run"]["status"] == "succeeded"
    assert latest["forecast_cycle"]["status"] == "forecast_running"
    assert latest["replay"]["latest_sequence"] >= 4
    assert reloaded.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True


def test_file_journal_candidate_state_attributes_cohort_qc_to_candidates(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", "forecast_state_save_qc")
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=cycle_time)
    repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "fcst_gfs_2026062800_model_a",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "forcing": {"forcing_version_id": "forc_gfs_2026062800_model_a"},
            "outputs": {"run_manifest_uri": "s3://nhms/manifests/run.json"},
        },
    )
    repository.update_hydro_run_status("fcst_gfs_2026062800_model_a", "succeeded", slurm_job_id="7001")
    reconciled = _source_job(
        cycle_time,
        source_id="gfs",
        job_id="job_fcst_gfs_2026062800_model_a_forecast_reconciled_7001_0",
    )
    reconciled.update(
        {
            "status": "succeeded",
            "restart_stage": "state_save_qc",
            "slurm_job_id": "7001_0",
        }
    )
    repository.upsert_pipeline_job(reconciled)
    cohort_qc = _source_job(
        cycle_time,
        source_id="gfs",
        job_id="job_cycle_gfs_2026062800_state_save_qc_cohort_abc123_state_save_qc",
        stage="state_save_qc",
    )
    cohort_qc.update(
        {
            "run_id": "cycle_gfs_2026062800_state_save_qc_cohort_abc123",
            "model_id": None,
            "job_type": "run_state_save_qc",
            "status": "succeeded",
            "slurm_job_id": "7002",
            "idempotency_key": "gfs:gfs_2026062800:cohort:state_save_qc:abc123",
        }
    )
    repository.upsert_pipeline_job(cohort_qc)

    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_a",
        run_id="fcst_gfs_2026062800_model_a",
        forcing_version_id="forc_gfs_2026062800_model_a",
        candidate_id=None,
        retry_limit=3,
        job_limit=50,
        event_limit=50,
    )

    assert state is not None
    qc_jobs = [job for job in state.get("pipeline_jobs") or [] if job.get("stage") == "state_save_qc"]
    assert qc_jobs
    assert state.get("restart_stage") is None
    assert state.get("completed_stage_evidence") is None
    # Cohort jobs are shared by all candidates: only the compact projection may
    # be attributed, or pass evidence multiplies past the size guard.
    for job in qc_jobs:
        assert "cohort_members" not in job
        assert "candidate_projections" not in job


def _own_failed_forecast_job(cycle_time: datetime, *, model_id: str = "model_a") -> dict[str, Any]:
    """The candidate's own failed forecast row (``retry_count`` 0, no pinned attempt)."""

    cycle_stamp = format_cycle_time(cycle_time)
    job = _active_job(cycle_time, model_id=model_id)
    job.update(
        {
            "job_id": f"job_fcst_gfs_{cycle_stamp}_{model_id}_forecast",
            "idempotency_key": f"gfs:{cycle_id_for('gfs', cycle_time)}:{model_id}:forecast",
            "run_id": f"fcst_gfs_{cycle_stamp}_{model_id}",
            "status": "failed",
            "stage": "forecast",
            "retry_count": 0,
            "slurm_job_id": "9001",
        }
    )
    return job


def _cycle_run_id_job(
    cycle_time: datetime,
    *,
    model_id: str | None,
    stage: str = "forecast",
    status: str = "failed",
    retry_count: int = 5,
    slurm_job_id: str = "9002",
    run_id_suffix: str = "",
) -> dict[str, Any]:
    """A pipeline job row carrying the cycle run id instead of a candidate run id.

    Production reaches the named variant when a single-basin pass names the row's
    model (``_cycle_pipeline_job_model_id``) while the cycle context falls back to
    the shared ``cycle_<source>_<stamp>`` run id.  The job id deliberately keeps the
    ``job_cycle_<source>_<stamp>_...`` grammar these rows really carry: that is the
    shape a marker degenerates into when its row is filtered out but the event is
    not, and it is exactly the id the unresolvable-entity pin gate re-resolves.
    """

    cycle_stamp = format_cycle_time(cycle_time)
    cycle_run_id = f"cycle_gfs_{cycle_stamp}"
    name_segment = f"{model_id}_" if model_id not in (None, "") else ""
    job = _active_job(cycle_time, model_id=model_id)
    job.update(
        {
            "job_id": f"job_cycle_gfs_{cycle_stamp}_{name_segment}{stage}",
            "idempotency_key": f"gfs:{cycle_id_for('gfs', cycle_time)}:{name_segment}{stage}",
            "run_id": f"{cycle_run_id}{run_id_suffix}",
            "model_id": model_id,
            "stage": stage,
            "status": status,
            "retry_count": retry_count,
            "slurm_job_id": slurm_job_id,
        }
    )
    return job


def _write_direct_pipeline_job(
    journal_root: Path,
    job: Mapping[str, Any],
    *,
    cycle_time: datetime,
    source_id: str = "gfs",
) -> dict[str, Any]:
    """Write a row as a direct ``pipeline-jobs/<job_id>.json`` record.

    A candidate reads only its OWN ``latest/<source>/<stamp>/<model>.json`` view, so
    another model's row can never be planted there — the direct record is the entry
    that actually reaches every candidate's row set.  The envelope ``model_id`` must
    match the payload's or the read is blocked as ``file_journal_model_mismatch``.
    """

    _write_json(
        journal_root / f"pipeline-jobs/{job['job_id']}.json",
        _journal_record(
            record_type="pipeline_job",
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=job.get("model_id"),
            payload=job,
        ),
    )
    return dict(job)


def _write_journal_pipeline_job(
    journal_root: Path,
    job: Mapping[str, Any],
    *,
    cycle_time: datetime,
    source_id: str = "gfs",
    sequence: int = 1,
) -> dict[str, Any]:
    """Append a row to the shared cycle journal segment.

    The direct-record entry cannot carry a SUFFIXED cohort run id — its cycle-wide
    scan (``_job_matches_source_cycle``) only accepts the exact cycle run id or a
    candidate ``fcst_...`` id — so the journal segment, which every candidate of the
    cycle replays, is the entry for those rows.
    """

    _write_jsonl(
        journal_root / f"journal/{source_id}/{format_cycle_time(cycle_time)}.jsonl",
        [
            _journal_record(
                record_type="pipeline_job",
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=job.get("model_id"),
                sequence=sequence,
                payload=job,
            )
        ],
    )
    return dict(job)


def _write_manual_retry_marker(
    journal_root: Path,
    *,
    cycle_time: datetime,
    entity_id: str,
    retry_count: int,
    source_id: str = "gfs",
    event_id: int = 11,
) -> None:
    _write_jsonl(
        journal_root / f"pipeline-events/{source_id}/{format_cycle_time(cycle_time)}.jsonl",
        [
            _journal_record(
                record_type="pipeline_event",
                source_id=source_id,
                cycle_time=cycle_time,
                model_id=None,
                sequence=event_id,
                payload={
                    "event_id": event_id,
                    "entity_type": "pipeline_job",
                    "entity_id": entity_id,
                    "event_type": "retry",
                    "created_at": "2026-06-28T00:05:00Z",
                    "details": {
                        "trigger": "manual",
                        "manual_retry_marker": True,
                        "retry_count": retry_count,
                    },
                },
            )
        ],
    )


def _foreign_model_cycle_run_fixture(
    journal_root: Path,
    cycle_time: datetime,
    *,
    with_marker: bool = True,
    **job_overrides: Any,
) -> dict[str, Any]:
    """#1288 shape: another model's named cycle-run row beside the candidate's own row."""

    _write_json(
        journal_root / f"latest/gfs/{format_cycle_time(cycle_time)}/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[_own_failed_forecast_job(cycle_time)]),
    )
    foreign = _write_direct_pipeline_job(
        journal_root,
        _cycle_run_id_job(cycle_time, model_id="model_b", **job_overrides),
        cycle_time=cycle_time,
    )
    if with_marker:
        _write_manual_retry_marker(
            journal_root,
            cycle_time=cycle_time,
            entity_id=foreign["job_id"],
            retry_count=int(foreign["retry_count"]),
        )
    return foreign


def test_candidate_state_excludes_foreign_model_cycle_run_row_and_its_marker(tmp_path: Path) -> None:
    """#1288 read side: a foreign model's named cycle-run row is not this candidate's row.

    Journal candidate-state membership must answer as the DB predicate does
    (``chain_repository_state.py:510-515``, whose cycle-run clause carries
    ``model_id IS NULL``).  The marker riding on that row has to leave in the SAME
    step: an orphaned ``pipeline_job`` event whose row is gone re-enters the pinning
    decision through the cycle-scope entity grammar.
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    foreign = _foreign_model_cycle_run_fixture(journal_root, cycle_time)

    state = _candidate_state(FileOrchestrationJournalRepository(journal_root), cycle_time=cycle_time)

    assert state is not None
    assert state.get("file_journal") is None
    assert [job.get("job_id") for job in state["pipeline_jobs"]] == [
        f"job_fcst_gfs_{format_cycle_time(cycle_time)}_model_a_forecast"
    ]
    assert foreign["job_id"] == f"job_cycle_gfs_{format_cycle_time(cycle_time)}_model_b_forecast"
    assert [(event.get("entity_type"), event.get("entity_id")) for event in state["pipeline_events"]] == []


def test_foreign_model_cycle_run_marker_cannot_pin_candidate_attempt(tmp_path: Path) -> None:
    """#1288 pin side: the foreign row's ``retry_count`` 5 must not become this candidate's attempt."""

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _foreign_model_cycle_run_fixture(journal_root, cycle_time)

    state = _candidate_state(FileOrchestrationJournalRepository(journal_root), cycle_time=cycle_time)

    assert state is not None
    assert state["failed_stage"] == "forecast"
    assert scheduler_module._manual_retry_requested(state) is False
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 1
    assert "new_attempt" not in scheduler_module._manual_retry_payload(state)


def test_model_less_cycle_scope_rows_stay_visible_to_every_candidate(tmp_path: Path) -> None:
    """Negative regression: the cohort contract (#841) survives the #1288 exclusion.

    Model-less cycle-scope rows — the exact cycle run id and the journal-only
    suffix widening — belong to every candidate of the cycle; only the foreign
    NAMED row leaves.
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_stamp = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    for model_id in ("model_a", "model_b"):
        _write_json(
            journal_root / f"latest/gfs/{cycle_stamp}/{model_id}.json",
            _latest_view(
                cycle_time=cycle_time,
                model_id=model_id,
                jobs=[_own_failed_forecast_job(cycle_time, model_id=model_id)],
            ),
        )
    cohort = _write_direct_pipeline_job(
        journal_root,
        _cycle_run_id_job(cycle_time, model_id=None, stage="download", status="failed"),
        cycle_time=cycle_time,
    )
    suffixed_cohort = _write_journal_pipeline_job(
        journal_root,
        _cycle_run_id_job(
            cycle_time,
            model_id=None,
            stage="state_save_qc",
            status="succeeded",
            run_id_suffix="_state_save_qc_cohort_abc123",
        ),
        cycle_time=cycle_time,
    )
    foreign = _write_direct_pipeline_job(
        journal_root,
        _cycle_run_id_job(cycle_time, model_id="model_b"),
        cycle_time=cycle_time,
    )

    repository = FileOrchestrationJournalRepository(journal_root)
    states = {
        model_id: _candidate_state(repository, cycle_time=cycle_time, model_id=model_id)
        for model_id in ("model_a", "model_b")
    }

    for model_id, state in states.items():
        assert state is not None, model_id
        job_ids = {job.get("job_id") for job in state["pipeline_jobs"]}
        assert cohort["job_id"] in job_ids, model_id
        assert suffixed_cohort["job_id"] in job_ids, model_id
    # The named row is nobody's cohort row but its own model's: it stays in
    # model_b's state (its absence from model_a's is asserted by the read-side
    # discriminating pair above).
    assert foreign["job_id"] in {job.get("job_id") for job in states["model_b"]["pipeline_jobs"]}


def test_candidate_own_named_cycle_run_row_keeps_visibility_and_pinning(tmp_path: Path) -> None:
    """Negative regression: the candidate's OWN named cycle-run row is untouched.

    The DB predicate includes it through its ``model_id = %s`` clause
    (``chain_repository_state.py:512``), so its marker keeps adopting and pinning.
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    own_cycle_row = _cycle_run_id_job(cycle_time, model_id="model_a")
    _write_json(
        journal_root / f"latest/gfs/{format_cycle_time(cycle_time)}/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[own_cycle_row]),
    )
    _write_manual_retry_marker(
        journal_root,
        cycle_time=cycle_time,
        entity_id=own_cycle_row["job_id"],
        retry_count=5,
    )

    state = _candidate_state(FileOrchestrationJournalRepository(journal_root), cycle_time=cycle_time)

    assert state is not None
    assert [job.get("job_id") for job in state["pipeline_jobs"]] == [own_cycle_row["job_id"]]
    assert [event.get("entity_id") for event in state["pipeline_events"]] == [own_cycle_row["job_id"]]
    assert scheduler_module._manual_retry_requested(state) is True
    assert scheduler_module._manual_retry_new_attempt(state, previous_attempt=0) == 5
    assert scheduler_module._manual_retry_payload(state)["new_attempt"] == 5


def test_foreign_model_cycle_run_row_stays_visible_to_the_duplicate_submission_gates(
    tmp_path: Path,
) -> None:
    """Negative regression: the cycle-level gates keep their wider cycle-run visibility.

    Their DB counterparts match the cycle run id unconditionally
    (``chain_repository.py:74-79`` / ``:177-181``), so narrowing the shared row
    predicate would loosen ``active_duplicate_pipeline`` instead of aligning it.
    Same row shape the read-side pair excludes from the candidate state — the answer
    here must NOT move with it.
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    foreign = _foreign_model_cycle_run_fixture(
        journal_root,
        cycle_time,
        with_marker=False,
        status="queued",
        retry_count=0,
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    assert [
        job.get("job_id")
        for job in repository.active_slurm_jobs(source_id="gfs", cycle_time=cycle_time, model_id="model_a")
    ] == [foreign["job_id"]]


def test_foreign_model_cycle_run_row_no_longer_completes_the_candidate(tmp_path: Path) -> None:
    """The completion gate's answer on the foreign shape, frozen then unfrozen.

    A queued row is a vacuous input for ``has_completed_pipeline``; only a
    succeeded completion-stage row discriminates.  #1288 froze this answer at
    ``True`` because the DB side of this gate reads ``hydro.hydro_run`` alone
    (``chain_repository.py:98-111``) and offered no job-row predicate to copy;
    #1302 unfroze it: the gate answers a candidate-scoped question, and that
    DB three-key (source/cycle/model) restriction is exactly the direction the
    journal side now aligns with — another model's completion is never this
    candidate's completion.
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _foreign_model_cycle_run_fixture(
        journal_root,
        cycle_time,
        with_marker=False,
        stage="state_save_qc",
        status="succeeded",
        retry_count=0,
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False


@pytest.mark.parametrize("stage", ["state_save_qc", "publish", "parse"])
def test_foreign_model_completion_row_does_not_complete_the_candidate(tmp_path: Path, stage: str) -> None:
    """#1302 main discriminator: a foreign model's completion is not this candidate's.

    Every completion stage of the default terminal contract is covered — the
    foreign row is excluded by identity, not by stage.
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _foreign_model_cycle_run_fixture(
        journal_root,
        cycle_time,
        with_marker=False,
        stage=stage,
        status="succeeded",
        retry_count=0,
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False


@pytest.mark.parametrize("stage", ["state_save_qc", "publish", "parse"])
def test_foreign_model_completion_row_does_not_complete_under_production_terminal_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """Same verdict under the production ``forecast_state_save_qc`` terminal contract.

    That contract returns ``has_terminal_completion`` directly
    (``file_orchestration_journal.py:575-576``), so the narrowed conjunction is
    the only thing standing between the foreign row and a ``True``.  The
    ``publish``/``parse`` rows answered ``False`` before this change too (they
    are not terminal under that contract) and must keep doing so.
    """

    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", "forecast_state_save_qc")
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _foreign_model_cycle_run_fixture(
        journal_root,
        cycle_time,
        with_marker=False,
        stage=stage,
        status="succeeded",
        retry_count=0,
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False


@pytest.mark.parametrize("hydro_status", ["failed", "cancelled", "created"])
def test_foreign_model_completion_row_cannot_complete_a_failed_candidate(
    tmp_path: Path, hydro_status: str
) -> None:
    """The harmful shape: the candidate itself failed, another model finished.

    Before #1302 the foreign row flipped this candidate to "completed" and four
    consumer surfaces skipped it silently.
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / f"latest/gfs/{format_cycle_time(cycle_time)}/model_a.json",
        _latest_view(
            cycle_time=cycle_time,
            hydro_status=hydro_status,
            jobs=[_own_failed_forecast_job(cycle_time)],
        ),
    )
    _write_direct_pipeline_job(
        journal_root,
        _cycle_run_id_job(
            cycle_time,
            model_id="model_b",
            stage="state_save_qc",
            status="succeeded",
            retry_count=0,
        ),
        cycle_time=cycle_time,
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False


@pytest.mark.parametrize("run_id_suffix", ["", "_state_save_qc_cohort_abc123"])
def test_model_less_cohort_completion_row_still_completes_every_candidate(
    tmp_path: Path, run_id_suffix: str
) -> None:
    """Negative regression: the cohort completion contract (#841) is untouched.

    Both cohort run-id shapes — the exact cycle run id and the journal-only
    suffix widening — complete every candidate of the cycle; only the foreign
    NAMED row leaves.  The suffixed row can only enter through the shared cycle
    journal segment (see ``_write_journal_pipeline_job``).
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    cohort = _cycle_run_id_job(
        cycle_time,
        model_id=None,
        stage="state_save_qc",
        status="succeeded",
        retry_count=0,
        run_id_suffix=run_id_suffix,
    )
    for model_id in ("model_a", "model_b"):
        _write_json(
            journal_root / f"latest/gfs/{format_cycle_time(cycle_time)}/{model_id}.json",
            _latest_view(
                cycle_time=cycle_time,
                model_id=model_id,
                jobs=[_own_failed_forecast_job(cycle_time, model_id=model_id)],
            ),
        )
    if run_id_suffix:
        _write_journal_pipeline_job(journal_root, cohort, cycle_time=cycle_time)
    else:
        _write_direct_pipeline_job(journal_root, cohort, cycle_time=cycle_time)
    repository = FileOrchestrationJournalRepository(journal_root)

    for model_id in ("model_a", "model_b"):
        assert (
            repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id=model_id) is True
        ), model_id


def test_candidate_own_completion_evidence_still_completes_the_candidate(tmp_path: Path) -> None:
    """Negative regression: every own-evidence arm of the gate keeps answering ``True``.

    The candidate's own NAMED cycle-run row, its own ``fcst_...`` run-id row and
    its own completed hydro run are the three legal completion sources left after
    the foreign exclusion (the hydro arm is reachable under the default terminal
    contract only — ``file_orchestration_journal.py:575-579``).
    """

    cycle_stamp = format_cycle_time(_dt("2026-06-28T00:00:00Z"))
    own_named_cycle_row = _cycle_run_id_job(
        _dt("2026-06-28T00:00:00Z"),
        model_id="model_a",
        stage="state_save_qc",
        status="succeeded",
        retry_count=0,
    )
    own_run_id_row = _own_failed_forecast_job(_dt("2026-06-28T00:00:00Z"))
    own_run_id_row.update(
        {
            "job_id": f"job_fcst_gfs_{cycle_stamp}_model_a_state_save_qc",
            "idempotency_key": f"gfs:{cycle_id_for('gfs', _dt('2026-06-28T00:00:00Z'))}:model_a:state_save_qc",
            "stage": "state_save_qc",
            "status": "succeeded",
        }
    )
    shapes: dict[str, dict[str, Any]] = {
        "own_named_cycle_row": {"jobs": [own_named_cycle_row], "hydro_status": None},
        "own_run_id_row": {"jobs": [own_run_id_row], "hydro_status": None},
        "own_completed_hydro_run": {"jobs": [], "hydro_status": "succeeded"},
    }

    for name, shape in shapes.items():
        cycle_time = _dt("2026-06-28T00:00:00Z")
        journal_root = tmp_path / name
        _write_json(
            journal_root / f"latest/gfs/{cycle_stamp}/model_a.json",
            _latest_view(cycle_time=cycle_time, hydro_status=shape["hydro_status"], jobs=shape["jobs"]),
        )
        _write_direct_pipeline_job(
            journal_root,
            _cycle_run_id_job(
                cycle_time,
                model_id="model_b",
                stage="state_save_qc",
                status="succeeded",
                retry_count=0,
            ),
            cycle_time=cycle_time,
        )
        repository = FileOrchestrationJournalRepository(journal_root)

        assert (
            repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
        ), name


def test_foreign_model_non_terminal_stage_row_never_completed_the_candidate(tmp_path: Path) -> None:
    """Negative regression: a foreign ``forecast`` success was never a completion."""

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _foreign_model_cycle_run_fixture(
        journal_root,
        cycle_time,
        with_marker=False,
        stage="forecast",
        status="succeeded",
        retry_count=0,
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False


def test_foreign_model_completion_row_leaves_the_duplicate_submission_gates_unchanged(
    tmp_path: Path,
) -> None:
    """Negative regression: the shared row predicate was NOT narrowed.

    On the very fixture whose completion verdict flips, the cycle-level
    duplicate-submission gates keep seeing the foreign rows — their DB
    counterparts match the cycle run id unconditionally
    (``chain_repository.py:74-79`` / ``:177-181``).
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _foreign_model_cycle_run_fixture(
        journal_root,
        cycle_time,
        with_marker=False,
        stage="state_save_qc",
        status="succeeded",
        retry_count=0,
    )
    foreign_active = _write_direct_pipeline_job(
        journal_root,
        _cycle_run_id_job(cycle_time, model_id="model_b", status="queued", retry_count=0),
        cycle_time=cycle_time,
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False
    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    assert [
        job.get("job_id")
        for job in repository.active_slurm_jobs(source_id="gfs", cycle_time=cycle_time, model_id="model_a")
    ] == [foreign_active["job_id"]]


def test_foreign_model_completion_row_no_longer_suppresses_the_hydro_active_arm(tmp_path: Path) -> None:
    """#1470 freeze / #1472 unfreeze: foreign completion no longer suppresses hydro-active.

    #1470 pinned the then-current behaviour — another model's named exact
    cycle-run completion suppressed this candidate's ACTIVE hydro arm, because
    ``has_active_pipeline`` kept its own local ``has_terminal_completion`` over
    the UNnarrowed shared row predicate.  #1472 unfreezes it: the foreign row
    stays visible to the wide duplicate-submission scans but is not this
    candidate's completion evidence, so the ACTIVE hydro run answers ``True``
    — matching the DB counterpart (``chain_repository.py:57-95``), a plain
    UNION with no suppression clause.  ``has_completed_pipeline`` on the same
    fixture stays ``False``: this is the active gate's verdict, not the
    completion gate's.
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / f"latest/gfs/{format_cycle_time(cycle_time)}/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="created", jobs=[]),
    )
    _write_direct_pipeline_job(
        journal_root,
        _cycle_run_id_job(
            cycle_time,
            model_id="model_b",
            stage="state_save_qc",
            status="succeeded",
            retry_count=0,
        ),
        cycle_time=cycle_time,
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False


@pytest.mark.parametrize("hydro_status", ["created", "staged", "submitted", "running"])
@pytest.mark.parametrize("stage", ["state_save_qc", "publish", "parse"])
def test_foreign_model_completion_row_cannot_suppress_any_active_hydro_status(
    tmp_path: Path, hydro_status: str, stage: str
) -> None:
    """#1472 main discriminator matrix: foreign completion never suppresses ACTIVE hydro.

    Every ACTIVE hydro status of the default terminal contract (``created``,
    ``staged``, ``submitted``, ``running``) crossed with every completion stage
    (``state_save_qc``, ``publish``, ``parse``): the candidate has no active
    pipeline-job row, so the answer must come from the hydro-active arm alone —
    the foreign row is excluded from the suppression conjunction by identity,
    not by stage or status.
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / f"latest/gfs/{format_cycle_time(cycle_time)}/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status=hydro_status, jobs=[]),
    )
    _write_direct_pipeline_job(
        journal_root,
        _cycle_run_id_job(
            cycle_time,
            model_id="model_b",
            stage=stage,
            status="succeeded",
            retry_count=0,
        ),
        cycle_time=cycle_time,
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True


@pytest.mark.parametrize("hydro_status", ["created", "staged", "submitted", "running"])
def test_foreign_model_completion_row_cannot_suppress_under_production_terminal_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hydro_status: str,
) -> None:
    """Same verdict under the production ``forecast_state_save_qc`` terminal contract.

    Under that contract ``has_terminal_completion`` accepts only
    ``state_save_qc`` completion rows; the foreign ``state_save_qc`` row must
    still not suppress the ACTIVE hydro arm.
    """

    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", "forecast_state_save_qc")
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / f"latest/gfs/{format_cycle_time(cycle_time)}/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status=hydro_status, jobs=[]),
    )
    _write_direct_pipeline_job(
        journal_root,
        _cycle_run_id_job(
            cycle_time,
            model_id="model_b",
            stage="state_save_qc",
            status="succeeded",
            retry_count=0,
        ),
        cycle_time=cycle_time,
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True


@pytest.mark.parametrize("hydro_status", ["created", "staged"])
def test_candidate_own_completion_rows_still_suppress_stale_active_hydro(
    tmp_path: Path, hydro_status: str
) -> None:
    """Negative regression: the candidate's own completion still suppresses stale hydro.

    Both own-evidence arms of the suppression conjunction — the candidate's own
    ``fcst_...`` run-id row and its own NAMED exact cycle-run row — keep their
    suppression authority over a stale ``created``/``staged`` hydro placeholder
    (#1472 must not delete suppression, only the foreign exclusion).
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_stamp = format_cycle_time(cycle_time)
    own_run_id_row = _own_failed_forecast_job(cycle_time)
    own_run_id_row.update(
        {
            "job_id": f"job_fcst_gfs_{cycle_stamp}_model_a_state_save_qc",
            "idempotency_key": f"gfs:{cycle_id_for('gfs', cycle_time)}:model_a:state_save_qc",
            "stage": "state_save_qc",
            "status": "succeeded",
            "retry_count": 0,
        }
    )
    own_named_cycle_row = _cycle_run_id_job(
        cycle_time,
        model_id="model_a",
        stage="state_save_qc",
        status="succeeded",
        retry_count=0,
    )
    shapes: dict[str, dict[str, Any]] = {
        "own_run_id_row": {"jobs": [own_run_id_row], "hydro_status": hydro_status},
        "own_named_cycle_row": {"jobs": [own_named_cycle_row], "hydro_status": hydro_status},
    }

    for name, shape in shapes.items():
        journal_root = tmp_path / name
        _write_json(
            journal_root / f"latest/gfs/{cycle_stamp}/model_a.json",
            _latest_view(cycle_time=cycle_time, hydro_status=shape["hydro_status"], jobs=shape["jobs"]),
        )
        repository = FileOrchestrationJournalRepository(journal_root)

        assert (
            repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False
        ), name


@pytest.mark.parametrize("hydro_status", ["created", "staged"])
@pytest.mark.parametrize("run_id_suffix", ["", "_state_save_qc_cohort_abc123"])
def test_model_less_cohort_completion_rows_still_suppress_stale_active_hydro(
    tmp_path: Path, hydro_status: str, run_id_suffix: str
) -> None:
    """Negative regression: model-less cohort completion keeps cycle-wide suppression.

    Both cohort run-id shapes — the exact cycle run id and the journal-only
    suffix widening — still suppress a stale ACTIVE hydro placeholder for the
    candidate (#1472 only excludes the foreign NAMED row; model-less cohort
    completion remains cycle-wide suppression authority).
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    cohort = _cycle_run_id_job(
        cycle_time,
        model_id=None,
        stage="state_save_qc",
        status="succeeded",
        retry_count=0,
        run_id_suffix=run_id_suffix,
    )
    _write_json(
        journal_root / f"latest/gfs/{format_cycle_time(cycle_time)}/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status=hydro_status, jobs=[]),
    )
    if run_id_suffix:
        _write_journal_pipeline_job(journal_root, cohort, cycle_time=cycle_time)
    else:
        _write_direct_pipeline_job(journal_root, cohort, cycle_time=cycle_time)
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False


def test_file_orchestration_journal_write_strips_redaction_placeholders(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)

    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=cycle_time)
    repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "fcst_gfs_2026062800_model_a",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "forcing": {"forcing_version_id": "forc_gfs_2026062800_model_a"},
            "outputs": {
                "run_manifest_uri": "[object-uri]",
                "output_uri": "[object-uri]",
                "log_uri": "s3://nhms/logs/run.log",
            },
        },
    )

    latest = json.loads((journal_root / "latest/gfs/2026062800/model_a.json").read_text(encoding="utf-8"))
    stored = latest["hydro_run"]
    assert stored["run_manifest_uri"] is None
    assert stored["output_uri"] is None
    assert stored["log_uri"] == "s3://nhms/logs/run.log"


def test_file_orchestration_journal_ensure_forecast_cycle_preserves_existing_status(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")

    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=cycle_time)
    repository.update_forecast_cycle_status(
        source_id="gfs",
        cycle_time=cycle_time,
        status="failed",
        error_code="RAW_MISSING",
        error_message="raw manifest missing",
    )
    ensured = repository.ensure_forecast_cycle(source_id="gfs", cycle_time=cycle_time)
    state = _candidate_state(repository, cycle_time=cycle_time)

    assert ensured["status"] == "failed"
    assert ensured["error_code"] == "RAW_MISSING"
    assert state is not None
    assert state["forecast_cycle"]["status"] == "failed"


def test_file_orchestration_journal_candidate_state_ignores_global_terminal_cycle_success(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_id = cycle_id_for("gfs", cycle_time)
    journal_root = tmp_path / "journal"
    terminal_event = {
        "event_id": "cycle-complete",
        "entity_type": "forecast_cycle",
        "entity_id": cycle_id,
        "event_type": "status_change",
        "status_from": "forecast_running",
        "status_to": "complete",
        "created_at": "2026-06-28T00:30:00Z",
    }
    latest = _latest_view(cycle_time=cycle_time, events=[terminal_event])
    latest["forecast_cycle"]["status"] = "complete"
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)

    state = _candidate_state(repository, cycle_time=cycle_time)

    assert state is not None
    assert state["forecast_cycle"] is None
    assert [event["entity_type"] for event in state["pipeline_events"]] == []


def test_file_orchestration_journal_status_error_messages_are_redacted_at_write_boundaries(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    secret_message = (
        "status failed for https://alice:pass123@slurm.example/status?"
        "X-Amz-Signature=sig123&token=tok123 token=tok123 password=pass123 "
        "Authorization: Bearer live-token-123 authorization=Basic basic-secret-123 "
        "{\"Authorization\": \"Bearer json-status-token-123\"}"
    )
    raw_secrets = (
        "alice:pass123",
        "pass123",
        "sig123",
        "tok123",
        "live-token-123",
        "basic-secret-123",
        "json-status-token-123",
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=cycle_time)
    forecast = repository.update_forecast_cycle_status(
        source_id="gfs",
        cycle_time=cycle_time,
        status="failed",
        error_code="RAW_SECRET",
        error_message=secret_message,
    )
    run = repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "fcst_gfs_2026062800_model_a",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "forcing": {"forcing_version_id": "forc_gfs_2026062800_model_a"},
        },
    )
    hydro = repository.update_hydro_run_status(
        run["run_id"],
        "failed",
        error_code="HYDRO_SECRET",
        error_message=secret_message,
    )
    record = _pipeline_reservation_record(cycle_time, job_id="job_secret_status_failed")
    repository.reserve_pipeline_job(record)
    _previous_status, job = repository.update_pipeline_job_status(
        "job_secret_status_failed",
        "failed",
        error_code="PIPELINE_SECRET",
        error_message=secret_message,
        finished_at=cycle_time,
    )

    raw_journal = (journal_root / "journal/gfs/2026062800.jsonl").read_text(encoding="utf-8")
    direct_rendered = (journal_root / "pipeline-jobs/job_secret_status_failed.json").read_text(encoding="utf-8")
    latest_rendered = "\n".join(path.read_text(encoding="utf-8") for path in (journal_root / "latest").rglob("*.json"))
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    read_rendered = json.dumps(
        {
            "forecast": forecast,
            "hydro": hydro,
            "job": job,
            "read_job": repository.get_pipeline_job("job_secret_status_failed"),
            "read_hydro": repository._hydro_run_for(run["run_id"]),
            "state": state,
        },
        sort_keys=True,
    )

    for rendered in (raw_journal, direct_rendered, latest_rendered, read_rendered):
        for raw_secret in raw_secrets:
            assert raw_secret not in rendered
        assert "[redacted]" in rendered


def test_file_orchestration_journal_lifecycle_updates_cycle_cohort_run_ids(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")

    run = repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "cycle_gfs_2026062800_convert_model_a",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "forcing": {"forcing_version_id": "forc_gfs_2026062800_model_a"},
        },
    )
    updated = repository.update_hydro_run_status(run["run_id"], "succeeded", slurm_job_id="3001")

    assert updated["run_id"] == "cycle_gfs_2026062800_convert_model_a"
    assert updated["status"] == "succeeded"
    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True


def test_file_orchestration_journal_pipeline_reservation_bind_event_and_terminal_guards(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    record = _pipeline_reservation_record(cycle_time)

    created = repository.reserve_pipeline_job(record)
    duplicate = repository.reserve_pipeline_job(record)
    bound = repository.bind_pipeline_job_reservation(record["idempotency_key"], slurm_job_id="3001")
    duplicate_bind = repository.bind_pipeline_job_reservation(record["idempotency_key"], slurm_job_id="3002")
    previous_status, succeeded = repository.update_pipeline_job_status(
        record["job_id"],
        "succeeded",
        finished_at=cycle_time,
    )
    event = repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=record["job_id"],
        event_type="status_change",
        status_from="submitted",
        status_to="succeeded",
        details={"stage": "forecast", "slurm_job_id": "3001"},
    )
    guarded_previous, guarded = repository.update_pipeline_job_status(record["job_id"], "running")

    assert created is not None
    assert created["status"] == "reserved"
    assert duplicate is None
    assert bound is not None
    assert bound["status"] == "submitted"
    assert bound["slurm_job_id"] == "3001"
    assert duplicate_bind is None
    assert previous_status == "submitted"
    assert succeeded["status"] == "succeeded"
    assert event["status_to"] == "succeeded"
    assert guarded_previous == "succeeded"
    assert guarded["status"] == "succeeded"
    assert repository.get_pipeline_job(record["job_id"])["status"] == "succeeded"
    assert repository.query_pipeline_job_by_slurm_id("3001")["job_id"] == record["job_id"]
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "succeeded"
    assert state["pipeline_jobs"][0]["status"] == "succeeded"
    assert state["pipeline_events"][0]["status_to"] == "succeeded"


def test_file_orchestration_journal_exposes_restart_reconcile_store_interface(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_reconcile_reserved")
    pending_bound = _pipeline_reservation_record(cycle_time, job_id="job_reconcile_pending_bound", status="pending")
    pending_bound["idempotency_key"] = "gfs:gfs_2026062800:basin_a:forecast_pending_bound"
    pending_bound["slurm_job_id"] = "3002"

    created = repository.reserve_pipeline_job(record)
    repository.upsert_pipeline_job(pending_bound)
    reserved = repository.query_reserved_unbound_jobs()
    bound = repository.bind_reservation(record["idempotency_key"], slurm_job_id="3001")
    inflight = repository.query_inflight_jobs()
    updated = repository.update_job_status(record["job_id"], "running")

    assert created is not None
    assert [job.job_id for job in reserved] == ["job_reconcile_reserved"]
    assert isinstance(reserved[0].updated_at, datetime)
    assert bound is not None
    assert bound.status == "submitted"
    assert bound.slurm_job_id == "3001"
    assert {job.job_id for job in inflight} == {"job_reconcile_reserved", "job_reconcile_pending_bound"}
    assert updated.status == "running"
    assert repository.get_pipeline_job(record["job_id"])["status"] == "running"
    assert repository.query_reserved_unbound_jobs() == []


def test_file_orchestration_journal_migration_blocks_bad_journal_path_until_repaired(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    pending_bound = _pipeline_reservation_record(
        cycle_time,
        job_id="job_reconcile_pending_after_bad_path",
        status="pending",
    )
    pending_bound["idempotency_key"] = "gfs:gfs_2026062800:basin_a:forecast_pending_after_bad_path"
    pending_bound["slurm_job_id"] = "3003"
    repository.upsert_pipeline_job(pending_bound)

    bad_path = journal_root / "journal" / "not_a_source" / "bad_cycle.jsonl"
    bad_path.parent.mkdir(parents=True)
    bad_path.write_text('{"record_type":"pipeline_job","job_id":"bad"}\n', encoding="utf-8")

    with pytest.raises(FileOrchestrationJournalError):
        repository.query_inflight_jobs()
    assert not (journal_root / "reconcile-inventory-migration-v1.json").exists()

    bad_path.unlink()
    reopened = FileOrchestrationJournalRepository(journal_root)
    inflight = reopened.query_inflight_jobs()
    assert {job.job_id for job in inflight} == {"job_reconcile_pending_after_bad_path"}
    assert (journal_root / "reconcile-inventory-migration-v1.json").is_file()


def test_file_orchestration_journal_reconcile_inventory_scan_closes_directory_fds(tmp_path: Path) -> None:
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root, max_files=64)
    for index in range(16):
        cycle_time = _dt(f"2026-06-28T{index % 10:02d}:00:00Z")
        job = _pipeline_reservation_record(
            cycle_time,
            job_id=f"job_reconcile_fd_stability_{index}",
            status="pending",
        )
        job["slurm_job_id"] = str(5000 + index)
        repository.upsert_pipeline_job(job)

    assert len(repository.query_inflight_jobs()) == 16

    before = _open_fd_count_or_skip()
    for _ in range(40):
        assert len(repository.query_inflight_jobs()) == 16
    after = _open_fd_count_or_skip()

    assert after - before <= 4


def test_safe_fs_missing_child_read_closes_intermediate_parent_fds(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    (root / "latest").mkdir(parents=True)

    before = _open_fd_count_or_skip()
    for _ in range(40):
        with pytest.raises(FileNotFoundError):
            safe_fs.read_bytes_limited_no_follow(
                root / "latest" / "missing_source" / "2026062800" / "model_a.json",
                max_bytes=32,
                containment_root=root,
            )
    after = _open_fd_count_or_skip()

    assert after - before <= 4


def test_file_orchestration_journal_cycle_rows_missing_alias_reads_close_parent_fds(tmp_path: Path) -> None:
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root, max_files=128)
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_segment = format_cycle_time(cycle_time)
    journal_records = []
    for index in range(16):
        model_id = f"model_{index}"
        _write_json(
            journal_root / "latest" / "gfs" / cycle_segment / f"{model_id}.json",
            _latest_view(source_id="gfs", cycle_time=cycle_time, model_id=model_id, jobs=[]),
        )
        job = _pipeline_reservation_record(
            cycle_time,
            job_id=f"job_cycle_gfs_{cycle_segment}_{model_id}_forcing",
            status="pending",
        )
        job.update(
            {
                "cycle_id": f"gfs_{cycle_segment}",
                "job_type": "produce_forcing_array",
                "model_id": model_id,
                "run_id": f"cycle_gfs_{cycle_segment}_{model_id}",
                "slurm_job_id": str(6000 + index),
                "stage": "forcing",
            }
        )
        journal_records.append(
            _journal_record(
                record_type="pipeline_job",
                source_id="gfs",
                cycle_time=cycle_time,
                payload=job,
                model_id=model_id,
                sequence=index + 1,
            )
        )
    _write_jsonl(journal_root / "journal" / "gfs" / f"{cycle_segment}.jsonl", journal_records)

    before = _open_fd_count_or_skip()
    for index in range(16):
        repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=f"model_{index}")
    after = _open_fd_count_or_skip()

    assert after - before <= 4


def test_file_orchestration_journal_migration_backfill_is_not_limited_by_former_recent_bound(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    old_reserved = _pipeline_reservation_record(cycle_time, job_id="job_reconcile_old_reserved")
    old_reserved["idempotency_key"] = "gfs:gfs_2026062800:basin_a:forecast_old_reserved"
    old_inflight = _pipeline_reservation_record(
        cycle_time,
        job_id="job_reconcile_old_inflight",
        status="running",
    )
    old_inflight["idempotency_key"] = "gfs:gfs_2026062800:basin_a:forecast_old_inflight"
    old_inflight["slurm_job_id"] = "3999"
    repository.upsert_pipeline_job(old_reserved)
    repository.upsert_pipeline_job(old_inflight)
    for index in range(5):
        newer = _pipeline_reservation_record(
            _dt(f"2026-06-28T0{index + 1}:00:00Z"),
            job_id=f"job_reconcile_newer_terminal_{index}",
            status="succeeded",
        )
        newer["idempotency_key"] = f"gfs:gfs_202606280{index + 1}:basin_a:terminal_{index}"
        newer["slurm_job_id"] = str(4100 + index)
        repository.upsert_pipeline_job(newer)

    reserved = repository.query_reserved_unbound_jobs()
    inflight = repository.query_inflight_jobs()

    assert [job.job_id for job in reserved] == ["job_reconcile_old_reserved"]
    assert [job.job_id for job in inflight] == ["job_reconcile_old_inflight"]


def test_file_orchestration_journal_migration_fails_closed_on_bad_entry_then_keeps_old_active(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    old_reserved = _pipeline_reservation_record(cycle_time, job_id="job_reconcile_old_reserved_after_bad_direct")
    old_reserved["idempotency_key"] = "gfs:gfs_2026062800:basin_a:forecast_old_reserved_after_bad_direct"
    repository.upsert_pipeline_job(old_reserved)
    os.utime(journal_root / "journal/gfs/2026062800.jsonl", (1, 1))

    for index in range(3):
        terminal_cycle_time = _dt(f"2026-06-28T0{index + 1}:00:00Z")
        terminal = _pipeline_reservation_record(
            terminal_cycle_time,
            job_id=f"job_reconcile_newer_terminal_after_bad_direct_{index}",
            status="succeeded",
        )
        terminal["idempotency_key"] = f"gfs:gfs_202606280{index + 1}:basin_a:terminal_after_bad_direct_{index}"
        terminal["slurm_job_id"] = str(4200 + index)
        terminal_journal_path = journal_root / f"journal/gfs/{format_cycle_time(terminal_cycle_time)}.jsonl"
        _write_jsonl(
            terminal_journal_path,
            [
                _journal_record(
                    record_type="pipeline_job",
                    source_id="gfs",
                    cycle_time=terminal_cycle_time,
                    payload=terminal,
                )
            ],
        )
        os.utime(terminal_journal_path, (10 + index, 10 + index))

    bad_direct_path = journal_root / "pipeline-jobs/unrelated bad direct.json"
    bad_direct_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(FileOrchestrationJournalError):
        repository.query_reserved_unbound_jobs()
    assert not (journal_root / "reconcile-inventory-migration-v1.json").exists()

    bad_direct_path.rename(journal_root / "quarantine-unrelated-bad-direct.json")
    reserved = type(repository)(journal_root).query_reserved_unbound_jobs()

    assert [job.job_id for job in reserved] == ["job_reconcile_old_reserved_after_bad_direct"]


def test_pipeline_event_public_surfaces_redact_runtime_root_recovery_details(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    workspace_root = tmp_path / "runtime" / "workspace"
    object_store_root = tmp_path / "runtime" / "object-store"
    manifest_index_path = tmp_path / "runtime" / "manifest-index.json"
    object_store_prefix = "s3://nhms-prod/private-root"
    repository = FileOrchestrationJournalRepository(journal_root)
    record = _pipeline_reservation_record(cycle_time, job_id="job_public_event_redaction")
    repository.reserve_pipeline_job(record)

    inserted = repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_public_event_redaction",
        event_type="submission",
        status_from="reserved",
        status_to="submitted",
        details={
            "manifest_index_path": str(manifest_index_path),
            "runtime_root_contract": {
                "workspace_dir": str(workspace_root),
                "object_store_root": str(object_store_root),
                "object_store_prefix": object_store_prefix,
            },
            "slurm": {
                "manifest": {
                    "workspace_dir": str(workspace_root),
                    "object_store_root": str(object_store_root),
                    "object_store_prefix": object_store_prefix,
                }
            },
        },
    )
    state = _candidate_state(repository, cycle_time=cycle_time)
    raw_journal = (journal_root / "journal/gfs/2026062800.jsonl").read_text(encoding="utf-8")
    latest_rendered = "\n".join(path.read_text(encoding="utf-8") for path in (journal_root / "latest").rglob("*.json"))
    public_rendered = "\n".join(
        [
            raw_journal,
            latest_rendered,
            json.dumps(inserted, sort_keys=True),
            json.dumps(state, sort_keys=True),
        ]
    )
    private_rendered = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (journal_root / "private/runtime-root-recovery").rglob("*.json")
    )

    assert state is not None
    for raw in (str(workspace_root), str(object_store_root), str(manifest_index_path), object_store_prefix):
        assert raw not in public_rendered
    assert "[local-path]" in public_rendered
    assert "[object-uri]" in public_rendered
    assert str(workspace_root) in private_rendered
    assert str(object_store_root) in private_rendered
    assert object_store_prefix in private_rendered


def test_pipeline_event_private_runtime_root_recovery_omits_secret_bearing_values(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    workspace_root = tmp_path / "runtime" / "workspace"
    object_store_root = tmp_path / "runtime" / "object-store"
    repository = FileOrchestrationJournalRepository(journal_root)
    record = _pipeline_reservation_record(cycle_time, job_id="job_private_recovery_filter")
    repository.reserve_pipeline_job(record)

    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_private_recovery_filter",
        event_type="submission",
        status_from="reserved",
        status_to="submitted",
        details={
            "runtime_root_contract": {
                "workspace_dir": str(workspace_root),
                "object_store_root": str(object_store_root),
                "object_store_prefix": "s3://user:secret@nhms-prod/private-root?token=private-token",
                "published_artifact_uri_prefix": "s3://nhms-prod/published?X-Amz-Signature=signature-secret",
            }
        },
    )

    private_files = sorted((journal_root / "private/runtime-root-recovery").rglob("*.json"))
    assert private_files
    private_rendered = "\n".join(path.read_text(encoding="utf-8") for path in private_files)

    assert str(workspace_root) in private_rendered
    assert str(object_store_root) in private_rendered
    for raw in ("user:secret", "private-token", "signature-secret", "X-Amz-Signature"):
        assert raw not in private_rendered
    assert "object_store_prefix" not in private_rendered
    assert "published_artifact_uri_prefix" not in private_rendered


def test_pipeline_event_public_surfaces_redact_message_text(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    record = _pipeline_reservation_record(cycle_time, job_id="job_public_message_redaction")
    repository.reserve_pipeline_job(record)

    inserted = repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_public_message_redaction",
        event_type="submission",
        status_from="reserved",
        status_to="submitted",
        message=(
            "submission used /secret/workspace/job.sh and s3://nhms-prod/raw/gfs.grib "
            "manifest=/secret/manifest.json object=s3://nhms-prod/raw/assigned.grib "
            "token=raw-token-123 Authorization: Bearer live-token-123"
        ),
        details={"note": "safe"},
    )
    state = _candidate_state(repository, cycle_time=cycle_time)
    raw_journal = (journal_root / "journal/gfs/2026062800.jsonl").read_text(encoding="utf-8")
    latest_rendered = "\n".join(path.read_text(encoding="utf-8") for path in (journal_root / "latest").rglob("*.json"))
    public_rendered = "\n".join(
        [
            raw_journal,
            latest_rendered,
            json.dumps(inserted, sort_keys=True),
            json.dumps(state, sort_keys=True),
        ]
    )

    assert state is not None
    for raw in (
        "/secret/workspace",
        "/secret/manifest.json",
        "s3://nhms-prod/raw/gfs.grib",
        "s3://nhms-prod/raw/assigned.grib",
        "raw-token-123",
        "live-token-123",
    ):
        assert raw not in public_rendered
    assert "[local-path]" in public_rendered
    assert "[object-uri]" in public_rendered
    assert "[redacted]" in public_rendered


def test_pipeline_event_public_surfaces_redact_arbitrary_detail_strings(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    record = _pipeline_reservation_record(cycle_time, job_id="job_public_detail_redaction")
    repository.reserve_pipeline_job(record)

    inserted = repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_public_detail_redaction",
        event_type="submission",
        status_from="reserved",
        status_to="submitted",
        details={
            "reason": (
                "Authorization: Bearer detail-bearer token=detail-token "
                "/secret/detail/path s3://nhms-prod/private/detail.grib"
            )
        },
    )
    state = _candidate_state(repository, cycle_time=cycle_time)
    raw_journal = (journal_root / "journal/gfs/2026062800.jsonl").read_text(encoding="utf-8")
    latest_rendered = "\n".join(path.read_text(encoding="utf-8") for path in (journal_root / "latest").rglob("*.json"))
    public_rendered = "\n".join(
        [
            raw_journal,
            latest_rendered,
            json.dumps(inserted, sort_keys=True),
            json.dumps(state, sort_keys=True),
        ]
    )

    assert state is not None
    for raw in (
        "detail-bearer",
        "detail-token",
        "/secret/detail/path",
        "s3://nhms-prod/private/detail.grib",
    ):
        assert raw not in public_rendered
    assert inserted["details"]["reason"].count("[redacted]") >= 1
    assert "[local-path]" in public_rendered
    assert "[object-uri]" in public_rendered
    assert "[redacted]" in public_rendered


def test_forecast_cycle_pipeline_event_persists_replays_and_materializes_latest(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "fcst_gfs_2026062800_model_a",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "forcing": {"forcing_version_id": "forc_gfs_2026062800_model_a"},
        },
    )

    inserted = repository.insert_pipeline_event(
        entity_type="forecast_cycle",
        entity_id=cycle_id_for("gfs", cycle_time),
        event_type="canonical_converter_version_stale",
        status_from="canonical_ready",
        status_to="canonical_stale",
        message="canonical demotion read /secret/canonical.json from s3://nhms/raw/canonical.json token=stale-token",
        details={"cycle_id": cycle_id_for("gfs", cycle_time), "manifest_uri": "s3://nhms/raw/canonical.json"},
    )
    state = _candidate_state(repository, cycle_time=cycle_time)
    latest = json.loads((journal_root / "latest/gfs/2026062800/model_a.json").read_text(encoding="utf-8"))
    raw_journal = (journal_root / "journal/gfs/2026062800.jsonl").read_text(encoding="utf-8")
    public_rendered = json.dumps({"inserted": inserted, "state": state, "latest": latest}, sort_keys=True)

    assert inserted["entity_type"] == "forecast_cycle"
    assert inserted["entity_id"] == cycle_id_for("gfs", cycle_time)
    assert repository.get_pipeline_job(cycle_id_for("gfs", cycle_time)) is None
    assert state is not None
    assert state["pipeline_jobs_total"] == 0
    assert state["pipeline_events"][0]["entity_type"] == "forecast_cycle"
    assert latest["pipeline_events"][0]["entity_type"] == "forecast_cycle"
    for raw in ("/secret/canonical.json", "s3://nhms/raw/canonical.json", "stale-token"):
        assert raw not in raw_journal
        assert raw not in public_rendered
    assert "[local-path]" in public_rendered
    assert "[object-uri]" in public_rendered
    assert "[redacted]" in public_rendered


def test_file_orchestration_journal_reservation_append_failure_leaves_no_direct_only_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    record = _pipeline_reservation_record(cycle_time, job_id="job_append_fails")

    def fail_append(*_args: Any, **_kwargs: Any) -> None:
        raise OrchestratorError("FILE_JOURNAL_WRITE_FAILED", "forced append failure")

    monkeypatch.setattr(repository, "_append_journal_record_unlocked", fail_append)

    with pytest.raises(OrchestratorError):
        repository.reserve_pipeline_job(record)

    assert not (journal_root / "pipeline-jobs/job_append_fails.json").exists()
    assert FileOrchestrationJournalRepository(journal_root).reserve_pipeline_job(record) is not None


def test_file_orchestration_journal_two_repositories_allocate_unique_event_ids(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    record = _pipeline_reservation_record(cycle_time, job_id="job_events")
    assert repository.reserve_pipeline_job(record) is not None

    first = FileOrchestrationJournalRepository(journal_root).insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_events",
        event_type="status_change",
        status_from="reserved",
        status_to="submitted",
    )
    second = FileOrchestrationJournalRepository(journal_root).insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_events",
        event_type="status_change",
        status_from="submitted",
        status_to="running",
    )
    state = _candidate_state(repository, cycle_time=cycle_time)

    assert first["event_id"] != second["event_id"]
    assert state is not None
    assert state["pipeline_events_total"] == 2


def test_file_orchestration_journal_overlapping_repositories_serialize_cycle_writes(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    context = get_context("spawn")
    ready_queue = context.Queue()
    result_queue = context.Queue()
    release_event = context.Event()
    first = context.Process(
        target=_reserve_pipeline_job_process,
        args=(
            str(journal_root),
            cycle_time.isoformat(),
            "job_overlap_first",
            "gfs:gfs_2026062800:basin_a:overlap_first",
            True,
            ready_queue,
            release_event,
            result_queue,
        ),
    )
    second = context.Process(
        target=_reserve_pipeline_job_process,
        args=(
            str(journal_root),
            cycle_time.isoformat(),
            "job_overlap_second",
            "gfs:gfs_2026062800:basin_a:overlap_second",
            False,
            ready_queue,
            release_event,
            result_queue,
        ),
    )

    first.start()
    try:
        assert ready_queue.get(timeout=10) == {"status": "holding", "job_id": "job_overlap_first"}
        second.start()
        release_event.set()
        first.join(10)
        second.join(10)
        assert not first.is_alive()
        assert not second.is_alive()
        outcomes = [result_queue.get(timeout=5), result_queue.get(timeout=5)]
    finally:
        release_event.set()
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(5)

    assert all(outcome["ok"] for outcome in outcomes)
    assert {outcome["job_id"] for outcome in outcomes} == {"job_overlap_first", "job_overlap_second"}
    repository = FileOrchestrationJournalRepository(journal_root)
    jobs = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
    state = _candidate_state(repository, cycle_time=cycle_time)
    records = [
        json.loads(line)
        for line in (journal_root / "journal/gfs/2026062800.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    latest = json.loads((journal_root / "latest/gfs/2026062800/model_a.json").read_text(encoding="utf-8"))

    assert {job["job_id"] for job in jobs} == {"job_overlap_first", "job_overlap_second"}
    assert state is not None
    assert state["pipeline_jobs_total"] == 2
    assert latest["replay"]["job_count"] == 2
    assert len({record["sequence"] for record in records}) == 2
    assert {record["payload"]["job_id"] for record in records} == {"job_overlap_first", "job_overlap_second"}


def test_file_orchestration_journal_reclaims_dead_reservation_and_keeps_permanent_failure_sticky(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    dead_record = _pipeline_reservation_record(cycle_time, job_id="job_dead")
    live_record = _pipeline_reservation_record(cycle_time, job_id="job_live")
    live_record["idempotency_key"] = "gfs:gfs_2026062800:basin_a:forecast_live"

    assert repository.reserve_pipeline_job(dead_record) is not None
    repository.update_pipeline_job_status("job_dead", "submission_failed", error_code="SBATCH_REJECTED")
    reclaimed = repository.reclaim_pipeline_job_reservation(dead_record)

    assert reclaimed is not None
    assert reclaimed["status"] == "reserved"
    assert reclaimed["slurm_job_id"] is None

    assert repository.reserve_pipeline_job(live_record) is not None
    repository.bind_pipeline_job_reservation(live_record["idempotency_key"], slurm_job_id="3002")
    assert repository.reclaim_pipeline_job_reservation(live_record) is None

    repository.update_pipeline_job_status("job_dead", "permanently_failed", error_code="RETRY_LIMIT_EXHAUSTED")
    previous, sticky = repository.update_pipeline_job_status("job_dead", "succeeded")

    assert previous == "permanently_failed"
    assert sticky["status"] == "permanently_failed"
    assert sticky["error_code"] == "RETRY_LIMIT_EXHAUSTED"


def test_file_journal_retry_service_schedules_auto_retry_and_records_event(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_forecast")
    repository.reserve_pipeline_job(record)
    repository.bind_pipeline_job_reservation(record["idempotency_key"], slurm_job_id="3001")
    repository.update_pipeline_job_status(
        "job_forecast",
        "failed",
        error_code="SLURM_TIMEOUT",
        error_message="Timed out while polling Slurm.",
        finished_at=cycle_time,
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    handled = service.handle_failed_job(repository.get_pipeline_job("job_forecast"))
    state = _candidate_state(repository, cycle_time=cycle_time)

    assert handled.job_id == "job_forecast_retry_1"
    assert handled.status == "pending"
    assert handled.retry_count == 1
    assert state is not None
    assert {job["job_id"]: job["status"] for job in state["pipeline_jobs"]}["job_forecast_retry_1"] == "pending"
    retry_event = next(event for event in state["pipeline_events"] if event["entity_id"] == "job_forecast_retry_1")
    assert retry_event["event_type"] == "retry"
    assert retry_event["details"]["trigger"] == "auto"
    assert retry_event["details"]["previous_job_id"] == "job_forecast"
    assert retry_event["details"]["failure"]["retryable"] is True


def test_file_journal_auto_retry_persists_retry_count_on_cycle_scope_rows(tmp_path: Path) -> None:
    """Cycle-scope (model-less) rows are NOT master rows: their retry_count is durable.

    The clean-reservation invariant resets ``retry_count`` only on forecast-cohort
    master rows, so any consumer that treats a cycle-wide job's recorded count as
    "always 0" is reading a premise the journal never guarantees.
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    cycle_run_id = f"cycle_gfs_{format_cycle_time(cycle_time)}"
    record = {
        "job_id": f"job_{cycle_run_id}_download",
        "run_id": cycle_run_id,
        "cycle_id": cycle_id_for("gfs", cycle_time),
        "source_id": "gfs",
        "job_type": "download_source_cycle",
        "model_id": None,
        "status": "reserved",
        "stage": "download",
        "idempotency_key": f"gfs:{cycle_id_for('gfs', cycle_time)}:download",
    }
    repository.reserve_pipeline_job(record)
    repository.bind_pipeline_job_reservation(record["idempotency_key"], slurm_job_id="9001")
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    job_id = record["job_id"]
    retry_counts = []
    for _ in range(3):
        repository.update_pipeline_job_status(
            job_id,
            "failed",
            error_code="SLURM_TIMEOUT",
            finished_at=cycle_time,
        )
        handled = service.handle_failed_job(repository.get_pipeline_job(job_id))
        job_id = handled.job_id
        retry_counts.append(repository.get_pipeline_job(job_id)["retry_count"])

    assert retry_counts == [1, 2, 3]
    assert repository.get_pipeline_job(job_id)["model_id"] is None
    # Every candidate of the cycle sees the model-less row -- and its durable
    # non-zero count -- in the unfiltered ``pipeline_jobs`` projection.
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    cycle_scope_counts = {
        job["job_id"]: job["retry_count"] for job in state["pipeline_jobs"] if job["model_id"] is None
    }
    assert max(cycle_scope_counts.values()) == 3


def test_file_journal_manual_retry_manifest_uses_source_cycle_fields_for_convert(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    workspace_root = tmp_path / "workspace"
    object_store_root = tmp_path / "object-store"
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_cycle_ifs_2026062800_convert_convert")
    record.update(
        {
            "run_id": "cycle_ifs_2026062800_convert_basins_qhh_shud",
            "cycle_id": cycle_id_for("IFS", cycle_time),
            "source_id": "IFS",
            "job_type": "convert_canonical",
            "stage": "convert",
            "idempotency_key": "IFS:ifs_2026062800:convert",
        }
    )
    repository.reserve_pipeline_job(record)
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=record["job_id"],
        event_type="submission",
        status_from="reserved",
        status_to="submitted",
        details={
            "runtime_root_contract": {
                "workspace_dir": str(workspace_root),
                "object_store_root": str(object_store_root),
                "object_store_prefix": "s3://nhms-prod",
            }
        },
    )
    repository.update_pipeline_job_status(
        record["job_id"],
        "permanently_failed",
        error_code="SLURM_JOB_FAILED",
        finished_at=cycle_time,
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7007", "status": "submitted"}

    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried = service.attempt_manual_retry(
        "cycle_ifs_2026062800_convert_basins_qhh_shud",
        gateway,
        trusted_internal=True,
    )

    assert retried.status == "submitted"
    assert gateway.requests[0].manifest["cycle_id"] == "ifs_2026062800"
    assert gateway.requests[0].manifest["source_id"] == "IFS"
    assert gateway.requests[0].manifest["cycle_time"] == "2026062800"
    assert gateway.requests[0].manifest["workspace_dir"] == str(workspace_root)
    assert gateway.requests[0].manifest["object_store_root"] == str(object_store_root)
    assert gateway.requests[0].manifest["object_store_prefix"] == "s3://nhms-prod"


def test_file_journal_manual_retry_uses_array_endpoint_for_array_job_types(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    workspace_root = tmp_path / "workspace"
    object_store_root = tmp_path / "object-store"
    run_id = "cycle_gfs_2026062800_forcing_basins_qhh_shud"
    tasks = [
        {
            "task_id": 0,
            "run_id": "fcst_gfs_2026062800_basins_qhh_shud",
            "model_id": "basins_qhh_shud",
            "cycle_id": "gfs_2026062800",
            "cycle_time": "2026062800",
        }
    ]
    index_path = workspace_root / "runs" / run_id / "input" / "forcing_manifest_index.json"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(json.dumps(tasks), encoding="utf-8")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_cycle_gfs_2026062800_forcing_forcing")
    record.update(
        {
            "run_id": run_id,
            "cycle_id": cycle_id_for("gfs", cycle_time),
            "source_id": "gfs",
            "job_type": "produce_forcing_array",
            "stage": "forcing",
            "model_id": "basins_qhh_shud",
            "idempotency_key": "gfs:gfs_2026062800:forcing",
        }
    )
    repository.reserve_pipeline_job(record)
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=record["job_id"],
        event_type="submission",
        status_from="reserved",
        status_to="submitted",
        details={
            "runtime_root_contract": {
                "workspace_dir": str(workspace_root),
                "object_store_root": str(object_store_root),
                "object_store_prefix": "s3://nhms-prod",
            }
        },
    )
    repository.update_pipeline_job_status(
        record["job_id"],
        "permanently_failed",
        error_code="SLURM_JOB_FAILED",
        finished_at=cycle_time,
    )

    class Gateway:
        def __init__(self) -> None:
            self.single_requests: list[Any] = []
            self.array_requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.single_requests.append(request)
            raise AssertionError("array job must not use the single-job endpoint")

        def submit_job_array(self, request: Any) -> dict[str, Any]:
            self.array_requests.append(request)
            return {"job_id": "7008", "status": "submitted"}

    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried = service.attempt_manual_retry(
        "cycle_gfs_2026062800_forcing_basins_qhh_shud",
        gateway,
        trusted_internal=True,
    )

    assert retried.status == "submitted"
    assert gateway.single_requests == []
    assert gateway.array_requests
    assert gateway.array_requests[0].resolved_job_type() == "produce_forcing_array"
    assert gateway.array_requests[0].manifest["tasks"] == tasks


def test_file_journal_manual_retry_preserves_db_free_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    monkeypatch.setenv("DATABASE_URL", "postgresql://writer:secret@db.example/nhms")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_forecast_db_free_failed")
    repository.reserve_pipeline_job(record)
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=record["job_id"],
        event_type="submission",
        status_from="reserved",
        status_to="submitted",
        details={
            "runtime_root_contract": {
                "workspace_dir": "/srv/nhms/workspace",
                "object_store_root": "/srv/nhms/object-store",
                "object_store_prefix": "s3://nhms-prod",
                "scheduler_db_free_required": "true",
                "scheduler_allowed_roots": "/srv/nhms/workspace:/srv/nhms/object-store",
                "scheduler_registry_backend": "file",
                "scheduler_registry_manifest": "/srv/nhms/object-store/scheduler/registry/manifest-last.json",
                "scheduler_canonical_readiness_backend": "file",
                "scheduler_canonical_readiness_index": (
                    "/srv/nhms/object-store/scheduler/canonical-readiness/index-last.json"
                ),
                "scheduler_state_index_backend": "file",
                "scheduler_state_index": "/srv/nhms/object-store/scheduler/state-index/index-last.json",
            }
        },
    )
    repository.update_pipeline_job_status(
        record["job_id"],
        "permanently_failed",
        error_code="NODE_FAILURE",
        finished_at=cycle_time,
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7010", "status": "submitted"}

    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried = service.attempt_manual_retry(record["run_id"], gateway, trusted_internal=True)

    manifest = gateway.requests[0].manifest
    assert retried.status == "submitted"
    assert manifest["scheduler_db_free_required"] == "true"
    assert manifest["scheduler_registry_backend"] == "file"
    assert manifest["scheduler_registry_manifest"] == "/srv/nhms/object-store/scheduler/registry/manifest-last.json"
    assert manifest["scheduler_canonical_readiness_backend"] == "file"
    assert manifest["scheduler_canonical_readiness_index"] == (
        "/srv/nhms/object-store/scheduler/canonical-readiness/index-last.json"
    )
    assert manifest["scheduler_state_index_backend"] == "file"
    assert manifest["scheduler_state_index"] == "/srv/nhms/object-store/scheduler/state-index/index-last.json"
    assert manifest["slurm_env"] == {"NHMS_SHUD_DB_FREE": "true"}
    assert manifest["previous_job_id"] == "job_forecast_db_free_failed"
    assert manifest["pipeline_job_id"] == retried.job_id
    assert manifest["manual_retry_marker"] is True
    assert "DATABASE_URL" not in json.dumps(manifest)
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    retry_event = next(event for event in state["pipeline_events"] if event["event_type"] == "retry")
    submission_event = next(
        event
        for event in state["pipeline_events"]
        if event["entity_id"] == retried.job_id and event["status_to"] == "submitted"
    )
    assert retry_event["details"]["manual_retry_marker"] is True
    assert retry_event["details"]["previous_job_id"] == "job_forecast_db_free_failed"
    assert submission_event["details"]["runtime_root_resolution"]["db_free_runtime"]["required"] is True
    assert submission_event["details"]["runtime_root_contract"]["scheduler_db_free_required"] == "true"


def test_file_journal_retry_rejects_db_free_selectors_outside_allowed_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    monkeypatch.setenv("WORKSPACE_ROOT", "/env/nhms/workspace")
    monkeypatch.setenv("OBJECT_STORE_ROOT", "/env/nhms/object-store")
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://env-nhms-prod")
    monkeypatch.setenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_ALLOWED_ROOTS", "/env/nhms/workspace:/env/nhms/object-store")
    monkeypatch.setenv("NHMS_SCHEDULER_REGISTRY_BACKEND", "file")
    monkeypatch.setenv(
        "NHMS_SCHEDULER_REGISTRY_MANIFEST",
        "/env/nhms/object-store/scheduler/registry/manifest-last.json",
    )
    monkeypatch.setenv("NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND", "file")
    monkeypatch.setenv(
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX",
        "/env/nhms/object-store/scheduler/canonical-readiness/index-last.json",
    )
    monkeypatch.setenv("NHMS_SCHEDULER_STATE_INDEX_BACKEND", "file")
    monkeypatch.setenv(
        "NHMS_SCHEDULER_STATE_INDEX",
        "/env/nhms/object-store/scheduler/state-index/index-last.json",
    )
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_forecast_db_free_failed")
    repository.reserve_pipeline_job(record)
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=record["job_id"],
        event_type="submission",
        status_from="reserved",
        status_to="submitted",
        details={
            "runtime_root_contract": {
                "workspace_dir": "/srv/nhms/workspace",
                "object_store_root": "/srv/nhms/object-store",
                "object_store_prefix": "s3://nhms-prod",
                "scheduler_db_free_required": "true",
                "scheduler_allowed_roots": "/srv/nhms/workspace:/srv/nhms/object-store",
                "scheduler_registry_backend": "file",
                "scheduler_registry_manifest": "/tmp/evil-registry.json",
                "scheduler_canonical_readiness_backend": "file",
                "scheduler_canonical_readiness_index": "/tmp/evil-readiness.json",
                "scheduler_state_index_backend": "file",
                "scheduler_state_index": "/tmp/evil-state.json",
            }
        },
    )
    repository.update_pipeline_job_status(
        record["job_id"],
        "permanently_failed",
        error_code="NODE_FAILURE",
        finished_at=cycle_time,
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7011", "status": "submitted"}

    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried = service.attempt_manual_retry(record["run_id"], gateway, trusted_internal=True)

    manifest = gateway.requests[0].manifest
    assert retried.status == "submitted"
    assert manifest["scheduler_registry_manifest"] == "/env/nhms/object-store/scheduler/registry/manifest-last.json"
    assert manifest["scheduler_canonical_readiness_index"] == (
        "/env/nhms/object-store/scheduler/canonical-readiness/index-last.json"
    )
    assert manifest["scheduler_state_index"] == "/env/nhms/object-store/scheduler/state-index/index-last.json"
    assert "/tmp/evil" not in json.dumps(manifest)
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    submission_event = next(
        event
        for event in state["pipeline_events"]
        if event["entity_id"] == retried.job_id and event["status_to"] == "submitted"
    )
    rejected = submission_event["details"]["runtime_root_resolution"]["rejected"]
    assert any(
        item["field"] == "scheduler_registry_manifest"
        and item["reason"] == "db_free_selector_path_outside_allowed_roots"
        for item in rejected
    )


def test_file_journal_retry_service_reuses_submission_failed_retry_and_clears_stale_fields(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_forecast")
    record["retry_count"] = 1
    repository.reserve_pipeline_job(record)
    repository.bind_pipeline_job_reservation(record["idempotency_key"], slurm_job_id="3001")
    repository.update_pipeline_job_status(
        "job_forecast",
        "failed",
        error_code="NODE_FAILURE",
        error_message="node failed",
        finished_at=cycle_time,
    )
    stale_retry = _pipeline_reservation_record(cycle_time, job_id="job_forecast_retry_2", status="submission_failed")
    stale_retry.update(
        {
            "run_id": record["run_id"],
            "idempotency_key": "gfs:gfs_2026062800:basin_a:forecast_retry_2",
            "candidate_id": "candidate_stale_retry",
            "slurm_job_id": None,
            "array_task_id": None,
            "submitted_at": "2026-06-28T00:03:00Z",
            "started_at": "2026-06-28T00:04:00Z",
            "finished_at": "2026-06-28T00:05:00Z",
            "exit_code": 1,
            "retry_count": 2,
            "error_code": "SUBMIT_INTERRUPTED",
            "error_message": "submission interrupted",
            "log_uri": "s3://logs/stale",
        }
    )
    repository.upsert_pipeline_job(stale_retry)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retry = service.schedule_auto_retry(repository.get_pipeline_job("job_forecast"))
    persisted = repository.get_pipeline_job("job_forecast_retry_2")
    state = _candidate_state(repository, cycle_time=cycle_time)
    direct_record = json.loads(
        (tmp_path / "journal/pipeline-jobs/job_forecast_retry_2.json").read_text(encoding="utf-8")
    )["payload"]

    assert retry.job_id == "job_forecast_retry_2"
    assert retry.status == "pending"
    assert persisted is not None
    for row in (vars(retry), persisted, direct_record):
        assert row["slurm_job_id"] is None
        assert row["array_task_id"] is None
        assert row["submitted_at"] is None
        assert row["started_at"] is None
        assert row["finished_at"] is None
        assert row["exit_code"] is None
        assert row["idempotency_key"] is None
        assert row["candidate_id"] is None
        assert row["error_code"] is None
        assert row["error_message"] is None
        assert row["log_uri"] is None
    assert state is not None
    retry_event = next(event for event in state["pipeline_events"] if event["entity_id"] == "job_forecast_retry_2")
    assert retry_event["details"]["reused_existing_retry_job"] is True
    assert retry_event["details"]["previous_job_id"] == "job_forecast"


def test_file_journal_retry_service_exhaustion_records_permanent_failure(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_exhausted")
    record["retry_count"] = 3
    repository.reserve_pipeline_job(record)
    repository.update_pipeline_job_status(
        "job_exhausted",
        "failed",
        error_code="SLURM_TIMEOUT",
        error_message="Timed out after final retry.",
        finished_at=cycle_time,
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    handled = service.handle_failed_job(repository.get_pipeline_job("job_exhausted"))
    state = _candidate_state(repository, cycle_time=cycle_time)

    assert handled.job_id == "job_exhausted"
    assert handled.status == "permanently_failed"
    assert handled.error_code == "SLURM_TIMEOUT"
    assert state is not None
    assert state["pipeline_status"] == "permanently_failed"
    assert state["retry_count"] == 3
    assert state["error_code"] == "SLURM_TIMEOUT"
    permanent_event = next(event for event in state["pipeline_events"] if event["event_type"] == "permanently_failed")
    assert permanent_event["status_to"] == "permanently_failed"
    assert permanent_event["details"]["failure"]["limit_exhausted"] is True


def test_file_journal_manual_repair_marker_allows_candidate_and_preserves_prior_reason(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_manual_repair")
    record["retry_count"] = 3
    repository.reserve_pipeline_job(record)
    repository.update_pipeline_job_status(
        "job_manual_repair",
        "permanently_failed",
        error_code="INVALID_MANIFEST",
        error_message="Operator repaired malformed manifest.",
        finished_at=cycle_time,
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    repair = service.record_manual_repair(
        "fcst_gfs_2026062800_model_a",
        requested_by="operator",
        request_id="manual-1",
        reason="manifest repaired",
        trusted_internal=True,
    )
    state = _candidate_state(repository, cycle_time=cycle_time)

    assert repair.job_id == "job_manual_repair"
    assert repair.status == "manual_repair_requested"
    assert repair.manual_retry_marker is True
    assert repair.retry_count == 4
    assert state is not None
    assert state["pipeline_status"] == "permanently_failed"
    assert scheduler_module._manual_retry_requested(state) is True
    manual_payload = scheduler_module._manual_retry_payload(state)
    assert manual_payload["marker"] is True
    assert manual_payload["new_attempt"] == 4
    assert manual_payload["prior_failure_reason"] == "INVALID_MANIFEST"
    event = next(event for event in state["pipeline_events"] if event["entity_id"] == "job_manual_repair")
    assert event["details"]["manual_retry_marker"] is True
    assert event["details"]["requested_by"] == "operator"
    assert event["details"]["request_id"] == "manual-1"
    assert event["details"]["policy_decision"]["decision"] == "allow"


def test_file_journal_manual_repair_marker_event_records_the_failed_stage(tmp_path: Path) -> None:
    """#1292 D1 writer half: the marker must record WHAT IT REPAIRS, not just which job.

    ``_unresolvable_marker_entity_pins_attempt`` decides the operator's pinned attempt off this
    field whenever the target row is gone from the candidate state (identity-filter deletion or
    row-window truncation); without it the pin gate is back to id-text forensics.  The value is
    the failed job's stage -- the same one the returned namespace already exposes as ``stage``.
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_manual_repair_stage")
    record["retry_count"] = 3
    repository.reserve_pipeline_job(record)
    repository.update_pipeline_job_status(
        "job_manual_repair_stage",
        "permanently_failed",
        error_code="INVALID_MANIFEST",
        finished_at=cycle_time,
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    repair = service.record_manual_repair("fcst_gfs_2026062800_model_a", trusted_internal=True)
    state = _candidate_state(repository, cycle_time=cycle_time)

    assert repair.stage == "forecast"
    assert state is not None
    event = next(event for event in state["pipeline_events"] if event["entity_id"] == "job_manual_repair_stage")
    assert event["details"]["failed_stage"] == "forecast"
    assert event["details"]["failed_stage"] == repair.stage
    # The key name is load-bearing: ``details.stage`` is read by the candidate-state
    # record-stage reader, ``details.failed_stage`` is read by no record-stage consumer.
    assert "stage" not in event["details"]


_MARKER_RECORD_CYCLE_TIME = _dt("2026-05-21T06:00:00Z")


def _cohort_marker_journal(
    journal_root: Path,
    *,
    stage: str = "download",
    job_id: str | None = None,
    status: str = "failed",
    retry_count: int = 4,
) -> FileOrchestrationJournalRepository:
    """One cycle's journal: a cohort master at ``stage``, the candidate's own row, one marker.

    The cohort master is model-less on the cycle run id -- the row a manual repair of a cycle
    stage targets, and the row both row-absence mechanisms take away from the decision state.
    Its timestamps are older than the candidate's own row on purpose, so a job window of one
    truncates exactly it while the event window keeps its marker.  The marker is written by the
    real ``record_manual_repair``, so its ``details`` are the production write face, not a
    hand-built approximation of it.
    """

    repository = FileOrchestrationJournalRepository(journal_root)
    cycle_stamp = format_cycle_time(_MARKER_RECORD_CYCLE_TIME)
    repository.upsert_pipeline_job(
        {
            "job_id": job_id or f"job_cycle_gfs_{cycle_stamp}_{stage}",
            "run_id": f"cycle_gfs_{cycle_stamp}",
            "cycle_id": cycle_id_for("gfs", _MARKER_RECORD_CYCLE_TIME),
            "job_type": "download_source_cycle",
            "model_id": None,
            "stage": stage,
            "status": status,
            "error_code": "SLURM_TIMEOUT",
            "retry_count": retry_count,
            "idempotency_key": f"gfs:gfs_{cycle_stamp}:cohort:{stage}",
            "created_at": "2026-05-21T06:00:00Z",
            "updated_at": "2026-05-21T06:05:00Z",
            "finished_at": "2026-05-21T06:05:00Z",
        }
    )
    repository.upsert_pipeline_job(
        {
            "job_id": f"job_fcst_gfs_{cycle_stamp}_model_a_forecast",
            "run_id": f"fcst_gfs_{cycle_stamp}_model_a",
            "cycle_id": cycle_id_for("gfs", _MARKER_RECORD_CYCLE_TIME),
            "job_type": "run_shud_forecast_array",
            "model_id": "model_a",
            "candidate_id": "gfs:2026-05-21T06:00:00Z:model_a:forecast_gfs_deterministic",
            "stage": "forecast",
            "status": "pending",
            "retry_count": 0,
            "idempotency_key": f"gfs:gfs_{cycle_stamp}:basin_a:forecast",
            "created_at": "2026-05-21T07:00:00Z",
            "updated_at": "2026-05-21T07:10:00Z",
        }
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    service.record_manual_repair(f"cycle_gfs_{cycle_stamp}", trusted_internal=True)
    return repository


@pytest.mark.parametrize(
    ("job_id", "status", "retry_count", "expected_pin", "expected_attempt"),
    [
        (None, "failed", 4, True, 5),
        ("job_cycle_gfs_2026052106_download_retry_2", "submission_failed", 2, False, 1),
    ],
    ids=["live_failure_record_pins", "unsubmitted_placeholder_record_refuses"],
)
def test_file_journal_manual_repair_target_record_decides_the_pin_end_to_end(
    tmp_path: Path,
    job_id: str | None,
    status: str,
    retry_count: int,
    expected_pin: bool,
    expected_attempt: int,
) -> None:
    """Write face to decision state, with no hand-built marker anywhere in between.

    ``record_manual_repair`` writes the target's shape, the projection truncates the target row
    out of the job window, the identity filter sanitizes the surviving event, and the pin gate
    rebuilds the row from what is left.  Both cells share every input but the target row's own
    shape: a live failure pins the operator's ``retry_count``, and a row that is really an
    unsubmitted auto-retry placeholder (``submission_failed``, a ``_retry_<n>`` id, a positive
    retry count, no Slurm binding) refuses it exactly as the row-present twin refuses that row --
    the shape whose evidence used to die with the row.

    The placeholder cell is a genuine write-face shape, not a constructed one:
    ``submission_failed`` is in ``MANUAL_RETRY_SOURCE_STATUSES``, so the manual repair really
    does select such a row as its target.
    """

    repository = _cohort_marker_journal(
        tmp_path / "journal",
        job_id=job_id,
        status=status,
        retry_count=retry_count,
    )
    target_job_id = job_id or "job_cycle_gfs_2026052106_download"
    state = _candidate_state(repository, cycle_time=_MARKER_RECORD_CYCLE_TIME, job_limit=1)
    assert state is not None
    candidate = _scheduler_candidate_fixture()
    evidence = scheduler_module._candidate_state_evidence(candidate, state)
    decision_state = scheduler_module._candidate_state_decision_state(state, evidence)
    marker_event = decision_state["pipeline_events"][0]
    details = marker_event["details"]

    # Premise: the target row really was truncated away, its marker really survived, and the
    # record really came through the sanitizer intact.
    assert target_job_id not in [job["job_id"] for job in decision_state["pipeline_jobs"]]
    assert marker_event["entity_id"] == target_job_id
    assert details["target_status"] == status
    assert details["target_retry_count"] == retry_count
    # ``False`` is a recorded value, not an absence: the writer writes it and the sanitizer
    # passes it through, which is what keeps a marker-flagged row out of the placeholder gate.
    assert details["target_manual_retry_marker"] is False
    # The pre-existing whitelist keys are untouched by the new ones.
    assert details["failed_stage"] == "download"
    assert details["retry_count"] == retry_count + 1
    assert details["previous_job_id"] == target_job_id
    assert details["trigger"] == "manual"
    assert details["manual_retry_marker"] is True
    assert details["prior_failure_reason"] == "SLURM_TIMEOUT"

    assert (
        scheduler_state_manual_retry_module._marker_event_pins_attempt(decision_state, marker_event)
        is expected_pin
    )
    assert scheduler_module._manual_retry_new_attempt(decision_state, previous_attempt=0) == expected_attempt


@pytest.mark.parametrize(
    ("stage", "keeps_details"),
    [("parse", False), ("state_save_qc", False), ("publish", False), ("download", True)],
)
def test_file_journal_completion_stage_compaction_unadopts_the_cycle_marker(
    tmp_path: Path,
    stage: str,
    keeps_details: bool,
) -> None:
    """The pin gate's journal-path live domain is the SUBMISSION stages, and this is why.

    ``_compact_cycle_scope_event`` drops ``details`` wholesale from a model-less cycle-scope
    event at a completion stage (``_CYCLE_SCOPE_COMPLETION_STAGES``: parse / state_save_qc /
    publish), as an evidence-size guard.  A marker event without ``details`` fails
    ``_manual_retry_marker_shape``, so it is never ADOPTED at all -- it does not fall back to
    the id-token backstop, it stops being a marker.  Everything the marker recorded, stage
    evidence and target record alike, goes with it.  The submission-stage control proves the
    compaction is what does it rather than the fixture.
    """

    repository = _cohort_marker_journal(tmp_path / "journal", stage=stage)
    state = _candidate_state(repository, cycle_time=_MARKER_RECORD_CYCLE_TIME)
    assert state is not None
    marker_event = next(
        event for event in state["pipeline_events"] if event["entity_id"].startswith("job_cycle_")
    )

    assert stage in journal_module._CYCLE_SCOPE_COMPLETION_STAGES or keeps_details
    assert bool(marker_event.get("details")) is keeps_details
    assert (
        scheduler_state_manual_retry_module._manual_retry_marker_shape(marker_event) is keeps_details
    )
    assert (
        scheduler_state_manual_retry_module._event_is_adopted_manual_retry_marker(state, marker_event)
        is keeps_details
    )
    if keeps_details:
        assert marker_event["details"]["failed_stage"] == stage
        assert marker_event["details"]["target_status"] == "failed"


def test_file_journal_manual_repair_marker_event_survives_terminal_stage_gating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1292 D1 consumer non-drop anchor: the field name must stay invisible to stage gating.

    ``chain_repository_state._normalized_record_stage`` reads ``details.stage`` off EVENT
    records too, and under the production terminal-stage setting a legacy-downstream stage makes
    the record disappear from the candidate state.  Naming the new field ``stage`` would
    therefore delete the operator's own marker event -- the retry would stop reporting
    ``manual_retry_requested`` at all -- for exactly the targets whose stage is being recorded.
    """

    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", "forecast_state_save_qc")
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    # The candidate's forecast leg succeeded first, so the candidate state still has a row of
    # its own once the legacy-downstream publish row is filtered out of it.
    upstream = _pipeline_reservation_record(cycle_time, job_id="job_manual_repair_forecast")
    repository.reserve_pipeline_job(upstream)
    repository.update_pipeline_job_status(
        "job_manual_repair_forecast",
        "succeeded",
        finished_at=_dt("2026-06-28T00:02:00Z"),
    )
    record = _pipeline_reservation_record(cycle_time, job_id="job_manual_repair_publish")
    record["stage"] = "publish"
    record["job_type"] = "publish_tiles"
    record["idempotency_key"] = "gfs:gfs_2026062800:basin_a:publish"
    record["retry_count"] = 3
    repository.reserve_pipeline_job(record)
    repository.update_pipeline_job_status(
        "job_manual_repair_publish",
        "permanently_failed",
        error_code="INVALID_MANIFEST",
        finished_at=_dt("2026-06-28T00:05:00Z"),
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    repair = service.record_manual_repair("fcst_gfs_2026062800_model_a", trusted_internal=True)
    state = _candidate_state(repository, cycle_time=cycle_time)

    assert repair.stage == "publish"
    assert state is not None
    event = next(event for event in state["pipeline_events"] if event["entity_id"] == "job_manual_repair_publish")
    assert event["details"]["failed_stage"] == "publish"
    assert scheduler_module._manual_retry_requested(state) is True
    # Control: the same event with the value under the ``stage`` key IS dropped by the gate,
    # which is the drop this key name avoids.
    assert chain_repository_state_module._record_allowed_for_compute_state_terminal(event) is True
    assert (
        chain_repository_state_module._record_allowed_for_compute_state_terminal(
            {**event, "details": {**event["details"], "stage": event["details"]["failed_stage"]}}
        )
        is False
    )


def test_file_journal_manual_repair_requires_policy_and_leaves_journal_unchanged(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_manual_repair_denied")
    repository.reserve_pipeline_job(record)
    repository.update_pipeline_job_status(
        "job_manual_repair_denied",
        "permanently_failed",
        error_code="INVALID_MANIFEST",
        finished_at=cycle_time,
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    with pytest.raises(RetryError, match="Authentication is required"):
        service.record_manual_repair("fcst_gfs_2026062800_model_a", requested_by="operator")

    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert scheduler_module._manual_retry_requested(state) is False
    assert state["pipeline_events_total"] == 0


def test_file_journal_manual_retry_refuses_old_failure_after_later_success(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    failed = _pipeline_reservation_record(cycle_time, job_id="job_old_failed")
    retry_success = _pipeline_reservation_record(cycle_time, job_id="job_retry_success")
    retry_success["idempotency_key"] = "gfs:gfs_2026062800:basin_a:forecast_retry_success"
    retry_success["retry_count"] = 1
    repository.reserve_pipeline_job(failed)
    repository.update_pipeline_job_status(
        "job_old_failed",
        "failed",
        error_code="SLURM_TIMEOUT",
        finished_at=_dt("2026-06-28T00:20:00Z"),
    )
    repository.reserve_pipeline_job(retry_success)
    repository.update_pipeline_job_status(
        "job_retry_success",
        "succeeded",
        finished_at=_dt("2026-06-28T00:10:00Z"),
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    with pytest.raises(RetryNotFoundError):
        service.record_manual_repair("fcst_gfs_2026062800_model_a", trusted_internal=True)

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7003", "status": "submitted"}

    gateway = Gateway()

    with pytest.raises(RetryNotFoundError):
        service.attempt_manual_retry("fcst_gfs_2026062800_model_a", gateway, trusted_internal=True)

    assert gateway.requests == []


def test_file_journal_manual_retry_uses_failed_source_when_durable_hydro_is_partial_after_later_success(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "fcst_gfs_2026062800_model_a",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "forcing": {"forcing_version_id": "forc_gfs_2026062800_model_a"},
        },
    )
    repository.update_hydro_run_status(
        "fcst_gfs_2026062800_model_a",
        "partially_failed",
        error_code="OUTPUT_INCOMPLETE",
    )
    failed = _pipeline_reservation_record(cycle_time, job_id="job_partial_failed_source")
    failed.update(
        {
            "status": "failed",
            "error_code": "OUTPUT_INCOMPLETE",
            "created_at": "2026-06-28T00:00:00Z",
            "updated_at": "2026-06-28T00:05:00Z",
            "finished_at": "2026-06-28T00:05:00Z",
        }
    )
    success = _pipeline_reservation_record(cycle_time, job_id="job_later_success")
    success.update(
        {
            "idempotency_key": "gfs:gfs_2026062800:basin_a:later_success",
            "status": "succeeded",
            "retry_count": 1,
            "created_at": "2026-06-28T00:10:00Z",
            "updated_at": "2026-06-28T00:20:00Z",
            "finished_at": "2026-06-28T00:20:00Z",
        }
    )
    repository.upsert_pipeline_job(failed)
    repository.upsert_pipeline_job(success)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    repair = service.record_manual_repair("fcst_gfs_2026062800_model_a", trusted_internal=True)
    state = _candidate_state(repository, cycle_time=cycle_time)

    assert repair.job_id == "job_partial_failed_source"
    assert repair.status == "manual_repair_requested"
    assert state is not None
    event = next(event for event in state["pipeline_events"] if event["entity_id"] == "job_partial_failed_source")
    assert event["details"]["previous_job_id"] == "job_partial_failed_source"
    assert event["details"]["prior_failure_reason"] == "OUTPUT_INCOMPLETE"


def _durable_hydro_manual_retry_fixture(
    tmp_path: Path,
    *,
    durable_status: str,
) -> tuple[FileOrchestrationJournalRepository, FileJournalRetryService, datetime]:
    """A file-lane run whose one retryable source job sits under a durable hydro status."""

    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "fcst_gfs_2026062800_model_a",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "forcing": {"forcing_version_id": "forc_gfs_2026062800_model_a"},
        },
    )
    repository.update_hydro_run_status("fcst_gfs_2026062800_model_a", durable_status)
    failed = _pipeline_reservation_record(cycle_time, job_id="job_durable_status_failed")
    failed.update(
        {
            "status": "failed",
            "error_code": "SLURM_TIMEOUT",
            "created_at": "2026-06-28T00:00:00Z",
            "updated_at": "2026-06-28T00:05:00Z",
            "finished_at": "2026-06-28T00:05:00Z",
        }
    )
    repository.upsert_pipeline_job(failed)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    return repository, service, cycle_time


@pytest.mark.parametrize("durable_status", ["succeeded", "parsed", "published"])
def test_file_journal_manual_retry_refuses_durable_hydro_success_without_mutation(
    tmp_path: Path,
    durable_status: str,
) -> None:
    """The file twin of the DB lane's durable-success refusal (openspec change durable-status-name-split).

    Both operator entry points read `MANUAL_RETRY_DURABLE_SUCCESS_STATUSES`; a run whose
    durable hydro status is one of its members is refused even though a failed job would
    otherwise be a perfectly good retry source.  The arm had no coverage before this change,
    so a botched rename here could have swapped the set silently.
    """

    journal_root = tmp_path / "journal"
    repository, service, cycle_time = _durable_hydro_manual_retry_fixture(tmp_path, durable_status=durable_status)
    before_records = (journal_root / "journal/gfs/2026062800.jsonl").read_text(encoding="utf-8")

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7010", "status": "submitted"}

    gateway = Gateway()

    with pytest.raises(RetryNotFoundError):
        service.record_manual_repair("fcst_gfs_2026062800_model_a", trusted_internal=True)
    with pytest.raises(RetryNotFoundError):
        service.attempt_manual_retry("fcst_gfs_2026062800_model_a", gateway, trusted_internal=True)

    after_records = (journal_root / "journal/gfs/2026062800.jsonl").read_text(encoding="utf-8")
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert gateway.requests == []
    assert after_records == before_records
    assert state is not None
    assert state["pipeline_events_total"] == 0
    assert {job["job_id"] for job in state["pipeline_jobs"]} == {"job_durable_status_failed"}


def test_file_journal_manual_retry_proceeds_when_durable_hydro_status_is_complete(tmp_path: Path) -> None:
    """`"complete"` is durable success for the scheduler but must not block a manual retry.

    This is the lane-level half of the membership split: the DB lane cannot express
    `"complete"` (no such `hydro.run_status` enum value) but the file journal can, so the
    merge direction "add `complete` to the manual-retry set" is only observable here.
    """

    repository, service, cycle_time = _durable_hydro_manual_retry_fixture(tmp_path, durable_status="complete")

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7011", "status": "submitted"}

    gateway = Gateway()

    repair = service.record_manual_repair("fcst_gfs_2026062800_model_a", trusted_internal=True)
    retried = service.attempt_manual_retry("fcst_gfs_2026062800_model_a", gateway, trusted_internal=True)
    state = _candidate_state(repository, cycle_time=cycle_time)

    assert repair.job_id == "job_durable_status_failed"
    assert repair.status == "manual_repair_requested"
    assert retried.status == "submitted"
    assert gateway.requests
    assert state is not None
    assert "job_durable_status_failed" in {job["job_id"] for job in state["pipeline_jobs"]}
    assert retried.job_id in {job["job_id"] for job in state["pipeline_jobs"]}


def test_file_journal_active_manual_retry_blocks_repair_and_submission_without_mutation(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    failed = _pipeline_reservation_record(cycle_time, job_id="job_manual_conflict_failed")
    failed.update({"status": "failed", "error_code": "SLURM_TIMEOUT", "finished_at": "2026-06-28T00:05:00Z"})
    active_retry = _pipeline_reservation_record(cycle_time, job_id="fcst_gfs_2026062800_model_a_retry_active")
    active_retry.update(
        {
            "status": "pending",
            "retry_count": 2,
            "manual_retry_marker": True,
            "previous_job_id": "job_manual_conflict_failed",
            "idempotency_key": "manual_retry:fcst_gfs_2026062800_model_a:2",
            "created_at": "2026-06-28T00:10:00Z",
            "updated_at": "2026-06-28T00:10:00Z",
        }
    )
    repository.upsert_pipeline_job(failed)
    repository.upsert_pipeline_job(active_retry)
    before_records = (journal_root / "journal/gfs/2026062800.jsonl").read_text(encoding="utf-8")
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7099", "status": "submitted"}

    gateway = Gateway()

    with pytest.raises(journal_module.RetryConflictError):
        service.record_manual_repair("fcst_gfs_2026062800_model_a", trusted_internal=True)
    with pytest.raises(journal_module.RetryConflictError):
        service.attempt_manual_retry("fcst_gfs_2026062800_model_a", gateway, trusted_internal=True)

    after_records = (journal_root / "journal/gfs/2026062800.jsonl").read_text(encoding="utf-8")
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert gateway.requests == []
    assert after_records == before_records
    assert state is not None
    assert state["pipeline_events_total"] == 0
    assert {job["job_id"] for job in state["pipeline_jobs"]} == {
        "job_manual_conflict_failed",
        "fcst_gfs_2026062800_model_a_retry_active",
    }


def test_file_journal_manual_retry_truth_sort_uses_created_at_before_retry_count(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    failed = _pipeline_reservation_record(cycle_time, job_id="job_equal_truth_failed")
    failed.update(
        {
            "status": "failed",
            "retry_count": 7,
            "error_code": "SLURM_TIMEOUT",
            "created_at": "2026-06-28T00:00:00Z",
            "updated_at": "2026-06-28T00:30:00Z",
            "finished_at": "2026-06-28T00:30:00Z",
        }
    )
    success = _pipeline_reservation_record(cycle_time, job_id="job_equal_truth_success")
    success.update(
        {
            "idempotency_key": "gfs:gfs_2026062800:basin_a:forecast_equal_truth_success",
            "status": "succeeded",
            "retry_count": 1,
            "created_at": "2026-06-28T00:10:00Z",
            "updated_at": "2026-06-28T00:30:00Z",
            "finished_at": "2026-06-28T00:20:00Z",
        }
    )
    repository.upsert_pipeline_job(failed)
    repository.upsert_pipeline_job(success)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    with pytest.raises(RetryNotFoundError):
        service.record_manual_repair("fcst_gfs_2026062800_model_a", trusted_internal=True)

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7006", "status": "submitted"}

    gateway = Gateway()

    with pytest.raises(RetryNotFoundError):
        service.attempt_manual_retry("fcst_gfs_2026062800_model_a", gateway, trusted_internal=True)

    assert gateway.requests == []


def test_file_journal_manual_retry_submission_failure_redacts_persisted_event_and_job_records(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    secret_message = (
        "sbatch failed for https://alice:pass123@slurm.example/sbatch?"
        "X-Amz-Signature=sig123&token=tok123 token=tok123 password=pass123 "
        "Authorization: Bearer live-token-123 authorization=Basic basic-secret-123 "
        "{\"Authorization\": \"Bearer json-retry-token-123\"} "
        "Proxy-Authorization='Basic proxy-retry-secret-123' "
        "stderr=\"Bearer bare-retry-token-123\" Basic bare-basic-retry-secret-123; next field"
    )
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_secret_failed")
    repository.reserve_pipeline_job(record)
    repository.update_pipeline_job_status(
        "job_secret_failed",
        "failed",
        error_code="SLURM_UNAVAILABLE",
        error_message="slurm unavailable",
        finished_at=cycle_time,
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            raise RuntimeError(secret_message)

    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried = service.attempt_manual_retry("fcst_gfs_2026062800_model_a", gateway, trusted_internal=True)
    raw_journal = (tmp_path / "journal/journal/gfs/2026062800.jsonl").read_text(encoding="utf-8")
    latest_files = sorted((tmp_path / "journal/latest").rglob("*.json"))
    latest_rendered = "\n".join(path.read_text(encoding="utf-8") for path in latest_files)
    direct_rendered = (tmp_path / f"journal/pipeline-jobs/{retried.job_id}.json").read_text(encoding="utf-8")
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    event = next(
        event
        for event in state["pipeline_events"]
        if event["entity_id"] == retried.job_id and event["status_to"] == "submission_failed"
    )
    event_output = json.dumps(event, sort_keys=True)

    assert retried.status == "submission_failed"
    assert retried.error_code == "SBATCH_SUBMISSION_FAILED"
    assert service.retry_policy_for_job(retried)["classifier"] == "transient_slurm_runtime"
    assert gateway.requests
    assert latest_files
    for rendered in (raw_journal, latest_rendered, direct_rendered, event_output, retried.error_message):
        for raw_secret in (
            "alice:pass123",
            "pass123",
            "sig123",
            "tok123",
            "live-token-123",
            "basic-secret-123",
            "json-retry-token-123",
            "proxy-retry-secret-123",
            "bare-retry-token-123",
            "bare-basic-retry-secret-123",
        ):
            assert raw_secret not in rendered
        assert "[redacted]" in rendered


def test_file_journal_manual_retry_submission_failure_preserves_explicit_error_code(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_explicit_code_failed")
    repository.reserve_pipeline_job(record)
    repository.update_pipeline_job_status(
        "job_explicit_code_failed",
        "failed",
        error_code="SLURM_UNAVAILABLE",
        finished_at=cycle_time,
    )

    class ExplicitCodeError(RuntimeError):
        code = "SBATCH_ACCOUNT_BLOCKED"

    class Gateway:
        def submit_job(self, request: Any) -> dict[str, Any]:
            raise ExplicitCodeError("account blocked")

    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried = service.attempt_manual_retry("fcst_gfs_2026062800_model_a", Gateway(), trusted_internal=True)

    assert retried.status == "submission_failed"
    assert retried.error_code == "SBATCH_ACCOUNT_BLOCKED"
    event = next(
        event
        for event in _candidate_state(repository, cycle_time=cycle_time)["pipeline_events"]
        if event["entity_id"] == retried.job_id and event["status_to"] == "submission_failed"
    )
    assert event["details"]["error_code"] == "SBATCH_ACCOUNT_BLOCKED"


def test_file_journal_download_source_manual_retry_manifest_and_hydro_reset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    workspace_root = tmp_path / "workspace"
    object_store_root = tmp_path / "object-store"
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("OBJECT_STORE_ROOT", raising=False)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "cycle_gfs_2026062800",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "forcing": {"forcing_version_id": "forc_gfs_2026062800_model_a"},
        },
    )
    repository.update_hydro_run_status(
        "cycle_gfs_2026062800",
        "failed",
        error_code="SOURCE_CYCLE_UNAVAILABLE",
        error_message="source cycle unavailable",
    )
    record = _pipeline_reservation_record(cycle_time, job_id="job_download_failed")
    record.update(
        {
            "run_id": "cycle_gfs_2026062800",
            "job_type": "download_source_cycle",
            "stage": "download",
            "model_id": None,
            "retry_count": 3,
            "idempotency_key": "gfs:gfs_2026062800:download",
        }
    )
    repository.reserve_pipeline_job(record)
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_download_failed",
        event_type="submission",
        status_from="reserved",
        status_to="submitted",
        details={
            "runtime_root_contract": {
                "workspace_dir": str(workspace_root),
                "object_store_root": str(object_store_root),
            }
        },
    )
    repository.update_pipeline_job_status(
        "job_download_failed",
        "permanently_failed",
        error_code="SOURCE_CYCLE_UNAVAILABLE",
        finished_at=cycle_time,
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7001", "status": "submitted", "submitted_at": "2026-06-28T00:15:00Z"}

    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried = service.attempt_manual_retry("cycle_gfs_2026062800", gateway, trusted_internal=True)
    hydro_run = repository._hydro_run_for("cycle_gfs_2026062800")
    state = _candidate_state(repository, cycle_time=cycle_time)

    assert retried.status == "submitted"
    assert gateway.requests[0].manifest["source_id"] == "gfs"
    assert gateway.requests[0].manifest["cycle_time"] == "2026062800"
    assert gateway.requests[0].manifest["workspace_dir"] == str(workspace_root)
    assert gateway.requests[0].manifest["object_store_root"] == str(object_store_root)
    assert hydro_run is not None
    assert hydro_run["status"] == "pending"
    assert hydro_run["error_code"] is None
    assert state is not None
    submission_event = next(
        event
        for event in state["pipeline_events"]
        if event["entity_id"] == retried.job_id and event["status_to"] == "submitted"
    )
    assert submission_event["details"]["runtime_root_resolution"]["resolved"]["workspace_dir"]["present"] is True
    assert submission_event["details"]["runtime_root_contract"]["object_store_root"] == "[local-path]"
    journal_records = [
        json.loads(line)
        for line in (tmp_path / "journal/journal/gfs/2026062800.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    persisted_submission = next(
        record
        for record in reversed(journal_records)
        if record.get("record_type") == "pipeline_event"
        and record.get("payload", {}).get("entity_id") == retried.job_id
        and record.get("payload", {}).get("status_to") == "submitted"
    )
    rendered_submission = json.dumps(persisted_submission, sort_keys=True)
    assert str(workspace_root) not in rendered_submission
    assert str(object_store_root) not in rendered_submission
    assert "[local-path]" in rendered_submission


def test_file_journal_download_retry_recovers_runtime_roots_from_historical_manifest_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    workspace_root = tmp_path / "workspace"
    object_store_root = tmp_path / "object-store"
    for name in (
        "WORKSPACE_ROOT",
        "OBJECT_STORE_ROOT",
        "OBJECT_STORE_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_download_failed")
    record.update(
        {
            "run_id": "cycle_gfs_2026062800",
            "job_type": "download_source_cycle",
            "stage": "download",
            "model_id": None,
            "retry_count": 1,
            "idempotency_key": "gfs:gfs_2026062800:download",
        }
    )
    repository.reserve_pipeline_job(record)
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_download_failed",
        event_type="submission",
        status_from="reserved",
        status_to="submitted",
        details={
            "request_manifest": {
                "workspace_dir": str(workspace_root),
                "object_store_root": str(workspace_root),
                "object_store_prefix": "request-prefix",
            },
            "slurm": {
                "manifest": {
                    "workspace_dir": str(workspace_root),
                    "object_store_root": str(object_store_root),
                    "object_store_prefix": "s3://nhms-prod",
                }
            },
        },
    )
    repository.update_pipeline_job_status(
        "job_download_failed",
        "permanently_failed",
        error_code="SOURCE_CYCLE_UNAVAILABLE",
        finished_at=cycle_time,
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7004", "status": "submitted"}

    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried = service.attempt_manual_retry("cycle_gfs_2026062800", gateway, trusted_internal=True)

    assert retried.status == "submitted"
    assert gateway.requests
    assert gateway.requests[0].manifest["workspace_dir"] == str(workspace_root)
    assert gateway.requests[0].manifest["object_store_root"] == str(object_store_root)
    assert gateway.requests[0].manifest["object_store_prefix"] == "s3://nhms-prod"
    submission_event = next(
        event
        for event in repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=None).pipeline_events
        if event["entity_id"] == retried.job_id and event["status_to"] == "submitted"
    )
    evidence = submission_event["details"]["runtime_root_resolution"]
    assert "slurm.manifest" in evidence["resolved"]["workspace_dir"]["source"]
    assert any(item["reason"] == "resolves_to_workspace_dir" for item in evidence["rejected"])
    assert evidence["candidate_counts"]["event_candidates_total"] >= 2


def test_file_journal_download_manual_retry_uses_previous_failed_job_runtime_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    old_workspace_root = tmp_path / "old-workspace"
    old_object_store_root = tmp_path / "old-object-store"
    corrected_workspace_root = tmp_path / "corrected-workspace"
    corrected_object_store_root = tmp_path / "corrected-object-store"
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("OBJECT_STORE_ROOT", raising=False)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")

    old_failed = _pipeline_reservation_record(cycle_time, job_id="job_download_old_failed")
    old_failed.update(
        {
            "run_id": "cycle_gfs_2026062800",
            "job_type": "download_source_cycle",
            "stage": "download",
            "model_id": None,
            "idempotency_key": "gfs:gfs_2026062800:download-old",
        }
    )
    corrected_failed = _pipeline_reservation_record(cycle_time, job_id="job_download_corrected_failed")
    corrected_failed.update(
        {
            "run_id": "cycle_gfs_2026062800",
            "job_type": "download_source_cycle",
            "stage": "download",
            "model_id": None,
            "retry_count": 1,
            "idempotency_key": "gfs:gfs_2026062800:download-corrected",
        }
    )
    repository.reserve_pipeline_job(old_failed)
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_download_old_failed",
        event_type="submission",
        status_from="reserved",
        status_to="submitted",
        details={
            "runtime_root_contract": {
                "workspace_dir": str(old_workspace_root),
                "object_store_root": str(old_object_store_root),
                "object_store_prefix": "s3://old-prefix",
            }
        },
    )
    repository.update_pipeline_job_status(
        "job_download_old_failed",
        "failed",
        error_code="SOURCE_CYCLE_UNAVAILABLE",
        finished_at=_dt("2026-06-28T00:20:00Z"),
    )
    repository.reserve_pipeline_job(corrected_failed)
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_download_corrected_failed",
        event_type="submission",
        status_from="reserved",
        status_to="submitted",
        details={
            "runtime_root_contract": {
                "workspace_dir": str(corrected_workspace_root),
                "object_store_root": str(corrected_object_store_root),
                "object_store_prefix": "s3://corrected-prefix",
            }
        },
    )
    repository.update_pipeline_job_status(
        "job_download_corrected_failed",
        "failed",
        error_code="SOURCE_CYCLE_UNAVAILABLE",
        finished_at=_dt("2026-06-28T00:10:00Z"),
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7005", "status": "submitted"}

    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried = service.attempt_manual_retry("cycle_gfs_2026062800", gateway, trusted_internal=True)

    assert retried.status == "submitted"
    assert retried.previous_job_id == "job_download_corrected_failed"
    assert gateway.requests[0].manifest["workspace_dir"] == str(corrected_workspace_root)
    assert gateway.requests[0].manifest["object_store_root"] == str(corrected_object_store_root)
    assert gateway.requests[0].manifest["object_store_prefix"] == "s3://corrected-prefix"
    persisted_retry = repository.get_pipeline_job(retried.job_id)
    assert persisted_retry["previous_job_id"] == "job_download_corrected_failed"
    submission_event = next(
        event
        for event in repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=None).pipeline_events
        if event["entity_id"] == retried.job_id and event["status_to"] == "submitted"
    )
    evidence = submission_event["details"]["runtime_root_resolution"]
    assert evidence["previous_job_id"] == "job_download_corrected_failed"
    assert "job_download_corrected_failed" in evidence["resolved"]["workspace_dir"]["source"]


def test_file_journal_download_retry_ignores_stale_manual_retry_runtime_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    workspace_root = tmp_path / "workspace"
    object_store_root = tmp_path / "object-store"
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("OBJECT_STORE_ROOT", raising=False)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "cycle_gfs_2026062800",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "forcing": {"forcing_version_id": "forc_gfs_2026062800_model_a"},
        },
    )
    repository.update_hydro_run_status("cycle_gfs_2026062800", "failed", error_code="SOURCE_CYCLE_UNAVAILABLE")
    stale_manual = _pipeline_reservation_record(cycle_time, job_id="job_manual_stale")
    stale_manual.update(
        {
            "run_id": "cycle_gfs_2026062800",
            "job_type": "download_source_cycle",
            "stage": "download",
            "model_id": None,
            "manual_retry_marker": True,
            "idempotency_key": "gfs:gfs_2026062800:manual-stale",
        }
    )
    source_failed = _pipeline_reservation_record(cycle_time, job_id="job_download_failed")
    source_failed.update(
        {
            "run_id": "cycle_gfs_2026062800",
            "job_type": "download_source_cycle",
            "stage": "download",
            "model_id": None,
            "idempotency_key": "gfs:gfs_2026062800:download",
        }
    )
    repository.reserve_pipeline_job(stale_manual)
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_manual_stale",
        event_type="submission",
        status_from="pending",
        status_to="submission_failed",
        details={
            "trigger": "manual",
            "runtime_root_contract": {
                "workspace_dir": str(workspace_root),
                "object_store_root": str(object_store_root),
            },
        },
    )
    repository.update_pipeline_job_status(
        "job_manual_stale",
        "submission_failed",
        error_code="SBATCH_REJECTED",
        finished_at=_dt("2026-06-28T00:01:00Z"),
    )
    repository.reserve_pipeline_job(source_failed)
    repository.update_pipeline_job_status(
        "job_download_failed",
        "permanently_failed",
        error_code="SOURCE_CYCLE_UNAVAILABLE",
        finished_at=_dt("2026-06-28T00:05:00Z"),
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7002", "status": "submitted"}

    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried = service.attempt_manual_retry("cycle_gfs_2026062800", gateway, trusted_internal=True)

    assert retried.status == "submission_failed"
    assert retried.error_code == "RETRY_RUNTIME_ROOTS_UNRESOLVED"
    assert gateway.requests == []


def test_file_journal_download_retry_recovers_original_contract_through_stale_retry_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    original_workspace_root = tmp_path / "original-workspace"
    original_object_store_root = tmp_path / "original-object-store"
    stale_workspace_root = tmp_path / "stale-workspace"
    stale_object_store_root = tmp_path / "stale-object-store"
    for name in (
        "WORKSPACE_ROOT",
        "OBJECT_STORE_ROOT",
        "OBJECT_STORE_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    original = _pipeline_reservation_record(cycle_time, job_id="job_download_original")
    original.update(
        {
            "run_id": "cycle_gfs_2026062800",
            "job_type": "download_source_cycle",
            "stage": "download",
            "model_id": None,
            "status": "failed",
            "retry_count": 1,
            "idempotency_key": "gfs:gfs_2026062800:download-original",
            "error_code": "SOURCE_CYCLE_UNAVAILABLE",
            "created_at": "2026-06-28T00:00:00Z",
            "updated_at": "2026-06-28T00:05:00Z",
            "finished_at": "2026-06-28T00:05:00Z",
        }
    )
    stale_retry = _pipeline_reservation_record(cycle_time, job_id="cycle_gfs_2026062800_retry_active")
    stale_retry.update(
        {
            "run_id": "cycle_gfs_2026062800",
            "job_type": "download_source_cycle",
            "stage": "download",
            "model_id": None,
            "status": "submission_failed",
            "retry_count": 2,
            "manual_retry_marker": True,
            "idempotency_key": "manual_retry:cycle_gfs_2026062800:2",
            "created_at": "2026-06-28T00:10:00Z",
            "updated_at": "2026-06-28T00:12:00Z",
        }
    )
    repository.upsert_pipeline_job(original)
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_download_original",
        event_type="submission",
        status_from="reserved",
        status_to="submitted",
        details={
            "runtime_root_contract": {
                "workspace_dir": str(original_workspace_root),
                "object_store_root": str(original_object_store_root),
                "object_store_prefix": "s3://nhms-original",
            }
        },
    )
    repository.upsert_pipeline_job(stale_retry)
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="cycle_gfs_2026062800_retry_active",
        event_type="retry",
        status_from="failed",
        status_to="pending",
        details={"trigger": "manual", "previous_job_id": "job_download_original"},
    )
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="cycle_gfs_2026062800_retry_active",
        event_type="submission",
        status_from="pending",
        status_to="submission_failed",
        details={
            "trigger": "manual",
            "runtime_root_contract": {
                "workspace_dir": str(stale_workspace_root),
                "object_store_root": str(stale_object_store_root),
                "object_store_prefix": "s3://nhms-stale",
            },
        },
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7007", "status": "submitted"}

    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried = service.attempt_manual_retry("cycle_gfs_2026062800", gateway, trusted_internal=True)

    assert retried.status == "submitted"
    assert retried.job_id == "cycle_gfs_2026062800_retry_2"
    assert gateway.requests[0].manifest["workspace_dir"] == str(original_workspace_root)
    assert gateway.requests[0].manifest["object_store_root"] == str(original_object_store_root)
    assert gateway.requests[0].manifest["object_store_prefix"] == "s3://nhms-original"
    assert "stale" not in json.dumps(gateway.requests[0].manifest, sort_keys=True)
    submission_event = next(
        event
        for event in repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=None).pipeline_events
        if event["entity_id"] == retried.job_id and event["status_to"] == "submitted"
    )
    evidence = submission_event["details"]["runtime_root_resolution"]
    assert "job_download_original" in evidence["resolved"]["workspace_dir"]["source"]
    assert evidence["candidate_counts"]["manual_retry_event_rows_ignored"] == 1


def test_file_journal_download_retry_same_run_runtime_root_scan_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    for name in (
        "WORKSPACE_ROOT",
        "OBJECT_STORE_ROOT",
        "OBJECT_STORE_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    for index in range(journal_module._RUNTIME_ROOT_SAME_RUN_JOB_SCAN_LIMIT + 40):
        job = _pipeline_reservation_record(cycle_time, job_id=f"job_download_no_candidate_{index:03d}")
        job.update(
            {
                "run_id": "cycle_gfs_2026062800",
                "job_type": "download_source_cycle",
                "stage": "download",
                "model_id": None,
                "status": "failed",
                "retry_count": index,
                "idempotency_key": f"gfs:gfs_2026062800:download-no-candidate-{index:03d}",
                "error_code": "SOURCE_CYCLE_UNAVAILABLE",
                "created_at": f"2026-06-28T00:{index % 60:02d}:00Z",
                "updated_at": f"2026-06-28T00:{index % 60:02d}:30Z",
                "finished_at": f"2026-06-28T00:{index % 60:02d}:30Z",
            }
        )
        repository.upsert_pipeline_job(job)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=100, backoff_schedule=[0]))
    scanned_job_ids: list[str] = []
    original_candidates = service._file_retry_event_runtime_root_candidates

    def counting_candidates(job_id: str, *, candidate_budget: int) -> Any:
        scanned_job_ids.append(job_id)
        return original_candidates(job_id, candidate_budget=candidate_budget)

    monkeypatch.setattr(service, "_file_retry_event_runtime_root_candidates", counting_candidates)

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7011", "status": "submitted"}

    gateway = Gateway()
    retried = service.attempt_manual_retry("cycle_gfs_2026062800", gateway, trusted_internal=True)
    events = repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=None).pipeline_events
    failure_event = next(
        event
        for event in events
        if event["entity_id"] == retried.job_id and event["status_to"] == "submission_failed"
    )
    candidate_counts = failure_event["details"]["runtime_root_resolution"]["candidate_counts"]

    assert retried.status == "submission_failed"
    assert retried.error_code == "RETRY_RUNTIME_ROOTS_UNRESOLVED"
    assert gateway.requests == []
    assert len(scanned_job_ids) <= journal_module._RUNTIME_ROOT_SAME_RUN_JOB_SCAN_LIMIT + 1
    assert candidate_counts["event_rows_omitted"] >= 39


def test_file_journal_download_retry_failure_persists_only_redacted_runtime_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    workspace_root = tmp_path / "workspace"
    object_store_root = tmp_path / "object-store"
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_store_root))
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "cycle_gfs_2026062800",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "forcing": {"forcing_version_id": "forc_gfs_2026062800_model_a"},
        },
    )
    repository.update_hydro_run_status("cycle_gfs_2026062800", "failed", error_code="SOURCE_CYCLE_UNAVAILABLE")
    record = _pipeline_reservation_record(cycle_time, job_id="job_download_failed")
    record.update(
        {
            "run_id": "cycle_gfs_2026062800",
            "job_type": "download_source_cycle",
            "stage": "download",
            "model_id": None,
            "retry_count": 3,
            "idempotency_key": "gfs:gfs_2026062800:download",
        }
    )
    repository.reserve_pipeline_job(record)
    repository.update_pipeline_job_status(
        "job_download_failed",
        "permanently_failed",
        error_code="SOURCE_CYCLE_UNAVAILABLE",
        finished_at=cycle_time,
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            raise RuntimeError("gateway rejected submission")

    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried = service.attempt_manual_retry("cycle_gfs_2026062800", gateway, trusted_internal=True)
    raw_journal = (tmp_path / "journal/journal/gfs/2026062800.jsonl").read_text(encoding="utf-8")
    latest_rendered = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "journal/latest").rglob("*.json")
    )

    assert retried.status == "submission_failed"
    assert retried.error_code == "SBATCH_SUBMISSION_FAILED"
    assert gateway.requests
    assert str(workspace_root) not in raw_journal
    assert str(object_store_root) not in raw_journal
    assert str(workspace_root) not in latest_rendered
    assert str(object_store_root) not in latest_rendered
    assert "[local-path]" in raw_journal


def test_file_journal_download_retry_rejects_symlink_loop_runtime_root_without_escaping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The db-free journal leg shares _local_runtime_root_safety with the DB leg.

    Before this change a symlink-loop root either escaped the helper as an
    errno-less RuntimeError into this leg's broad ``except Exception`` (CPython
    <=3.12, degrading the attribution to SBATCH_SUBMISSION_FAILED and dropping
    the runtime_root_resolution evidence) or was silently admitted into the
    submitted manifest (3.13+).
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    loop_first = tmp_path / "loop_a"
    loop_second = tmp_path / "loop_b"
    loop_first.symlink_to(loop_second)
    loop_second.symlink_to(loop_first)
    object_store_root = tmp_path / "object-store"
    object_store_root.mkdir()
    for name in (
        "WORKSPACE_ROOT",
        "OBJECT_STORE_ROOT",
        "OBJECT_STORE_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
    ):
        monkeypatch.delenv(name, raising=False)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "cycle_gfs_2026062800",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "forcing": {"forcing_version_id": "forc_gfs_2026062800_model_a"},
        },
    )
    repository.update_hydro_run_status("cycle_gfs_2026062800", "failed", error_code="SOURCE_CYCLE_UNAVAILABLE")
    record = _pipeline_reservation_record(cycle_time, job_id="job_download_failed")
    record.update(
        {
            "run_id": "cycle_gfs_2026062800",
            "job_type": "download_source_cycle",
            "stage": "download",
            "model_id": None,
            "idempotency_key": "gfs:gfs_2026062800:download",
        }
    )
    repository.reserve_pipeline_job(record)
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_download_failed",
        event_type="submission",
        status_from="pending",
        status_to="submitted",
        details={
            "stage": "download",
            "job_type": "download_source_cycle",
            "runtime_root_contract": {
                "workspace_dir": str(loop_first),
                "object_store_root": str(object_store_root),
                "object_store_prefix": "s3://nhms-prod",
            },
        },
    )
    repository.update_pipeline_job_status(
        "job_download_failed",
        "permanently_failed",
        error_code="SOURCE_CYCLE_UNAVAILABLE",
        finished_at=_dt("2026-06-28T00:05:00Z"),
    )

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7020", "status": "submitted"}

    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried = service.attempt_manual_retry("cycle_gfs_2026062800", gateway, trusted_internal=True)
    events = repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=None).pipeline_events
    failure_event = next(
        event
        for event in events
        if event["entity_id"] == retried.job_id and event["status_to"] == "submission_failed"
    )
    evidence = failure_event["details"]["runtime_root_resolution"]

    assert retried.status == "submission_failed"
    assert retried.error_code == "RETRY_RUNTIME_ROOTS_UNSAFE"
    assert gateway.requests == []
    assert any(
        item["field"] == "workspace_dir" and item["reason"] == "unresolvable_local_root"
        for item in evidence["rejected"]
    )
    assert "workspace_dir" not in evidence["resolved"]


def test_file_orchestration_journal_direct_model_context_must_match_path_model(tmp_path: Path) -> None:
    journal_root = tmp_path / "journal"
    _write_json(journal_root / "models/model_a.json", _direct_model_context_record("model_b"))

    with pytest.raises(OrchestratorError) as error:
        FileOrchestrationJournalRepository(journal_root).load_model_context("model_a")

    assert error.value.error_code == "FILE_JOURNAL_READ_BLOCKED"
    assert "file_journal_model_mismatch" in error.value.message


def test_file_orchestration_journal_direct_forcing_record_must_match_path_identity(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / "forcing/gfs/2026062800/model_a.json",
        _direct_forcing_context_record(source_id="ifs", cycle_time=cycle_time),
    )

    with pytest.raises(OrchestratorError) as error:
        FileOrchestrationJournalRepository(journal_root).find_forcing_context(
            source_id="gfs",
            cycle_time=cycle_time,
            model_id="model_a",
        )

    assert error.value.error_code == "FILE_JOURNAL_READ_BLOCKED"
    assert "file_journal_source_mismatch" in error.value.message


def test_file_orchestration_journal_list_stage_statuses_returns_blocked_row(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        {"schema_version": "wrong", "source_id": "gfs", "cycle_time": cycle_time.isoformat(), "model_id": "model_a"},
    )

    rows = FileOrchestrationJournalRepository(journal_root).list_stage_statuses(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_a",
    )

    assert rows == [
        {
            "stage": "file_journal_read",
            "status": "running",
            "job_id": "file_journal_read_blocked",
            "cycle_id": cycle_id_for("gfs", cycle_time),
            "model_id": "model_a",
            "slurm_job_id": "unknown_after_attempt",
            "error_code": "file_journal_schema_mismatch",
            "file_journal": {
                "status": "blocked",
                "reason": "file_journal_schema_mismatch",
                "field": "schema_version",
                "evidence": {
                    "expected": FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
                    "actual": "wrong",
                },
            },
        }
    ]


def test_file_orchestration_journal_list_stage_statuses_all_sources_for_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    gfs_job = _source_job(cycle_time, source_id="gfs", job_id="job_gfs_forecast")
    ifs_job = _source_job(cycle_time, source_id="ifs", job_id="job_ifs_forecast")
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(source_id="gfs", cycle_time=cycle_time, jobs=[gfs_job]),
    )
    _write_json(
        journal_root / "latest/ifs/2026062800/model_a.json",
        _latest_view(source_id="ifs", cycle_time=cycle_time, jobs=[ifs_job]),
    )
    repository = FileOrchestrationJournalRepository(journal_root)
    read_paths: list[Path] = []
    original_read_optional_json = repository._read_optional_json

    def read_optional_json(path: Path) -> dict[str, Any] | None:
        read_paths.append(path.relative_to(journal_root))
        return original_read_optional_json(path)

    monkeypatch.setattr(repository, "_read_optional_json", read_optional_json)

    rows = repository.list_stage_statuses(
        source_id=None,
        cycle_time=cycle_time,
        model_id="model_a",
    )

    assert {
        (source.source_id, source.source_segment)
        for source in repository._cycle_source_discoveries(cycle_time=cycle_time)
    } == {("gfs", "gfs"), ("IFS", "ifs")}
    assert Path("latest/ifs/2026062800/model_a.json") in read_paths
    assert Path("latest/IFS/2026062800/model_a.json") not in read_paths
    assert {(row["job_id"], row["source_id"]) for row in rows} == {
        ("job_gfs_forecast", "gfs"),
        ("job_ifs_forecast", "IFS"),
    }
    assert {row["cycle_id"] for row in rows} == {
        cycle_id_for("gfs", cycle_time),
        cycle_id_for("ifs", cycle_time),
    }


def test_file_orchestration_journal_list_stage_statuses_all_sources_reads_mixed_alias_history(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_stamp = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    latest_job = _active_job(cycle_time)
    latest_job.update(
        {
            "job_id": "job_ifs_latest_download",
            "idempotency_key": f"cycle_ifs_{cycle_stamp}:download_source_cycle",
            "run_id": f"fcst_ifs_{cycle_stamp}_model_a",
            "cycle_id": cycle_id_for("IFS", cycle_time),
            "source_id": "IFS",
            "stage": "download_source_cycle",
        }
    )
    history_job = _active_job(cycle_time)
    history_job.update(
        {
            "job_id": "job_ifs_history_forecast",
            "idempotency_key": f"cycle_ifs_{cycle_stamp}:forecast",
            "run_id": f"fcst_ifs_{cycle_stamp}_model_a",
            "cycle_id": cycle_id_for("IFS", cycle_time),
            "source_id": "ifs",
            "stage": "forecast",
            "slurm_job_id": "3002",
            "submitted_at": "2026-06-28T00:02:00Z",
        }
    )
    latest = _latest_view(source_id="IFS", cycle_time=cycle_time, jobs=[latest_job])
    latest["forcing_version"] = None
    _write_json(journal_root / "latest/IFS/2026062800/model_a.json", latest)
    _write_jsonl(
        journal_root / "journal/ifs/2026062800.jsonl",
        [
            _journal_record(
                record_type="pipeline_job",
                source_id="ifs",
                cycle_time=cycle_time,
                payload=history_job,
            )
        ],
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert [
        (source.source_id, source.source_segments)
        for source in repository._cycle_source_discoveries(cycle_time=cycle_time)
    ] == [("IFS", ("IFS", "ifs"))]

    rows = repository.list_stage_statuses(
        source_id=None,
        cycle_time=cycle_time,
        model_id="model_a",
    )

    rows_by_job_id = {row["job_id"]: row for row in rows}
    assert set(rows_by_job_id) == {"job_ifs_latest_download", "job_ifs_history_forecast"}
    assert {row["source_id"] for row in rows_by_job_id.values()} == {"IFS"}
    assert {row["cycle_id"] for row in rows_by_job_id.values()} == {cycle_id_for("IFS", cycle_time)}


def test_file_orchestration_journal_list_stage_statuses_preserves_db_stage_order(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    jobs = [
        _source_job(cycle_time, source_id="gfs", job_id="job_unknown", stage="custom_stage"),
        _source_job(cycle_time, source_id="gfs", job_id="job_forecast", stage="forecast"),
        _source_job(cycle_time, source_id="gfs", job_id="job_convert_canonical", stage="convert_canonical"),
        _source_job(cycle_time, source_id="gfs", job_id="job_download_gfs", stage="download_gfs"),
        _source_job(cycle_time, source_id="gfs", job_id="job_forcing", stage="forcing"),
        _source_job(cycle_time, source_id="gfs", job_id="job_parse_output", stage="parse_output"),
        _source_job(cycle_time, source_id="gfs", job_id="job_publish", stage="publish"),
    ]
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=jobs),
    )

    rows = FileOrchestrationJournalRepository(journal_root).list_stage_statuses(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_a",
    )

    assert [row["job_id"] for row in rows] == [
        "job_download_gfs",
        "job_convert_canonical",
        "job_forcing",
        "job_forecast",
        "job_publish",
        "job_parse_output",
        "job_unknown",
    ]


def test_file_orchestration_journal_list_stage_statuses_all_sources_blocks_malformed_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    gfs_job = _source_job(cycle_time, source_id="gfs", job_id="job_gfs_forecast")
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(source_id="gfs", cycle_time=cycle_time, jobs=[gfs_job]),
    )
    _write_json(
        journal_root / "latest/ifs/2026062800/model_a.json",
        {
            "schema_version": "wrong /secret/schema token=stage-secret",
            "source_id": "ifs",
            "cycle_time": cycle_time.isoformat(),
            "model_id": "model_a",
        },
    )
    repository = FileOrchestrationJournalRepository(journal_root)
    read_paths: list[Path] = []
    original_read_optional_json = repository._read_optional_json

    def read_optional_json(path: Path) -> dict[str, Any] | None:
        read_paths.append(path.relative_to(journal_root))
        return original_read_optional_json(path)

    monkeypatch.setattr(repository, "_read_optional_json", read_optional_json)

    rows = repository.list_stage_statuses(
        source_id=None,
        cycle_time=cycle_time,
        model_id="model_a",
    )
    rendered = json.dumps(rows, sort_keys=True)

    assert Path("latest/ifs/2026062800/model_a.json") in read_paths
    assert Path("latest/IFS/2026062800/model_a.json") not in read_paths
    assert [row["job_id"] for row in rows] == ["job_gfs_forecast", "file_journal_read_blocked"]
    assert rows[1]["cycle_id"] == cycle_id_for("ifs", cycle_time)
    assert rows[1]["error_code"] == "file_journal_schema_mismatch"
    assert "/secret/schema" not in rendered
    assert "stage-secret" not in rendered
    assert "[local-path]" in rendered
    assert "[redacted]" in rendered


def test_file_orchestration_journal_discovery_uses_no_follow_listing_and_missing_dirs_are_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time)) == []

    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[_active_job(cycle_time)]),
    )

    def fail_path_iterdir(_path: Path) -> Any:
        raise AssertionError("file journal discovery must not use path-based iterdir")

    monkeypatch.setattr(Path, "iterdir", fail_path_iterdir)

    assert repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))[0]["job_id"] == (
        "job_cycle_gfs_2026062800_forecast"
    )


def test_file_orchestration_journal_discovery_blocks_symlink_parent(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    target = tmp_path / "outside-latest"
    target.mkdir()
    journal_root.mkdir()
    (journal_root / "latest").symlink_to(target, target_is_directory=True)

    query = FileOrchestrationJournalRepository(journal_root).query_pipeline_jobs_by_cycle(
        cycle_id_for("gfs", cycle_time)
    )

    # #1734: the containment fault is unchanged -- same error_code, raised by
    # the same stat_no_follow containment check. Only the reported path
    # deepened, because the narrowed lookup enters the tree at the cycle
    # directory instead of at ``latest/``.
    assert query[0]["error_code"] == "file_journal_unsafe_scanned_entry"
    assert query[0]["file_journal"]["field"] == "latest/gfs/2026062800"


@pytest.mark.parametrize(
    ("field_name", "value", "expected_reason"),
    [
        ("job_id", ["job"], "file_journal_invalid_identity"),
        ("job_id", "/secret/job", "file_journal_unsafe_identity"),
        ("run_id", {"run": "bad"}, "file_journal_invalid_identity"),
        ("cycle_id", ["gfs_2026062800"], "file_journal_invalid_identity"),
        ("model_id", {"model": "bad"}, "file_journal_invalid_identity"),
        ("status", {"state": "queued"}, "file_journal_invalid_field"),
        ("stage", ["forecast"], "file_journal_invalid_field"),
        ("slurm_job_id", {"slurm": "3001"}, "file_journal_invalid_field"),
        ("idempotency_key", ["cycle:gfs"], "file_journal_invalid_field"),
    ],
)
def test_file_orchestration_journal_rejects_unsafe_job_scheduler_fields(
    tmp_path: Path,
    field_name: str,
    value: Any,
    expected_reason: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    job[field_name] = value
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[job]),
    )

    state = _candidate_state(FileOrchestrationJournalRepository(journal_root), cycle_time=cycle_time)

    assert state is not None
    assert state["file_journal"]["reason"] == expected_reason
    assert state["file_journal"]["field"] == field_name


@pytest.mark.parametrize(
    ("field_name", "value", "expected_reason"),
    [
        ("entity_id", ["job"], "file_journal_invalid_identity"),
        ("entity_id", "/secret/job", "file_journal_unsafe_identity"),
        ("status_to", {"status": "queued"}, "file_journal_invalid_field"),
    ],
)
def test_file_orchestration_journal_rejects_unsafe_event_scheduler_fields(
    tmp_path: Path,
    field_name: str,
    value: Any,
    expected_reason: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    event = {
        "event_id": 1,
        "entity_type": "pipeline_job",
        "entity_id": job["job_id"],
        "event_type": "status_change",
        "status_to": "queued",
        "created_at": "2026-06-28T00:01:00Z",
    }
    event[field_name] = value
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[job], events=[event]),
    )

    state = _candidate_state(FileOrchestrationJournalRepository(journal_root), cycle_time=cycle_time)

    assert state is not None
    assert state["file_journal"]["reason"] == expected_reason
    assert state["file_journal"]["field"] == field_name


def test_file_orchestration_journal_scoped_cycle_ignores_global_replay_scan(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    other_cycle_time = _dt("2026-06-28T12:00:00Z")
    journal_root = tmp_path / "journal"
    scoped_job = _active_job(cycle_time)
    scoped_job["status"] = "succeeded"
    scoped_job["updated_at"] = "2026-06-28T00:02:00Z"
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="complete", jobs=[scoped_job]),
    )
    intruder_job = _active_job(other_cycle_time)
    intruder_job["status"] = "queued"
    intruder_job["updated_at"] = "2026-06-28T12:02:00Z"
    intruder_job["job_id"] = "job_cycle_ifs_2026062812_forecast"
    intruder_job["idempotency_key"] = "cycle_ifs_2026062812:forecast"
    intruder_job["run_id"] = "cycle_ifs_2026062812"
    intruder_job["cycle_id"] = cycle_id_for("ifs", other_cycle_time)
    _write_json(
        journal_root / "latest/ifs/2026062812/model_a.json",
        _latest_view(source_id="ifs", cycle_time=other_cycle_time, jobs=[intruder_job]),
    )
    _write_json(
        journal_root / "latest/era5/2026062812/model_a.json",
        {
            "schema_version": "wrong",
            "source_id": "ERA5",
            "cycle_time": other_cycle_time.isoformat(),
            "pipeline_jobs": [],
        },
    )

    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False
    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_jobs"][0]["status"] == "succeeded"

    # #1734: ``query_pipeline_jobs_by_cycle`` joined the scoped-reader class.
    # The spec delta requires it ("SHALL NOT read any other cycle's files"), so
    # the foreign ERA5 view no longer blocks a GFS lookup -- exactly the
    # behaviour the three scoped assertions above already demanded of the other
    # readers on this same fixture.
    query = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
    assert [row["job_id"] for row in query] == [scoped_job["job_id"]]
    assert query[0]["status"] == "succeeded"

    # The whole-tree fail-closed property still has a home: an underivable
    # cycle id falls open to the full scan, which does read the foreign view.
    blocked = repository.query_pipeline_jobs_by_cycle("unknown-source_2026062800")
    assert blocked == [
        {
            "job_id": "file_journal_read_blocked",
            "idempotency_key": None,
            "cycle_id": "unknown-source_2026062800",
            "run_id": None,
            "slurm_job_id": "unknown_after_attempt",
            "status": "running",
            "stage": "file_journal_read",
            "error_code": "file_journal_schema_mismatch",
            "file_journal": {
                "status": "blocked",
                "reason": "file_journal_schema_mismatch",
                "field": "schema_version",
                "evidence": {
                    "actual": "wrong",
                    "expected": FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
                },
            },
        }
    ]


def test_file_orchestration_journal_unsafe_segments_fail_closed(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")

    assert repository.has_active_pipeline(source_id="../gfs", cycle_time=cycle_time, model_id="model_a") is True
    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model/a") is True

    state = _candidate_state(repository, source_id="gfs", cycle_time=cycle_time, model_id="model/a")
    assert state is not None
    assert state["file_journal"]["reason"] == "file_journal_unsafe_path_segment"

    query = repository.get_pipeline_job("job/../bad")
    assert query is not None
    assert query["status"] == "running"
    assert query["stage"] == "file_journal_read"
    assert query["error_code"] == "file_journal_unsafe_path_segment"


def test_file_orchestration_journal_candidate_state_sorts_jobs_before_limit(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    older_failed = _active_job(cycle_time)
    older_failed.update(
        {
            "job_id": "job_cycle_gfs_2026062800_forecast_failed",
            "status": "failed",
            "submitted_at": "2026-06-28T00:01:00Z",
            "finished_at": "2026-06-28T00:02:00Z",
            "updated_at": "2026-06-28T00:02:00Z",
            "created_at": "2026-06-28T00:00:00Z",
        }
    )
    newer_active = _active_job(cycle_time)
    newer_active.update(
        {
            "job_id": "job_cycle_gfs_2026062800_forecast_active",
            "status": "running",
            "submitted_at": "2026-06-28T00:03:00Z",
            "started_at": "2026-06-28T00:04:00Z",
            "updated_at": "2026-06-28T00:04:00Z",
            "created_at": "2026-06-28T00:03:00Z",
        }
    )
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[older_failed, newer_active]),
    )

    repository = FileOrchestrationJournalRepository(journal_root)

    state = _candidate_state(repository, cycle_time=cycle_time, job_limit=1)
    assert state is not None
    assert state["pipeline_jobs_total"] == 2
    assert state["state_truncated"] is True
    assert state["pipeline_status"] == "running"
    assert [job["job_id"] for job in state["pipeline_jobs"]] == [newer_active["job_id"]]


def test_file_orchestration_journal_replays_append_only_records(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    event = {
        "event_id": 1,
        "entity_type": "pipeline_job",
        "entity_id": job["job_id"],
        "event_type": "submission",
        "status_to": "queued",
        "created_at": "2026-06-28T00:01:01Z",
    }
    _write_jsonl(
        journal_root / "journal/gfs/2026062800.jsonl",
        [
            {
                "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
                "sequence": 1,
                "record_type": "pipeline_job",
                "source_id": "gfs",
                "cycle_time": cycle_time.isoformat(),
                "payload": job,
            },
            {
                "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
                "sequence": 2,
                "record_type": "pipeline_event",
                "source_id": "gfs",
                "cycle_time": cycle_time.isoformat(),
                "payload": event,
            },
        ],
    )

    repository = FileOrchestrationJournalRepository(journal_root)
    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_a",
        run_id="fcst_gfs_2026062800_model_a",
        forcing_version_id="forc_gfs_2026062800_model_a",
        candidate_id="gfs:2026-06-28T00:00:00Z:model_a:forecast_gfs_deterministic",
    )
    assert state is not None
    assert state["pipeline_events"][0]["event_id"] == 1


def test_file_orchestration_journal_journal_sequence_order_overrides_timestamps_for_same_job(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    terminal_job = _active_job(cycle_time)
    terminal_job.update(
        {
            "status": "failed",
            "slurm_job_id": "1001",
            "submitted_at": "2026-06-28T00:01:00Z",
            "finished_at": "2026-06-28T00:05:00Z",
            "updated_at": "2026-06-28T00:05:00Z",
        }
    )
    active_job = dict(terminal_job)
    active_job.update({"status": "running", "slurm_job_id": "3001"})
    for timestamp_field in ("submitted_at", "finished_at", "updated_at", "created_at"):
        active_job.pop(timestamp_field, None)
    _write_jsonl(
        journal_root / "journal/gfs/2026062800.jsonl",
        [
            _journal_record(
                record_type="pipeline_job",
                source_id="gfs",
                cycle_time=cycle_time,
                payload=terminal_job,
                sequence=1,
            ),
            _journal_record(
                record_type="pipeline_job",
                source_id="gfs",
                cycle_time=cycle_time,
                payload=active_job,
                sequence=2,
            ),
        ],
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    assert repository.active_slurm_jobs(source_id="gfs", cycle_time=cycle_time, model_id="model_a")[0][
        "slurm_job_id"
    ] == "3001"
    assert repository.get_pipeline_job(active_job["job_id"])["status"] == "running"
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "running"
    assert state["pipeline_jobs"][0]["slurm_job_id"] == "3001"
    assert "_file_journal_replay_sequence" not in json.dumps(state, sort_keys=True)


def test_file_orchestration_journal_later_journal_sequence_overrides_stale_latest_view(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    terminal_latest_job = _active_job(cycle_time)
    terminal_latest_job.update(
        {
            "status": "failed",
            "slurm_job_id": "9009",
            "submitted_at": "2026-06-28T00:01:00Z",
            "finished_at": "2026-06-28T00:10:00Z",
            "updated_at": "2026-06-28T00:10:00Z",
        }
    )
    later_journal_job = dict(terminal_latest_job)
    later_journal_job.update({"status": "running", "slurm_job_id": "3001"})
    for timestamp_field in ("submitted_at", "finished_at", "updated_at", "created_at"):
        later_journal_job.pop(timestamp_field, None)
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[terminal_latest_job]),
    )
    _write_jsonl(
        journal_root / "journal/gfs/2026062800.jsonl",
        [
            _journal_record(
                record_type="pipeline_job",
                source_id="gfs",
                cycle_time=cycle_time,
                payload=later_journal_job,
                sequence=2,
            )
        ],
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    assert repository.active_slurm_jobs(source_id="gfs", cycle_time=cycle_time, model_id="model_a")[0][
        "slurm_job_id"
    ] == "3001"
    assert repository.get_pipeline_job(later_journal_job["job_id"])["status"] == "running"
    assert repository.query_candidate_state(later_journal_job["idempotency_key"])["slurm_job_id"] == "3001"
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "running"
    assert state["pipeline_jobs"][0]["slurm_job_id"] == "3001"


def test_file_orchestration_journal_newer_latest_view_overrides_older_journal_sequence(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    terminal_latest_job = _active_job(cycle_time)
    terminal_latest_job.update(
        {
            "status": "succeeded",
            "slurm_job_id": "9009",
            "submitted_at": "2026-06-28T00:01:00Z",
            "finished_at": "2026-06-28T00:10:00Z",
            "updated_at": "2026-06-28T00:10:00Z",
        }
    )
    older_journal_job = dict(terminal_latest_job)
    older_journal_job.update({"status": "running", "slurm_job_id": "3001"})
    latest = _latest_view(cycle_time=cycle_time, jobs=[terminal_latest_job])
    latest["replay"]["latest_sequence"] = 10
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    _write_jsonl(
        journal_root / "journal/gfs/2026062800.jsonl",
        [
            _journal_record(
                record_type="pipeline_job",
                source_id="gfs",
                cycle_time=cycle_time,
                payload=older_journal_job,
                sequence=5,
            )
        ],
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False
    assert repository.active_slurm_jobs(source_id="gfs", cycle_time=cycle_time, model_id="model_a") == []
    assert repository.get_pipeline_job(terminal_latest_job["job_id"])["status"] == "succeeded"
    assert repository.query_candidate_state(terminal_latest_job["idempotency_key"])["slurm_job_id"] == "9009"
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_jobs"][0]["status"] == "succeeded"
    assert state["pipeline_status"] != "running"


def test_file_orchestration_journal_new_write_advances_beyond_latest_only_replay_sequence(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    latest_job = _active_job(cycle_time)
    latest_job.update(
        {
            "status": "failed",
            "slurm_job_id": "9009",
            "submitted_at": "2026-06-28T00:01:00Z",
            "finished_at": "2026-06-28T00:10:00Z",
            "updated_at": "2026-06-28T00:10:00Z",
        }
    )
    latest = _latest_view(cycle_time=cycle_time, jobs=[latest_job])
    latest["replay"]["latest_sequence"] = 10
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)
    new_job = dict(latest_job)
    new_job.update(
        {
            "status": "running",
            "slurm_job_id": "3011",
            "error_code": None,
            "error_message": None,
        }
    )

    written = repository.upsert_pipeline_job(new_job)

    journal_records = [
        json.loads(line)
        for line in (journal_root / "journal/gfs/2026062800.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    latest_after = json.loads((journal_root / "latest/gfs/2026062800/model_a.json").read_text(encoding="utf-8"))
    direct_after = json.loads((journal_root / f"pipeline-jobs/{latest_job['job_id']}.json").read_text(encoding="utf-8"))
    state = _candidate_state(repository, cycle_time=cycle_time)

    assert written["status"] == "running"
    assert journal_records[-1]["sequence"] == 11
    assert latest_after["replay"]["latest_sequence"] == 11
    assert latest_after["pipeline_jobs"][0]["status"] == "running"
    assert direct_after["sequence"] == 11
    assert direct_after["payload"]["status"] == "running"
    assert repository.get_pipeline_job(latest_job["job_id"])["status"] == "running"
    assert state is not None
    assert state["pipeline_jobs"][0]["status"] == "running"
    assert state["pipeline_status"] == "running"


def test_file_orchestration_journal_new_write_advances_beyond_alias_replay_sequence(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_stamp = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    stale_job = _active_job(cycle_time)
    stale_job.update(
        {
            "job_id": f"job_cycle_ifs_{cycle_stamp}_forecast",
            "idempotency_key": f"cycle_ifs_{cycle_stamp}:forecast",
            "run_id": f"fcst_ifs_{cycle_stamp}_model_a",
            "cycle_id": cycle_id_for("IFS", cycle_time),
            "source_id": "ifs",
            "status": "failed",
            "slurm_job_id": "9009",
            "submitted_at": "2026-06-28T00:01:00Z",
            "finished_at": "2026-06-28T00:10:00Z",
            "updated_at": "2026-06-28T00:10:00Z",
        }
    )
    latest = _latest_view(source_id="ifs", cycle_time=cycle_time, jobs=[stale_job])
    latest["forcing_version"] = None
    latest["replay"]["latest_sequence"] = 10
    _write_json(journal_root / "latest/ifs/2026062800/model_a.json", latest)
    _write_jsonl(
        journal_root / "journal/ifs/2026062800.jsonl",
        [
            _journal_record(
                record_type="pipeline_job",
                source_id="ifs",
                cycle_time=cycle_time,
                payload=stale_job,
                sequence=10,
            )
        ],
    )
    repository = FileOrchestrationJournalRepository(journal_root)
    new_job = dict(stale_job)
    new_job.update(
        {
            "source_id": "IFS",
            "status": "running",
            "slurm_job_id": "3011",
            "finished_at": None,
            "error_code": None,
            "error_message": None,
        }
    )

    written = repository.upsert_pipeline_job(new_job)

    journal_records = [
        json.loads(line)
        for line in (journal_root / "journal/IFS/2026062800.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    latest_after = json.loads((journal_root / "latest/IFS/2026062800/model_a.json").read_text(encoding="utf-8"))
    direct_after = json.loads((journal_root / f"pipeline-jobs/{stale_job['job_id']}.json").read_text(encoding="utf-8"))
    state = _candidate_state(repository, source_id="IFS", cycle_time=cycle_time)

    assert written["status"] == "running"
    assert journal_records[-1]["sequence"] == 11
    assert latest_after["replay"]["latest_sequence"] == 11
    assert latest_after["pipeline_jobs"][0]["source_id"] == "IFS"
    assert latest_after["pipeline_jobs"][0]["status"] == "running"
    assert direct_after["sequence"] == 11
    assert direct_after["payload"]["status"] == "running"
    assert repository.get_pipeline_job(stale_job["job_id"])["status"] == "running"
    assert state is not None
    assert state["pipeline_jobs"][0]["source_id"] == "IFS"
    assert state["pipeline_jobs"][0]["status"] == "running"
    assert state["pipeline_status"] == "running"


def test_file_orchestration_journal_candidate_state_includes_run_manifest_package_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    object_root = tmp_path / "object-store"
    manifest_key = "runs/fcst_gfs_2026062800_model_a/input/manifest.json"
    _write_json(
        object_root / manifest_key,
        {
            "model": {
                "model_package_uri": "s3://nhms/models/model_a/old/package/",
                "model_package_manifest_uri": "s3://nhms/models/model_a/old/manifest.json",
                "model_package_checksum": "old-package-sha",
            }
        },
    )
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    journal_root = tmp_path / "journal"
    latest = _latest_view(cycle_time=cycle_time, hydro_status="created")
    assert latest["hydro_run"] is not None
    latest["hydro_run"]["run_manifest_uri"] = f"s3://nhms/{manifest_key}"
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)

    state = _candidate_state(FileOrchestrationJournalRepository(journal_root), cycle_time=cycle_time)

    assert state is not None
    package = state["run_manifest_model_package"]
    assert package["status"] == "loaded"
    assert package["source"] == "run_manifest"
    assert package["model_package_uri_sha256"] == hashlib.sha256(
        b"s3://nhms/models/model_a/old/package/"
    ).hexdigest()
    assert package["model_package_manifest_uri_sha256"] == hashlib.sha256(
        b"s3://nhms/models/model_a/old/manifest.json"
    ).hexdigest()
    assert package["model_package_checksum"] == "old-package-sha"


def test_file_orchestration_journal_accepts_model_less_cycle_cohort_run_id(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    run_id = "cycle_gfs_2026062800_forcing"

    reserved = repository.reserve_pipeline_job(
        {
            "job_id": f"job_{run_id}_forcing",
            "run_id": run_id,
            "cycle_id": "gfs_2026062800",
            "source_id": "gfs",
            "cycle_time": cycle_time,
            "job_type": "produce_forcing_array",
            "stage": "forcing",
            "idempotency_key": f"{run_id}:forcing",
        }
    )

    assert reserved is not None
    assert reserved["model_id"] is None
    assert reserved["run_id"] == run_id
    assert repository.query_pipeline_jobs_by_run(run_id)[0]["status"] == "reserved"


@pytest.mark.parametrize(
    ("field_name", "envelope_value", "expected_reason"),
    [
        ("run_id", "fcst_gfs_2026062800_model_b", "file_journal_run_mismatch"),
        ("job_id", "job_cycle_gfs_2026062800_intruder", "file_journal_job_mismatch"),
    ],
)
def test_file_orchestration_journal_journal_envelope_payload_job_identity_mismatch_blocks_reads(
    tmp_path: Path,
    field_name: str,
    envelope_value: str,
    expected_reason: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    job.update({"status": "running", "slurm_job_id": "3001"})
    record = _journal_record(
        record_type="pipeline_job",
        source_id="gfs",
        cycle_time=cycle_time,
        payload=job,
        sequence=1,
    )
    record[field_name] = envelope_value
    _write_jsonl(journal_root / "journal/gfs/2026062800.jsonl", [record])
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_jobs"][0]["job_id"] == "file_journal_read_blocked"
    assert state["file_journal"]["reason"] == expected_reason
    assert state["file_journal"]["field"] == field_name

    query_by_job = repository.get_pipeline_job(job["job_id"])
    assert query_by_job is not None
    assert query_by_job["stage"] == "file_journal_read"
    assert query_by_job["error_code"] == expected_reason

    query_by_idempotency = repository.query_candidate_state(job["idempotency_key"])
    assert query_by_idempotency is not None
    assert query_by_idempotency["stage"] == "file_journal_read"
    assert query_by_idempotency["error_code"] == expected_reason


def test_file_orchestration_journal_direct_pipeline_job_envelope_payload_job_mismatch_blocks_read(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    record = _journal_record(
        record_type="pipeline_job",
        source_id="gfs",
        cycle_time=cycle_time,
        payload=job,
        sequence=1,
    )
    record["job_id"] = "job_cycle_gfs_2026062800_intruder"
    _write_json(journal_root / f"pipeline-jobs/{job['job_id']}.json", record)
    repository = FileOrchestrationJournalRepository(journal_root)

    query_by_job = repository.get_pipeline_job(job["job_id"])

    assert query_by_job is not None
    assert query_by_job["stage"] == "file_journal_read"
    assert query_by_job["error_code"] == "file_journal_job_mismatch"
    assert query_by_job["file_journal"]["field"] == "job_id"


@pytest.mark.parametrize(
    ("case_name", "expected_field"),
    [
        ("latest_model", "model_id"),
        ("nested_hydro", "model_id"),
    ],
)
def test_file_orchestration_journal_latest_identity_mismatch_blocks_reads(
    tmp_path: Path,
    case_name: str,
    expected_field: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    latest = _latest_view(cycle_time=cycle_time, hydro_status="complete", jobs=[_active_job(cycle_time)])
    if case_name == "latest_model":
        latest["model_id"] = "model_b"
    else:
        assert latest["hydro_run"] is not None
        latest["hydro_run"]["model_id"] = "model_b"
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["file_journal"]["reason"] == "file_journal_model_mismatch"
    assert state["file_journal"]["field"] == expected_field


@pytest.mark.parametrize(
    ("case_name", "expected_reason"),
    [
        ("envelope_payload_model", "file_journal_model_mismatch"),
        ("envelope_run_model", "file_journal_run_mismatch"),
    ],
)
def test_file_orchestration_journal_journal_model_identity_mismatch_blocks_before_sibling_skip(
    tmp_path: Path,
    case_name: str,
    expected_reason: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    job.update(
        {
            "run_id": "fcst_gfs_2026062800_model_a",
            "model_id": "model_a",
            "status": "queued",
        }
    )
    if case_name == "envelope_run_model":
        del job["model_id"]
    record = _journal_record(
        record_type="pipeline_job",
        source_id="gfs",
        cycle_time=cycle_time,
        payload=job,
        model_id="model_b",
    )
    _write_jsonl(journal_root / "journal/gfs/2026062800.jsonl", [record])
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["file_journal"]["reason"] == expected_reason
    assert state["file_journal"]["field"] in {"model_id", "run_id"}


@pytest.mark.parametrize(
    ("field_name", "value", "expected_field"),
    [
        ("pipeline_jobs", ["not-an-object"], "pipeline_jobs[0]"),
        ("pipeline_events", ["not-an-object"], "pipeline_events[0]"),
    ],
)
def test_file_orchestration_journal_non_object_embedded_rows_block(
    tmp_path: Path,
    field_name: str,
    value: list[str],
    expected_field: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    latest = _latest_view(cycle_time=cycle_time, jobs=[_active_job(cycle_time)])
    latest[field_name] = value
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)

    state = _candidate_state(FileOrchestrationJournalRepository(journal_root), cycle_time=cycle_time)

    assert state is not None
    assert state["file_journal"]["reason"] == "file_journal_expected_object"
    assert state["file_journal"]["field"] == expected_field


@pytest.mark.parametrize("surface", ["journal", "direct_pipeline_job", "sidecar_event"])
def test_file_orchestration_journal_non_object_payload_blocks_state_replay_surfaces(
    tmp_path: Path,
    surface: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    record = {
        "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
        "sequence": 1,
        "record_type": "pipeline_job",
        "source_id": "gfs",
        "cycle_time": cycle_time.isoformat(),
        "model_id": "model_a",
        "payload": ["not-an-object"],
    }
    if surface == "journal":
        _write_jsonl(journal_root / "journal/gfs/2026062800.jsonl", [record])
    elif surface == "direct_pipeline_job":
        _write_json(journal_root / f"pipeline-jobs/{job['job_id']}.json", record)
    else:
        _write_json(
            journal_root / "latest/gfs/2026062800/model_a.json",
            _latest_view(cycle_time=cycle_time, jobs=[job]),
        )
        event_record = {
            **record,
            "record_type": "pipeline_event",
        }
        _write_jsonl(journal_root / "pipeline-events/gfs/2026062800.jsonl", [event_record])
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "running"
    assert state["file_journal"]["reason"] == "file_journal_expected_object"
    assert state["file_journal"]["field"] == "payload"


@pytest.mark.parametrize("surface", ["direct_model", "direct_forcing"])
def test_file_orchestration_journal_non_object_payload_blocks_direct_context_records(
    tmp_path: Path,
    surface: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    if surface == "direct_model":
        _write_json(
            journal_root / "models/model_a.json",
            {
                "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
                "record_type": "model_context",
                "model_id": "model_a",
                "payload": ["not-an-object"],
            },
        )
        with pytest.raises(OrchestratorError) as error:
            FileOrchestrationJournalRepository(journal_root).load_model_context("model_a")
    else:
        _write_json(
            journal_root / "forcing/gfs/2026062800/model_a.json",
            {
                "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
                "record_type": "forcing_version",
                "source_id": "gfs",
                "cycle_time": cycle_time.isoformat(),
                "model_id": "model_a",
                "payload": ["not-an-object"],
            },
        )
        with pytest.raises(OrchestratorError) as error:
            FileOrchestrationJournalRepository(journal_root).find_forcing_context(
                source_id="gfs",
                cycle_time=cycle_time,
                model_id="model_a",
            )

    assert error.value.error_code == "FILE_JOURNAL_READ_BLOCKED"
    assert "file_journal_expected_object" in error.value.message


def test_file_orchestration_journal_direct_pipeline_job_requires_journal_schema(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    _write_json(journal_root / f"pipeline-jobs/{job['job_id']}.json", job)

    query = FileOrchestrationJournalRepository(journal_root).get_pipeline_job(job["job_id"])

    assert query is not None
    assert query["status"] == "running"
    assert query["error_code"] == "file_journal_schema_mismatch"


def test_file_orchestration_journal_valid_direct_only_custom_pipeline_job_is_read(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    job.update(
        {
            "job_id": "custom_safe_job",
            "idempotency_key": "custom-idempotency",
            "status": "running",
            "slurm_job_id": "7777",
        }
    )
    _write_json(
        journal_root / "pipeline-jobs/custom_safe_job.json",
        _journal_record(record_type="pipeline_job", source_id="gfs", cycle_time=cycle_time, payload=job),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    assert repository.active_slurm_jobs(source_id="gfs", cycle_time=cycle_time, model_id="model_a")[0][
        "job_id"
    ] == "custom_safe_job"
    assert repository.get_pipeline_job("custom_safe_job")["slurm_job_id"] == "7777"
    assert repository.query_candidate_state("custom-idempotency")["job_id"] == "custom_safe_job"
    assert repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))[0]["job_id"] == "custom_safe_job"
    assert repository.query_pipeline_jobs_by_run(job["run_id"])[0]["job_id"] == "custom_safe_job"
    assert repository.query_pipeline_job_by_slurm_id("7777")["job_id"] == "custom_safe_job"
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "running"
    assert state["pipeline_jobs"][0]["job_id"] == "custom_safe_job"


def test_file_orchestration_journal_cycle_level_direct_jobs_ignore_unrelated_valid_snapshots(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    unrelated_cycle_time = _dt("2026-06-28T12:00:00Z")
    journal_root = tmp_path / "journal"
    completed_job = _active_job(cycle_time)
    completed_job.update({"status": "succeeded", "slurm_job_id": "3001"})
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="complete", jobs=[completed_job]),
    )
    unrelated_job = _active_job(unrelated_cycle_time)
    unrelated_job.update(
        {
            "job_id": "job_cycle_ifs_2026062812_forecast",
            "idempotency_key": "cycle_ifs_2026062812:forecast",
            "run_id": "cycle_ifs_2026062812",
            "cycle_id": cycle_id_for("ifs", unrelated_cycle_time),
            "source_id": "ifs",
            "cycle_time": unrelated_cycle_time.isoformat(),
            "status": "running",
            "slurm_job_id": "9999",
        }
    )
    _write_json(
        journal_root / "pipeline-jobs/job_cycle_ifs_2026062812_forecast.json",
        _journal_record(
            record_type="pipeline_job",
            source_id="ifs",
            cycle_time=unrelated_cycle_time,
            payload=unrelated_job,
            model_id=None,
        ),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_orchestration(source_id="gfs", cycle_time=cycle_time) is False
    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "succeeded"


def test_file_orchestration_journal_get_pipeline_job_exact_direct_path_ignores_unrelated_bad_direct_snapshot(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    job.update({"job_id": "custom_safe_job", "idempotency_key": "custom-idempotency"})
    _write_json(
        journal_root / "pipeline-jobs/custom_safe_job.json",
        _journal_record(record_type="pipeline_job", source_id="gfs", cycle_time=cycle_time, payload=job),
    )
    bad_direct = journal_root / "pipeline-jobs/unrelated_bad_snapshot.json"
    bad_direct.parent.mkdir(parents=True, exist_ok=True)
    bad_direct.write_text("{not-json", encoding="utf-8")

    assert FileOrchestrationJournalRepository(journal_root).get_pipeline_job("custom_safe_job")["job_id"] == (
        "custom_safe_job"
    )


def test_file_orchestration_journal_scoped_direct_snapshot_discovery_fails_closed_on_malformed_present_evidence(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    scoped_job = _active_job(cycle_time)
    scoped_job["status"] = "succeeded"
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="complete", jobs=[scoped_job]),
    )
    unrelated_direct = journal_root / "pipeline-jobs/job_cycle_ifs_2026062812_forecast.json"
    unrelated_direct.parent.mkdir(parents=True)
    unrelated_direct.write_text("{not-json", encoding="utf-8")

    repository = FileOrchestrationJournalRepository(journal_root)

    # #1734 D2a + D9: the flat direct surface is filtered by file name on BOTH
    # readers, and this file's name resolves to IFS/2026062812 -- a different
    # cycle -- so a GFS lookup neither opens it nor is blocked by it. Round 1
    # filtered only the narrowed reader, which is why this test previously
    # pinned ``has_active_pipeline`` as fail-closed here; D9 moved the scoped
    # direct reader onto the same one filter definition, so a foreign cycle's
    # corruption can no longer wedge this cycle on either path.
    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert "file_journal" not in state or state["file_journal"].get("reason") is None
    query = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
    assert [row["job_id"] for row in query] == [scoped_job["job_id"]]
    assert "error_code" not in query[0]

    # The whole-tree fail-closed property keeps a home on the fall-open path.
    blocked = repository.query_pipeline_jobs_by_cycle("unknown-source_2026062800")
    assert blocked[0]["error_code"] == "file_journal_malformed_json"
    assert blocked[0]["file_journal"]["field"] == "pipeline-jobs/job_cycle_ifs_2026062812_forecast.json"

    # Fail-closed keeps its home on the scoped reader too: a malformed flat
    # file whose name belongs to THIS cycle still blocks it. Only the foreign
    # cycle's file stopped counting, which is equivalence, not a silent drop.
    own_cycle_direct = journal_root / "pipeline-jobs/job_cycle_gfs_2026062800_forecast.json"
    own_cycle_direct.write_text("{not-json", encoding="utf-8")
    reopened = FileOrchestrationJournalRepository(journal_root)
    assert reopened.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    own_state = _candidate_state(reopened, cycle_time=cycle_time)
    assert own_state is not None
    assert own_state["pipeline_status"] == "running"
    assert own_state["file_journal"]["reason"] == "file_journal_malformed_json"
    assert own_state["file_journal"]["field"] == "pipeline-jobs/job_cycle_gfs_2026062800_forecast.json"


@pytest.mark.parametrize("authoritative_surface", ["latest", "journal"])
def test_file_orchestration_journal_direct_terminal_job_cannot_mask_active_replay(
    tmp_path: Path,
    authoritative_surface: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    direct_job = _active_job(cycle_time)
    direct_job["status"] = "succeeded"
    direct_job["slurm_job_id"] = "9009"
    direct_job["updated_at"] = "2026-06-28T00:05:00Z"
    latest_job = dict(direct_job)
    latest_job["status"] = "running"
    latest_job["slurm_job_id"] = "3001"
    latest_job["updated_at"] = "2026-06-28T00:03:00Z"
    _write_json(
        journal_root / f"pipeline-jobs/{direct_job['job_id']}.json",
        _journal_record(record_type="pipeline_job", source_id="gfs", cycle_time=cycle_time, payload=direct_job),
    )
    if authoritative_surface == "latest":
        _write_json(
            journal_root / "latest/gfs/2026062800/model_a.json",
            _latest_view(cycle_time=cycle_time, jobs=[latest_job]),
        )
    else:
        _write_jsonl(
            journal_root / "journal/gfs/2026062800.jsonl",
            [
                _journal_record(
                    record_type="pipeline_job",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    payload=latest_job,
                )
            ],
        )

    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is False
    active = repository.active_slurm_jobs(source_id="gfs", cycle_time=cycle_time, model_id="model_a")
    assert [job["slurm_job_id"] for job in active] == ["3001"]

    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "running"
    assert state["pipeline_jobs_total"] == 1
    assert state["pipeline_jobs"][0]["slurm_job_id"] == "3001"

    query = repository.get_pipeline_job(direct_job["job_id"])
    assert query is not None
    assert query["status"] == "running"
    assert query["slurm_job_id"] == "3001"


def test_file_orchestration_journal_model_scoped_journal_replay_ignores_sibling_singletons(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    model_a_forcing = {
        "forcing_version_id": "forc_gfs_2026062800_model_a",
        "forcing_package_uri": "s3://nhms/forcing/model-a.tar",
        "source_id": "gfs",
        "cycle_time": cycle_time.isoformat(),
        "model_id": "model_a",
        "max_lead_hours": 3,
    }
    model_b_forcing = {
        "forcing_version_id": "forc_gfs_2026062800_model_b",
        "forcing_package_uri": "s3://nhms/forcing/model-b.tar",
        "source_id": "gfs",
        "cycle_time": cycle_time.isoformat(),
        "model_id": "model_b",
        "max_lead_hours": 9,
    }
    model_b_hydro = {
        "run_id": "fcst_gfs_2026062800_model_b",
        "source_id": "gfs",
        "cycle_time": cycle_time.isoformat(),
        "model_id": "model_b",
        "status": "created",
        "updated_at": "2026-06-28T00:05:00Z",
    }
    _write_jsonl(
        journal_root / "journal/gfs/2026062800.jsonl",
        [
            _journal_record(
                record_type="forcing_version",
                source_id="gfs",
                cycle_time=cycle_time,
                payload=model_a_forcing,
                sequence=1,
                model_id="model_a",
            ),
            _journal_record(
                record_type="forcing_version",
                source_id="gfs",
                cycle_time=cycle_time,
                payload=model_b_forcing,
                sequence=2,
                model_id="model_b",
            ),
            {
                "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
                "sequence": 3,
                "record_type": "future_model_b_state",
                "source_id": "gfs",
                "cycle_time": cycle_time.isoformat(),
                "model_id": "model_b",
                "payload": {"model_id": "model_b"},
            },
            _journal_record(
                record_type="hydro_run",
                source_id="gfs",
                cycle_time=cycle_time,
                payload=model_b_hydro,
                sequence=4,
                model_id="model_b",
            ),
        ],
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    forcing = repository.find_forcing_context(source_id="gfs", cycle_time=cycle_time, model_id="model_a")
    state = _candidate_state(repository, cycle_time=cycle_time)

    assert forcing.forcing_version_id == "forc_gfs_2026062800_model_a"
    assert forcing.max_lead_hours == 3
    assert state is not None
    assert state["forcing_version"]["forcing_version_id"] == "forc_gfs_2026062800_model_a"
    assert state["hydro_status"] is None


def test_file_orchestration_journal_model_scoped_pipeline_job_requires_run_id(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    del job["run_id"]
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[job]),
    )

    state = _candidate_state(FileOrchestrationJournalRepository(journal_root), cycle_time=cycle_time)

    assert state is not None
    assert state["file_journal"]["reason"] == "file_journal_missing_identity"
    assert state["file_journal"]["field"] == "run_id"


@pytest.mark.parametrize(
    ("record_override", "expected_reason"),
    [
        ({"schema_version": "wrong"}, "file_journal_schema_mismatch"),
        ({"cycle_time": "2026-06-28T12:00:00Z"}, "file_journal_cycle_mismatch"),
    ],
)
def test_file_orchestration_journal_sidecar_pipeline_events_validate_schema_and_cycle(
    tmp_path: Path,
    record_override: Mapping[str, Any],
    expected_reason: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[job]),
    )
    event = {
        "event_id": 1,
        "entity_type": "pipeline_job",
        "entity_id": job["job_id"],
        "event_type": "status_change",
        "created_at": "2026-06-28T00:01:00Z",
    }
    record = _journal_record(record_type="pipeline_event", source_id="gfs", cycle_time=cycle_time, payload=event)
    record.update(record_override)
    _write_jsonl(journal_root / "pipeline-events/gfs/2026062800.jsonl", [record])

    state = _candidate_state(FileOrchestrationJournalRepository(journal_root), cycle_time=cycle_time)

    assert state is not None
    assert state["file_journal"]["reason"] == expected_reason


@pytest.mark.parametrize("surface", ["latest", "journal"])
def test_file_orchestration_journal_unknown_path_source_blocks_query_helpers(
    tmp_path: Path,
    surface: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    if surface == "latest":
        _write_json(
            journal_root / "latest/unknown-source/2026062800/model_a.json",
            {
                "schema_version": FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
                "source_id": "unknown-source",
                "cycle_time": cycle_time.isoformat(),
                "model_id": "model_a",
                "pipeline_jobs": [],
            },
        )
    else:
        _write_jsonl(
            journal_root / "journal/unknown-source/2026062800.jsonl",
            [
                {
                    "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
                    "sequence": 1,
                    "record_type": "pipeline_job",
                    "source_id": "unknown-source",
                    "cycle_time": cycle_time.isoformat(),
                    "payload": _active_job(cycle_time),
                }
            ],
        )

    # #1734: a derivable cycle id no longer walks foreign source directories at
    # all, so the probe uses an underivable one. That still enters this same
    # public entrypoint and still reaches the whole-tree scan, via the D4
    # fall-open path production itself relies on.
    repository = FileOrchestrationJournalRepository(journal_root)
    assert journal_module._cycle_scope_from_cycle_id("unknown-source_2026062800") is None
    query = repository.query_pipeline_jobs_by_cycle("unknown-source_2026062800")

    assert query[0]["status"] == "running"
    assert query[0]["stage"] == "file_journal_read"
    assert query[0]["error_code"] == "file_journal_invalid_identity"
    assert query[0]["file_journal"]["field"] == "source_id"

    # And the narrowed lookup for a real cycle is not blocked by that foreign
    # directory -- the decoupling this change requires.
    assert repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time)) == []


@pytest.mark.parametrize("surface", ["latest", "journal", "direct"])
def test_file_orchestration_journal_unknown_record_source_blocks_candidate_and_query_helpers(
    tmp_path: Path,
    surface: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job = _active_job(cycle_time)
    if surface == "latest":
        latest = _latest_view(cycle_time=cycle_time, jobs=[job])
        latest["source_id"] = "unknown-source"
        _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    elif surface == "journal":
        record = _journal_record(record_type="pipeline_job", source_id="gfs", cycle_time=cycle_time, payload=job)
        record["source_id"] = "unknown-source"
        _write_jsonl(journal_root / "journal/gfs/2026062800.jsonl", [record])
    else:
        record = _journal_record(record_type="pipeline_job", source_id="gfs", cycle_time=cycle_time, payload=job)
        record["source_id"] = "unknown-source"
        _write_json(journal_root / f"pipeline-jobs/{job['job_id']}.json", record)

    repository = FileOrchestrationJournalRepository(journal_root)
    state = _candidate_state(repository, cycle_time=cycle_time)
    query = repository.get_pipeline_job(job["job_id"])

    assert state is not None
    assert state["pipeline_status"] == "running"
    assert state["file_journal"]["reason"] == "file_journal_invalid_identity"
    assert state["file_journal"]["field"] == "source_id"
    assert query is not None
    assert query["status"] == "running"
    assert query["stage"] == "file_journal_read"
    assert query["error_code"] == "file_journal_invalid_identity"


@pytest.mark.parametrize("surface", ["latest", "journal"])
def test_file_orchestration_journal_invalid_cycle_time_blocks_without_raw_exception(
    tmp_path: Path,
    surface: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    if surface == "latest":
        latest = _latest_view(cycle_time=cycle_time, jobs=[_active_job(cycle_time)])
        latest["cycle_time"] = "not-a-cycle"
        _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    else:
        _write_jsonl(
            journal_root / "journal/gfs/2026062800.jsonl",
            [
                {
                    **_journal_record(
                        record_type="pipeline_job",
                        source_id="gfs",
                        cycle_time=cycle_time,
                        payload=_active_job(cycle_time),
                    ),
                    "cycle_time": "not-a-cycle",
                }
            ],
        )

    state = _candidate_state(FileOrchestrationJournalRepository(journal_root), cycle_time=cycle_time)

    assert state is not None
    assert state["file_journal"]["reason"] == "file_journal_invalid_cycle_time"
    assert state["file_journal"]["field"] == "cycle_time"


@pytest.mark.parametrize(
    ("case_name", "expected_reason"),
    [
        ("symlink", "file_journal_unsafe_scanned_entry"),
        ("unsafe_name", "file_journal_unsafe_path_segment"),
    ],
)
def test_file_orchestration_journal_scanned_unsafe_entries_block_queries(
    tmp_path: Path,
    case_name: str,
    expected_reason: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    latest_dir = journal_root / "latest/gfs/2026062800"
    latest_dir.mkdir(parents=True)
    if case_name == "symlink":
        target = tmp_path / "target.json"
        target.write_text("{}", encoding="utf-8")
        (latest_dir / "model_a.json").symlink_to(target)
    else:
        (latest_dir / "bad name.json").write_text("{}", encoding="utf-8")

    query = FileOrchestrationJournalRepository(journal_root).query_pipeline_jobs_by_cycle(
        cycle_id_for("gfs", cycle_time)
    )

    assert query[0]["error_code"] == expected_reason


@pytest.mark.parametrize(
    ("case_name", "expected_reason"),
    [
        ("file_count", "file_journal_file_limit_exceeded"),
        ("depth", "file_journal_depth_limit_exceeded"),
        ("json_nodes", "file_journal_json_node_limit_exceeded"),
        ("json_depth", "file_journal_json_depth_exceeded"),
    ],
)
def test_file_orchestration_journal_resource_limits_fail_closed(
    tmp_path: Path,
    case_name: str,
    expected_reason: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[_active_job(cycle_time)]),
    )
    if case_name == "file_count":
        _write_json(
            journal_root / "latest/gfs/2026062800/model_b.json",
            _latest_view(cycle_time=cycle_time, model_id="model_b", jobs=[_active_job(cycle_time, model_id="model_b")]),
        )
        repository = FileOrchestrationJournalRepository(journal_root, max_files=1)
        query = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
        assert query[0]["error_code"] == expected_reason
        return
    if case_name == "depth":
        # #1734: the depth budget is still enforced on the narrowed walk, but
        # it is now measured from the cycle directory rather than from
        # ``latest/``. Nest below the cycle directory so the walk recurses, and
        # move the budget accordingly.
        (journal_root / "latest/gfs/2026062800/nested").mkdir(parents=True, exist_ok=True)
        repository = FileOrchestrationJournalRepository(journal_root, max_depth=0)
        query = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
        assert query[0]["error_code"] == expected_reason
        # `error_code` alone cannot discriminate: max_depth=0 trips one level
        # below whatever the walk root is. The reported path is what pins WHERE
        # the budget fired -- from ``latest/`` it would fire at ``latest/gfs``.
        assert query[0]["file_journal"]["field"] == "latest/gfs/2026062800/nested"
        assert query[0]["file_journal"]["evidence"] == {"max_depth": 0}
        return
    if case_name == "json_nodes":
        repository = FileOrchestrationJournalRepository(journal_root, max_json_nodes=2)
    else:
        repository = FileOrchestrationJournalRepository(journal_root, max_json_depth=2)

    state = _candidate_state(repository, cycle_time=cycle_time)

    assert state is not None
    assert state["file_journal"]["reason"] == expected_reason


def test_file_orchestration_journal_non_matching_directory_entries_count_toward_scan_limit(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    latest_dir = journal_root / "latest/gfs/2026062800"
    latest_dir.mkdir(parents=True)
    (latest_dir / "note_one.txt").write_text("not json", encoding="utf-8")
    (latest_dir / "note_two.txt").write_text("not json", encoding="utf-8")

    # #1734: non-matching entries still count toward the scan budget; the walk
    # root moved from ``latest/`` to the cycle directory, so the budget that
    # trips on these two entries moved with it.
    query = FileOrchestrationJournalRepository(journal_root, max_files=1).query_pipeline_jobs_by_cycle(
        cycle_id_for("gfs", cycle_time)
    )

    assert query[0]["error_code"] == "file_journal_file_limit_exceeded"
    assert query[0]["file_journal"]["field"] == "latest/gfs/2026062800"


def test_file_orchestration_journal_oversized_non_matching_directory_listing_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    # #1734: the narrowed walk enters at the cycle directory, so that is the
    # directory whose listing must stay bounded.
    (journal_root / "latest/gfs/2026062800").mkdir(parents=True)
    max_files = 3
    consumed_entries = 0

    class LazyScandir:
        def __enter__(self) -> LazyScandir:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def __iter__(self) -> LazyScandir:
            return self

        def __next__(self) -> object:
            nonlocal consumed_entries
            consumed_entries += 1
            if consumed_entries > max_files + 1:
                raise AssertionError("directory listing consumed beyond the bounded sentinel")
            return type("Entry", (), {"name": f"note_{consumed_entries:04d}.txt"})()

    def fake_scandir(_fd: int) -> LazyScandir:
        return LazyScandir()

    monkeypatch.setattr(safe_fs.os, "scandir", fake_scandir)

    query = FileOrchestrationJournalRepository(journal_root, max_files=max_files).query_pipeline_jobs_by_cycle(
        cycle_id_for("gfs", cycle_time)
    )

    assert consumed_entries == max_files + 1
    assert query[0]["error_code"] == "file_journal_file_limit_exceeded"
    assert query[0]["file_journal"]["field"] == "latest/gfs/2026062800"


def test_file_orchestration_journal_pipeline_query_has_aggregate_record_budget(
    tmp_path: Path,
) -> None:
    first_cycle_time = _dt("2026-06-28T00:00:00Z")
    second_cycle_time = _dt("2026-06-28T12:00:00Z")
    journal_root = tmp_path / "journal"
    _write_jsonl(
        journal_root / "journal/gfs/2026062800.jsonl",
        [
            _journal_record(
                record_type="pipeline_job",
                source_id="gfs",
                cycle_time=first_cycle_time,
                payload=_active_job(first_cycle_time),
            )
        ],
    )
    _write_jsonl(
        journal_root / "journal/gfs/2026062812.jsonl",
        [
            _journal_record(
                record_type="pipeline_job",
                source_id="gfs",
                cycle_time=second_cycle_time,
                payload=_active_job(second_cycle_time),
            )
        ],
    )

    repository = FileOrchestrationJournalRepository(journal_root, max_records=1)

    # #1734: the aggregate record budget guards the WHOLE-TREE replay, and a
    # derivable cycle id no longer reaches it. The property is kept through the
    # same public entrypoint with an underivable cycle id, which falls open to
    # the full scan (D4) and there still sees both cycles' records.
    assert journal_module._cycle_scope_from_cycle_id("unknown-source_2026062800") is None
    blocked = repository.query_pipeline_jobs_by_cycle("unknown-source_2026062800")

    assert blocked[0]["error_code"] == "file_journal_record_limit_exceeded"
    assert blocked[0]["file_journal"]["field"] == "pipeline_job_records"

    # The narrowed half, asserted so the pair is self-discriminating: the same
    # budget over the narrowed read sees one cycle's single record, does not
    # trip, and the real row comes back. Re-widening this lookup would make the
    # budget trip here too and turn this row into a blocked row.
    narrowed = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", first_cycle_time))

    assert [job["job_id"] for job in narrowed] == [_active_job(first_cycle_time)["job_id"]]
    assert "error_code" not in narrowed[0]
    assert "file_journal" not in narrowed[0]


def test_file_orchestration_journal_equal_timestamp_tiebreak_matches_db_ordering(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    job_a = _active_job(cycle_time)
    job_a.update({"job_id": "job_a", "updated_at": "2026-06-28T00:03:00Z", "created_at": "2026-06-28T00:00:00Z"})
    job_z = dict(job_a)
    job_z["job_id"] = "job_z"
    events = [
        {
            "event_id": 1,
            "entity_type": "pipeline_job",
            "entity_id": "job_z",
            "event_type": "status_change",
            "created_at": "2026-06-28T00:04:00Z",
        },
        {
            "event_id": 2,
            "entity_type": "pipeline_job",
            "entity_id": "job_z",
            "event_type": "status_change",
            "created_at": "2026-06-28T00:04:00Z",
        },
    ]
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[job_a, job_z], events=events),
    )

    state = _candidate_state(
        FileOrchestrationJournalRepository(journal_root),
        cycle_time=cycle_time,
        job_limit=1,
        event_limit=1,
    )

    assert state is not None
    assert [job["job_id"] for job in state["pipeline_jobs"]] == ["job_z"]
    assert [event["event_id"] for event in state["pipeline_events"]] == [2]


def test_file_orchestration_journal_active_slurm_jobs_sorts_null_submitted_at_after_limit(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    null_submitted = _active_job(cycle_time)
    null_submitted.update(
        {
            "job_id": "job_null_submitted",
            "slurm_job_id": "3002",
            "submitted_at": None,
            "created_at": "2026-06-28T00:00:00Z",
            "status": "running",
        }
    )
    submitted = _active_job(cycle_time)
    submitted.update(
        {
            "job_id": "job_submitted",
            "slurm_job_id": "3001",
            "submitted_at": "2026-06-28T00:05:00Z",
            "created_at": "2026-06-28T00:10:00Z",
            "status": "running",
        }
    )
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[null_submitted, submitted]),
    )

    jobs = FileOrchestrationJournalRepository(journal_root).active_slurm_jobs(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_a",
        limit=1,
    )

    assert [job["job_id"] for job in jobs] == ["job_submitted"]


def test_file_orchestration_journal_query_pipeline_jobs_match_db_ordering(
    tmp_path: Path,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    early = _active_job(cycle_time)
    early.update(
        {
            "job_id": "job_early",
            "submitted_at": "2026-06-28T00:01:00Z",
            "created_at": "2026-06-28T00:09:00Z",
        }
    )
    later = _active_job(cycle_time)
    later.update(
        {
            "job_id": "job_later",
            "submitted_at": "2026-06-28T00:02:00Z",
            "created_at": "2026-06-28T00:00:00Z",
        }
    )
    tie_b = _active_job(cycle_time)
    tie_b.update(
        {
            "job_id": "job_tie_b",
            "submitted_at": "2026-06-28T00:03:00Z",
            "created_at": "2026-06-28T00:04:00Z",
        }
    )
    tie_a = _active_job(cycle_time)
    tie_a.update(
        {
            "job_id": "job_tie_a",
            "submitted_at": "2026-06-28T00:03:00Z",
            "created_at": "2026-06-28T00:04:00Z",
        }
    )
    null_submitted = _active_job(cycle_time)
    null_submitted.update({"job_id": "job_null", "submitted_at": None, "created_at": "2026-06-28T00:00:00Z"})
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[null_submitted, tie_b, later, tie_a, early]),
    )
    repository = FileOrchestrationJournalRepository(journal_root)
    expected_order = ["job_early", "job_later", "job_tie_a", "job_tie_b", "job_null"]

    assert [
        job["job_id"] for job in repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
    ] == expected_order
    assert [job["job_id"] for job in repository.query_pipeline_jobs_by_run(early["run_id"])] == expected_order


def test_file_orchestration_journal_blocked_query_sentinels_redact_raw_ids(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        {"schema_version": "wrong", "source_id": "gfs", "cycle_time": cycle_time.isoformat(), "model_id": "model_a"},
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    payload = {
        "job": repository.get_pipeline_job("/secret/job"),
        "idempotency": repository.query_candidate_state("file:///secret/idempotency"),
        "cycle": repository.query_pipeline_jobs_by_cycle("s3://bucket/secret-cycle"),
        "run": repository.query_pipeline_jobs_by_run("published://secret/run"),
        "slurm": repository.query_pipeline_job_by_slurm_id("/secret/slurm"),
    }
    rendered = json.dumps(payload, sort_keys=True)

    assert "/secret" not in rendered
    assert "file://" not in rendered
    assert "s3://bucket" not in rendered
    assert "published://" not in rendered
    assert "[local-path]" in rendered
    assert "[uri]" in rendered or "[object-uri]" in rendered


@pytest.mark.parametrize(
    ("model_id", "expected_token"),
    [
        ("/private/model-a", "[local-path]"),
        ("file:///private/model-a", "[uri]"),
    ],
)
def test_file_orchestration_journal_active_slurm_blocked_sentinel_redacts_model_id(
    tmp_path: Path,
    model_id: str,
    expected_token: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    jobs = FileOrchestrationJournalRepository(tmp_path / "journal").active_slurm_jobs(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id=model_id,
    )
    rendered = json.dumps(jobs, sort_keys=True)

    assert jobs[0]["job_id"] == "file_journal_read_blocked"
    assert jobs[0]["model_id"] == expected_token
    assert "/private" not in rendered
    assert "file://" not in rendered


def test_file_orchestration_journal_candidate_state_blocker_redacts_public_evidence(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        {
            "schema_version": "s3://private-bucket/schema",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "model_id": "model_a",
        },
    )

    state = _candidate_state(FileOrchestrationJournalRepository(journal_root), cycle_time=cycle_time)
    rendered = json.dumps(state, sort_keys=True)

    assert state is not None
    assert state["file_journal"]["reason"] == "file_journal_schema_mismatch"
    assert state["file_journal"]["evidence"]["actual"] == "[object-uri]"
    assert "s3://private-bucket" not in rendered


def test_db_free_scheduler_from_env_uses_file_journal_without_database_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path)
    cycle_time = _dt("2026-06-28T00:00:00Z")
    _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=(0, 3),
        model=_model("model_a", "basin_a"),
    )
    _enable_db_free_nfs_raw_manifest(monkeypatch, roots, cycle_time=cycle_time)
    _write_json(
        paths["NHMS_SCHEDULER_JOURNAL_ROOT"] / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="complete"),
    )
    _write_json(
        roots["object_store_root"] / "runs/fcst_gfs_2026062800_model_a/input/manifest.json",
        {
            "initial_state": {
                "quality": "fresh",
                "state_id": "state_gfs_model_a_2026062800_gfs_2026062718_f006",
            }
        },
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def fail_db_factory(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("DB-backed repository factory must not be called in DB-free read-side construction")

    monkeypatch.setattr(scheduler_module, "_active_repository_from_env", fail_db_factory)
    monkeypatch.setattr(scheduler_module, "_orchestrator_repository_from_env", fail_db_factory)

    scheduler = ProductionScheduler.from_env(
        ProductionSchedulerConfig(
            now=cycle_time,
            dry_run=True,
            lookback_hours=0,
            cycle_lag_hours=0,
            max_cycles_per_source=1,
        )
    )
    assert isinstance(scheduler.active_repository, FileOrchestrationJournalRepository)
    scheduler.adapters = {"gfs": FakeAdapter("gfs", [(cycle_time.isoformat(), True)])}

    result = scheduler.run_once()

    assert result.status == "planned"
    assert result.evidence["counts"]["submitted_count"] == 0
    # The journal-backed candidate-state provider now resolves the completed
    # run into a durable terminal skip instead of the provider-less
    # completed_duplicate_pipeline early exit.
    assert result.evidence["skipped_candidates"][0]["reason"] == "terminal_hydro_success"
    assert result.evidence["skipped_candidates"][0]["state_evidence"]["decision"] == "skip_terminal"


def test_db_free_scheduler_from_env_run_once_uses_file_journal_active_slurm_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path)
    cycle_time = _dt("2026-06-28T00:00:00Z")
    _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=(0, 3),
        model=_model("model_a", "basin_a"),
    )
    _enable_db_free_nfs_raw_manifest(monkeypatch, roots, cycle_time=cycle_time)
    _write_json(
        paths["NHMS_SCHEDULER_JOURNAL_ROOT"] / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[_active_job(cycle_time)]),
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        scheduler_module,
        "_default_adapters",
        lambda: {"gfs": FakeAdapter("gfs", [(cycle_time.isoformat(), True)])},
    )

    def fail_db_factory(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("DB-backed repository factory must not be called in DB-free read-side construction")

    monkeypatch.setattr(scheduler_module, "_active_repository_from_env", fail_db_factory)
    monkeypatch.setattr(scheduler_module, "_orchestrator_repository_from_env", fail_db_factory)

    result = ProductionScheduler.from_env(
        ProductionSchedulerConfig(
            now=cycle_time,
            dry_run=True,
            lookback_hours=0,
            cycle_lag_hours=0,
            max_cycles_per_source=1,
        )
    ).run_once()

    assert result.status == "planned"
    assert result.evidence["counts"]["submitted_count"] == 0
    assert result.evidence["skipped_candidates"][0]["reason"] == "active_slurm_job"
    assert result.evidence["skipped_candidates"][0]["active_slurm_jobs"][0]["slurm_job_id"] == "3001"


def test_db_free_scheduler_status_sync_blocks_without_default_db_orchestrator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path)
    cycle_time = _dt("2026-06-28T00:00:00Z")
    _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=(0, 3),
        model=_model("model_a", "basin_a"),
    )
    _enable_db_free_nfs_raw_manifest(monkeypatch, roots, cycle_time=cycle_time)
    _write_json(
        paths["NHMS_SCHEDULER_JOURNAL_ROOT"] / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[_active_job(cycle_time)]),
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        scheduler_module,
        "_default_adapters",
        lambda: {"gfs": FakeAdapter("gfs", [(cycle_time.isoformat(), True)])},
    )

    def fail_db_orchestrator_factory(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("DB-free status sync must not construct the default DB-backed orchestrator")

    monkeypatch.setattr(scheduler_module, "_orchestrator_repository_from_env", fail_db_orchestrator_factory)
    monkeypatch.setattr(
        "services.orchestrator.chain.HttpSlurmGatewayClient",
        lambda _url: _FailingSlurmGatewayClient(status_error_code="SLURM_STATUS_SYNC_FAILED"),
    )

    result = ProductionScheduler.from_env(
        ProductionSchedulerConfig(
            now=cycle_time,
            dry_run=False,
            lookback_hours=0,
            cycle_lag_hours=0,
            max_cycles_per_source=1,
            restart_reconcile_enabled=False,
        )
    ).run_once()

    assert result.status == "slurm_status_sync_failed"
    assert result.evidence["skipped_candidates"][0]["reason"] == "active_slurm_status_sync_failed"
    sync_evidence = result.evidence["skipped_candidates"][0]["state_evidence"]["slurm_state_sync"]
    assert sync_evidence["error_code"] == "SLURM_STATUS_SYNC_FAILED"
    assert sync_evidence["sync_attempted"] is True
    assert sync_evidence["sync_called"] is True
    assert sync_evidence["status"] == "failed"
    assert result.evidence["slurm_status_sync_proof"]["status"] == "failed"
    assert result.evidence["slurm_status_sync_proof"]["sync_called"] is True
    assert result.evidence["slurm_status_sync_proof"]["protected_by_pre_execution_evidence"] is True
    assert result.evidence["no_mutation_proof"]["slurm_status_sync_called"] is True
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is False


def test_db_free_scheduler_cancel_blocks_without_default_db_orchestrator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots, paths = _set_db_free_scheduler_env(monkeypatch, tmp_path)
    cycle_time = _dt("2026-06-28T00:00:00Z")
    _write_db_free_file_provider_fixtures(
        monkeypatch,
        roots,
        paths,
        cycle_time=cycle_time,
        forecast_hours=(0, 3),
        model=_model("model_a", "basin_a"),
    )
    _enable_db_free_nfs_raw_manifest(monkeypatch, roots, cycle_time=cycle_time)
    _write_json(
        paths["NHMS_SCHEDULER_JOURNAL_ROOT"] / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[_active_job(cycle_time)]),
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        scheduler_module,
        "_default_adapters",
        lambda: {"gfs": FakeAdapter("gfs", [(cycle_time.isoformat(), True)])},
    )

    def fail_db_orchestrator_factory(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("DB-free cancellation must not construct the default DB-backed orchestrator")

    monkeypatch.setattr(scheduler_module, "_orchestrator_repository_from_env", fail_db_orchestrator_factory)
    monkeypatch.setattr(
        "services.orchestrator.chain.HttpSlurmGatewayClient",
        lambda _url: _FailingSlurmGatewayClient(cancel_error_code="SLURM_CANCEL_FAILED"),
    )

    result = ProductionScheduler.from_env(
        ProductionSchedulerConfig(
            now=cycle_time,
            dry_run=False,
            lookback_hours=0,
            cycle_lag_hours=0,
            max_cycles_per_source=1,
            cancel_active_slurm=True,
            restart_reconcile_enabled=False,
        )
    ).run_once()

    assert result.status == "slurm_cancellation_blocked"
    assert result.evidence["skipped_candidates"][0]["reason"] == "cancel_requested_active_slurm"
    cancellation = result.evidence["slurm_cancellation_evidence"][0]
    assert cancellation["error_code"] == "SLURM_CANCEL_FAILED"
    assert cancellation["cancel_attempted"] is True
    assert cancellation["replacement_submitted"] is False
    assert cancellation["status"] == "failed"
    assert result.evidence["slurm_cancellation_proof"]["status"] == "slurm_cancellation_blocked"
    assert result.evidence["slurm_cancellation_proof"]["cancel_called"] is True
    assert result.evidence["slurm_cancellation_proof"]["protected_by_pre_execution_evidence"] is True
    assert result.evidence["counts"]["slurm_cancellation_blocked_count"] == 1
    assert result.evidence["no_mutation_proof"]["slurm_cancellation_called"] is True
    assert result.evidence["no_mutation_proof"]["slurm_submit_called"] is False


def test_file_orchestration_journal_malformed_latest_fails_closed(tmp_path: Path) -> None:
    cycle_time = datetime(2026, 6, 28, tzinfo=UTC)
    journal_root = tmp_path / "journal"
    path = journal_root / "latest/gfs/2026062800/model_a.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")

    repository = FileOrchestrationJournalRepository(journal_root)

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id="model_a",
        run_id="fcst_gfs_2026062800_model_a",
        forcing_version_id="forc_gfs_2026062800_model_a",
        candidate_id="gfs:2026-06-28T00:00:00Z:model_a:forecast_gfs_deterministic",
    )
    assert state is not None
    assert state["pipeline_status"] == "running"
    assert state["file_journal"]["status"] == "blocked"
    assert state["file_journal"]["reason"] == "file_journal_malformed_json"


# ---------------------------------------------------------------------------
# §8.7 / #1107: read-only accessor for the completed run's recorded identity.
# ---------------------------------------------------------------------------


def test_completed_pipeline_init_state_id_returns_recorded_identity(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    latest = _latest_view(cycle_time=cycle_time, hydro_status="complete")
    latest["hydro_run"]["init_state_id"] = "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)
    before = (journal_root / "latest/gfs/2026062800/model_a.json").read_bytes()

    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_a"
        )
        == "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    )
    # Read-only invariant: the accessor never rewrites the audit entry.
    assert (journal_root / "latest/gfs/2026062800/model_a.json").read_bytes() == before


@pytest.mark.parametrize(
    "leg",
    ["no_journal", "no_recorded_id", "hydro_run_absent", "other_model"],
)
def test_completed_pipeline_init_state_id_returns_none_for_unjudgeable_rows(
    tmp_path: Path,
    leg: str,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_stamp = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    if leg == "no_recorded_id":
        # Completed run that recorded no init_state_id at all.
        _write_json(
            journal_root / "latest/gfs/2026062800/model_a.json",
            _latest_view(cycle_time=cycle_time, hydro_status="complete"),
        )
    elif leg == "hydro_run_absent":
        # Completion is carried by a terminal pipeline job with no hydro_run
        # row at all, so there is no identity to read.  No terminal-stage env
        # is set: this leg holds under the default stage as well as under
        # ``forecast_state_save_qc`` (that mode is pinned separately below).
        terminal_job = _active_job(cycle_time)
        terminal_job.update(
            {
                "job_id": f"job_cycle_gfs_{cycle_stamp}_state_save_qc",
                "idempotency_key": f"cycle_gfs_{cycle_stamp}:state_save_qc",
                "run_id": f"fcst_gfs_{cycle_stamp}_model_a",
                "stage": "state_save_qc",
                "status": "succeeded",
                "finished_at": "2026-06-28T00:05:00Z",
            }
        )
        _write_json(
            journal_root / "latest/gfs/2026062800/model_a.json",
            _latest_view(cycle_time=cycle_time, hydro_status=None, jobs=[terminal_job]),
        )
    elif leg == "other_model":
        latest = _latest_view(cycle_time=cycle_time, model_id="model_b", hydro_status="complete")
        latest["hydro_run"]["init_state_id"] = "state_gfs_model_b_2026062800_gfs_2026062712_f012"
        _write_json(journal_root / "latest/gfs/2026062800/model_b.json", latest)

    repository = FileOrchestrationJournalRepository(journal_root)

    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_a"
        )
        is None
    )


def test_completed_pipeline_init_state_id_ignores_superseded_hydro_placeholder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The judged identity must be the COMPLETED run's row.

    Under the ``forecast_state_save_qc`` terminal stage (the value
    ``chain_repository_state._compute_state_save_qc_terminal_enabled`` compares
    against) completion is decided from the pipeline job alone, while the
    ``hydro_run`` row is still a ``created`` placeholder that the write side
    already populated with an ``init_state_id``.  That placeholder does not
    describe the run that completed, so the accessor declines rather than
    handing the scheduler an identity to quarantine on.
    """
    monkeypatch.setenv("NHMS_ORCHESTRATOR_TERMINAL_STAGE", "forecast_state_save_qc")
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_stamp = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    terminal_job = _active_job(cycle_time)
    terminal_job.update(
        {
            "job_id": f"job_cycle_gfs_{cycle_stamp}_state_save_qc",
            "idempotency_key": f"cycle_gfs_{cycle_stamp}:state_save_qc",
            "run_id": f"fcst_gfs_{cycle_stamp}_model_a",
            "stage": "state_save_qc",
            "status": "succeeded",
            "finished_at": "2026-06-28T00:05:00Z",
        }
    )
    latest = _latest_view(cycle_time=cycle_time, hydro_status="created", jobs=[terminal_job])
    latest["hydro_run"]["init_state_id"] = "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)

    # The cycle IS complete — completion and identity are decided separately.
    assert repository.has_completed_pipeline(
        source_id="gfs", cycle_time=cycle_time, model_id="model_a"
    ) is True
    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_a"
        )
        is None
    )


# ---------------------------------------------------------------------------
# #1185: the full cohort-aware identity accessor and the string wrapper that
# delegates to it.  The string wrapper must keep returning only the historical
# ``init_state_id`` / ``initial_state_id`` aliases, while the full accessor
# also exposes the optional checksum/URI/valid-time fields the completion
# verdict compares.
# ---------------------------------------------------------------------------


def _cohort_candidate_terminal_row(
    cycle_time: datetime,
    *,
    model_id: str = "model_a",
    array_task_id: int = 0,
    job_id: str | None = None,
    status: str = "succeeded",
    retry_count: int = 0,
    created_at: str | None = None,
    updated_at: str | None = None,
    identity: dict[str, Any] | None = None,
    identity_sentinel: Any = None,
) -> dict[str, Any]:
    """One current accepted-submit per-model candidate row as reconcile writes it."""

    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    stamp = format_cycle_time(cycle_time)
    row = {
        "job_id": job_id
        or f"job_fcst_gfs_{stamp}_{model_id}_forecast_reconciled_17667_{array_task_id}",
        "run_id": f"fcst_gfs_{stamp}_{model_id}",
        "cycle_id": cycle_id_for("gfs", cycle_time),
        "job_type": "run_shud_forecast_array",
        "slurm_job_id": f"17667_{array_task_id}",
        "array_task_id": array_task_id,
        "model_id": model_id,
        "status": status,
        "stage": "forecast",
        "candidate_id": f"gfs:{cycle_time.isoformat()}:{model_id}:forecast_gfs_deterministic",
        "submit_outcome": "accepted",
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "restart_stage": "forecast",
        "native_shud_resubmitted": False,
        "retry_count": retry_count,
        "created_at": created_at or f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T00:00:00Z",
        "updated_at": updated_at or f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}T00:05:00Z",
    }
    row["init_state_identities"] = (
        identity_sentinel if identity_sentinel is not None else ([identity] if identity is not None else [])
    )
    return row


def _cohort_candidate_identity(
    *,
    model_id: str = "model_a",
    array_task_id: int = 0,
    init_state_id: str = "state_gfs_model_a_2026062800_gfs_2026062712_f012",
) -> dict[str, Any]:
    return {
        "array_task_id": array_task_id,
        "model_id": model_id,
        "init_state_id": init_state_id,
        "init_state_checksum": "sha256:" + "c" * 64,
        "init_state_uri": "s3://nhms/states/gfs/model_a/2026062800/state.cfg.ic",
        "init_state_valid_time": "2026-06-28T00:00:00Z",
    }


def _full_accessor(
    repository: FileOrchestrationJournalRepository,
    cycle_time: datetime,
    *,
    model_id: str = "model_a",
) -> dict[str, Any] | None:
    return repository.completed_pipeline_init_state_identity(
        source_id="gfs", cycle_time=cycle_time, model_id=model_id
    )


def test_full_identity_accessor_prefers_completed_hydro_row_and_preserves_aliases(
    tmp_path: Path,
) -> None:
    """Hydro identity wins over any job row, and bare ``state_id`` is preserved.

    The completed hydro row records only ``init_state_id`` plus a bare
    ``state_id`` alias; the full accessor returns both, while the string
    wrapper returns only the historical alias.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    latest = _latest_view(cycle_time=cycle_time, hydro_status="complete")
    latest["hydro_run"]["init_state_id"] = "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    latest["hydro_run"]["state_id"] = "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)
    before = (journal_root / "latest/gfs/2026062800/model_a.json").read_bytes()

    identity = _full_accessor(repository, cycle_time)
    assert identity is not None
    assert identity["init_state_id"] == "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    assert identity["state_id"] == "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_a"
        )
        == "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    )
    # Read-only invariant: the accessor never rewrites the audit entry.
    assert (journal_root / "latest/gfs/2026062800/model_a.json").read_bytes() == before


def test_bare_state_id_hydro_row_stays_full_only_and_no_judgement_on_both_wirings(
    tmp_path: Path,
) -> None:
    """D1/D2: a bare ``state_id`` alias never becomes the legacy ``init_state_id``.

    The completed hydro row records ONLY the bare ``state_id`` alias.  The full
    accessor must expose it verbatim (the optional-field matcher still reads it
    as the canonical id), while the string wrapper must keep returning ``None``
    — a string wrapper that promotes a bare ``state_id`` to ``init_state_id``
    would leak the row into discovery stale scoring and candidate quarantine.
    The two §8.7 consumers therefore stay no-judgement on the same repository.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    latest = _latest_view(cycle_time=cycle_time, hydro_status="complete")
    latest["hydro_run"]["state_id"] = "state_gfs_model_a_2026062800_gfs_2026062712_f024"
    _write_json(journal_root / f"latest/gfs/{stamp}/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)

    identity = _full_accessor(repository, cycle_time)
    assert identity is not None
    assert identity["state_id"] == "state_gfs_model_a_2026062800_gfs_2026062712_f024"
    assert "init_state_id" not in identity
    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_a"
        )
        is None
    )

    discovery, candidate, source_cycle, discovery_context, construction_context = (
        _discovery_and_quarantine_harness(
            tmp_path, active_repository=repository, cycle_time=cycle_time, stamp=stamp
        )
    )
    stale_tokens, quarantine = _discovery_and_quarantine_context(
        discovery_context, construction_context, discovery, candidate, source_cycle
    )
    assert stale_tokens is None
    assert quarantine is None


def test_full_identity_accessor_prefers_completed_hydro_over_qualified_candidate(
    tmp_path: Path,
) -> None:
    """CAND-01: completed hydro identity A beats a qualified candidate identity B.

    Both sources can coexist for one cycle/model: a completed ``hydro_run`` row
    records identity A, and a current-contract terminal candidate row records a
    DIFFERENT qualified identity B.  The hydro branch must win on both the full
    accessor and the delegated string accessor.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    candidate_identity = _cohort_candidate_identity(
        init_state_id="state_gfs_model_a_2026062800_gfs_2026062712_f024"
    )
    terminal = _cohort_candidate_terminal_row(cycle_time, identity=candidate_identity)
    latest = _latest_view(cycle_time=cycle_time, hydro_status="complete", jobs=[terminal])
    latest["hydro_run"]["init_state_id"] = "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    latest["hydro_run"]["init_state_checksum"] = "sha256:" + "a" * 64
    journal_root = tmp_path / "journal"
    _write_json(journal_root / f"latest/gfs/{stamp}/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)

    identity = _full_accessor(repository, cycle_time)
    assert identity is not None
    assert identity["init_state_id"] == "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    assert identity["init_state_checksum"] == "sha256:" + "a" * 64
    assert identity.get("model_id") != "model_a"  # hydro identity has no model binding
    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_a"
        )
        == "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    )
    # The candidate row is qualified (a distinct wrong-suffix lineage B) but
    # the hydro identity still wins the authority order.
    assert candidate_identity["init_state_id"] != identity["init_state_id"]


def test_full_identity_accessor_placeholder_hydro_falls_back_to_qualified_candidate(
    tmp_path: Path,
) -> None:
    """CAND-03: a declined placeholder P lets the qualified candidate Q win.

    Under the ``forecast_state_save_qc`` terminal mode the hydro row is a
    non-completed placeholder carrying its own recorded id; when a qualified
    current-contract candidate row is ALSO present, the accessor must decline
    the placeholder (hydro status is not completed) and fall back to the
    candidate identity Q on both accessors — never leak P.
    """
    from services.orchestrator.accepted_submit_identity import (
        INIT_STATE_IDENTITY_FIELD,
        accepted_submit_candidate_immutable_evidence,
    )

    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    terminal = _cohort_candidate_terminal_row(cycle_time, identity=_cohort_candidate_identity())
    latest = _latest_view(cycle_time=cycle_time, hydro_status="created", jobs=[terminal])
    latest["hydro_run"]["init_state_id"] = "state_placeholder_gfs_model_a_2026062800"
    journal_root = tmp_path / "journal"
    _write_json(journal_root / f"latest/gfs/{stamp}/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)

    identity = _full_accessor(repository, cycle_time)
    assert identity is not None
    assert identity["init_state_id"] == "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_a"
        )
        == "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    )
    # The candidate row is the qualified fallback Q.
    evidence = accepted_submit_candidate_immutable_evidence(terminal)
    assert evidence[INIT_STATE_IDENTITY_FIELD][0]["init_state_id"] == identity["init_state_id"]


def test_full_identity_accessor_preserves_completed_hydro_optional_identity_fields(
    tmp_path: Path,
) -> None:
    """The completed-hydro full mapping keeps every optional field.

    The hydro projection must not drop ``init_state_checksum`` /
    ``init_state_uri`` / ``init_state_valid_time``, so a direct completed-hydro
    row whose optional identity fields conflicted with the strict selection
    would silently match.  All present optional fields must survive.
    """
    from services.orchestrator.scheduler_init_state_match import (
        INIT_STATE_IDENTITY_FIELDS,
        init_state_field,
    )

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    latest = _latest_view(cycle_time=cycle_time, hydro_status="complete")
    latest["hydro_run"]["init_state_id"] = "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    latest["hydro_run"]["init_state_checksum"] = "sha256:" + "a" * 64
    latest["hydro_run"]["init_state_uri"] = "s3://nhms/states/gfs/model_a/2026062800/state.cfg.ic"
    latest["hydro_run"]["init_state_valid_time"] = "2026-06-28T00:00:00Z"
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)
    before = (journal_root / "latest/gfs/2026062800/model_a.json").read_bytes()

    identity = _full_accessor(repository, cycle_time)
    assert identity is not None
    assert identity["init_state_id"] == "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    assert identity["init_state_checksum"] == "sha256:" + "a" * 64
    assert identity["init_state_uri"] == "s3://nhms/states/gfs/model_a/2026062800/state.cfg.ic"
    assert identity["init_state_valid_time"] == "2026-06-28T00:00:00Z"
    # Every canonical matcher field is resolvable through the accessor output.
    for field in INIT_STATE_IDENTITY_FIELDS:
        assert init_state_field(identity, field) not in (None, ""), field
    # Read-only invariant.
    assert (journal_root / "latest/gfs/2026062800/model_a.json").read_bytes() == before


def test_full_identity_accessor_preserves_hydro_optional_field_through_aliases(
    tmp_path: Path,
) -> None:
    """The matcher's alternate aliases (initial_state_*, ic_file_uri, ...) survive too."""
    from services.orchestrator.scheduler_init_state_match import init_state_field

    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    latest = _latest_view(cycle_time=cycle_time, hydro_status="complete")
    latest["hydro_run"]["initial_state_id"] = "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    latest["hydro_run"]["initial_state_checksum"] = "sha256:" + "b" * 64
    latest["hydro_run"]["ic_file_uri"] = "s3://nhms/states/gfs/model_a/2026062800/state.cfg.ic"
    latest["hydro_run"]["valid_time"] = "2026-06-28T00:00:00Z"
    _write_json(journal_root / "latest/gfs/2026062800/model_a.json", latest)
    repository = FileOrchestrationJournalRepository(journal_root)

    identity = _full_accessor(repository, cycle_time)
    assert identity is not None
    assert init_state_field(identity, "state_id") == "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    assert init_state_field(identity, "checksum") == "sha256:" + "b" * 64
    assert init_state_field(identity, "uri") == "s3://nhms/states/gfs/model_a/2026062800/state.cfg.ic"
    assert init_state_field(identity, "valid_time") == "2026-06-28T00:00:00Z"
    # The string wrapper still only recognizes the two historical id aliases.
    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_a"
        )
        == "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    )


def test_full_identity_accessor_round_trips_the_cohort_candidate_terminal_row(
    tmp_path: Path,
) -> None:
    """Real write -> reopen -> read: the full mapping and the old string id.

    ``completed_pipeline_init_state_id`` and the full accessor both come from
    the same journal on a reopened repository, and no byte on disk changes.
    """
    cycle_time = _dt("2026-07-20T00:00:00Z")
    repository, record = _reserved_cohort_master(
        tmp_path,
        member_count=1,
        init_state_identities=[
            _cohort_init_state_identity(0)
        ],
    )
    reserved = repository.reserve_pipeline_job(record)
    assert reserved is not None
    _bind_and_project_cohort(repository, record, member_count=1)
    terminal_id = "job_fcst_gfs_2026072000_model_0_forecast_reconciled_17667_0"
    assert repository.get_pipeline_job(terminal_id) is not None

    reopened = FileOrchestrationJournalRepository(tmp_path / "journal")
    identity = reopened.completed_pipeline_init_state_identity(
        source_id="gfs", cycle_time=cycle_time, model_id="model_0"
    )
    assert identity is not None
    assert identity["init_state_id"] == "state_gfs_model_0_2026072000_gfs_2026071912_f012"
    assert identity["init_state_checksum"].startswith("sha256:")
    assert identity["init_state_uri"].startswith("s3://")
    assert identity["init_state_valid_time"] == "2026-07-20T00:00:00Z"
    assert identity["array_task_id"] == 0
    assert identity["model_id"] == "model_0"
    assert reopened.completed_pipeline_init_state_id(
        source_id="gfs", cycle_time=cycle_time, model_id="model_0"
    ) == "state_gfs_model_0_2026072000_gfs_2026071912_f012"

    # Read-only: the accessor leaves every on-disk payload untouched.
    before = {
        path: path.read_bytes()
        for path in (tmp_path / "journal").rglob("*")
        if path.is_file() and not path.name.endswith(".lock")
    }
    reopened.completed_pipeline_init_state_identity(
        source_id="gfs", cycle_time=cycle_time, model_id="model_0"
    )
    assert {
        path: path.read_bytes()
        for path in (tmp_path / "journal").rglob("*")
        if path.is_file() and not path.name.endswith(".lock")
    } == before


def test_full_identity_accessor_returns_latest_current_candidate_terminal_identity(
    tmp_path: Path,
) -> None:
    """Latest current-contract candidate row wins when it is terminal-success."""
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    terminal = _cohort_candidate_terminal_row(cycle_time, identity=_cohort_candidate_identity())
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / f"latest/gfs/{stamp}/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[terminal]),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    identity = _full_accessor(repository, cycle_time)
    assert identity is not None
    assert identity["init_state_id"] == "state_gfs_model_a_2026062800_gfs_2026062712_f012"
    assert identity["init_state_checksum"] == "sha256:" + "c" * 64
    assert repository.completed_pipeline_init_state_id(
        source_id="gfs", cycle_time=cycle_time, model_id="model_a"
    ) == "state_gfs_model_a_2026062800_gfs_2026062712_f012"


def test_full_identity_accessor_returns_latest_first_old_success_cannot_hide_newer_failure(
    tmp_path: Path,
) -> None:
    """A newer failed/empty row hides an older succeeded one (D2 latest-first)."""
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    old_success = _cohort_candidate_terminal_row(
        cycle_time,
        job_id="job_fcst_gfs_2026062800_model_a_forecast_reconciled_17667_0",
        updated_at="2026-06-28T00:04:00Z",
        identity=_cohort_candidate_identity(),
    )
    journal_root = tmp_path / "journal"

    for newer_status in ("failed", "empty"):
        if newer_status == "failed":
            newer = _cohort_candidate_terminal_row(
                cycle_time,
                job_id="job_fcst_gfs_2026062800_model_a_forecast_reconciled_17667_1",
                status="failed",
                updated_at="2026-06-28T00:06:00Z",
                identity=_cohort_candidate_identity(init_state_id="state_newer"),
            )
        else:
            newer = _cohort_candidate_terminal_row(
                cycle_time,
                job_id="job_fcst_gfs_2026062800_model_a_forecast_reconciled_17667_1",
                updated_at="2026-06-28T00:06:00Z",
                identity_sentinel=[],
            )
        _write_json(
            journal_root / f"latest/gfs/{stamp}/model_a.json",
            _latest_view(cycle_time=cycle_time, jobs=[old_success, newer]),
        )
        repository = FileOrchestrationJournalRepository(journal_root)
        assert _full_accessor(repository, cycle_time) is None, newer_status
        assert (
            repository.completed_pipeline_init_state_id(
                source_id="gfs", cycle_time=cycle_time, model_id="model_a"
            )
            is None
        ), newer_status


def test_full_identity_accessor_latest_valid_success_returns_when_newer_masks(
    tmp_path: Path,
) -> None:
    """A newer failed/empty row masks an older success; the newest valid wins.

    D2 requires the LATEST row to be terminal-success with a single identity.
    A newer failed row masks an older success (None), and a newest valid
    terminal-success row returns its identity — the truth-order selection is
    latest-first, not any-success.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    old_success = _cohort_candidate_terminal_row(
        cycle_time,
        job_id="job_fcst_gfs_2026062800_model_a_forecast_reconciled_17667_0",
        updated_at="2026-06-28T00:04:00Z",
        identity=_cohort_candidate_identity(),
    )
    newest = _cohort_candidate_terminal_row(
        cycle_time,
        job_id="job_fcst_gfs_2026062800_model_a_forecast_reconciled_17667_2",
        updated_at="2026-06-28T00:08:00Z",
        identity=_cohort_candidate_identity(init_state_id="state_newest"),
    )
    journal_root = tmp_path / "journal"

    _write_json(
        journal_root / f"latest/gfs/{stamp}/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[old_success, newest]),
    )
    repository = FileOrchestrationJournalRepository(journal_root)
    identity = _full_accessor(repository, cycle_time)
    assert identity is not None
    assert identity["init_state_id"] == "state_newest"


@pytest.mark.parametrize(
    "leg",
    [
        "latest_failed",
        "latest_empty_identity",
        "marker_free",
        "malformed_incomplete_master",
        "other_model",
        "no_journal",
    ],
)
def test_full_identity_accessor_returns_none_for_unqualified_shapes(
    tmp_path: Path,
    leg: str,
) -> None:
    """Every rejection shape returns ``None`` and never raises.

    The rejection is split across two honest boundaries:

    - ``latest_failed`` / ``latest_empty_identity`` / ``marker_free`` reach
      the ACCESSOR SELECTOR: they are valid rows the selector must decline.
    - ``malformed_incomplete_master`` / ``other_model`` are rejected by the
      LOADER's model binding / accepted-submit normalization before the
      selector ever runs; these legs are loader-level fail-to-absent proofs,
      not selector proofs.
    - A VALID current-contract cohort master — one the loader accepts and that
      survives ``_cycle_rows`` — is excluded from candidate authority by the
      row-kind filter (``accepted_submit_row_kind == "candidate"``) alone, not
      by the loader (see
      ``test_full_identity_accessor_valid_cohort_master_without_terminal_candidate``).
    - ``no_journal`` is the empty authority shape.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    if leg == "latest_failed":
        terminal = _cohort_candidate_terminal_row(cycle_time, status="failed")
    elif leg == "latest_empty_identity":
        terminal = _cohort_candidate_terminal_row(cycle_time, identity_sentinel=[])
    elif leg == "marker_free":
        terminal = _cohort_candidate_terminal_row(cycle_time)
        del terminal["accepted_submit_contract_version"]
    elif leg == "malformed_incomplete_master":
        # Loader-level fail-to-absent proof: a MASTER-SHAPED but INCOMPLETE
        # row (model-less + cycle run id + master markers, yet missing the
        # required master evidence fields such as ``cohort_members``,
        # ``submission_attempt`` and the reconciliation family) is rejected by
        # ``normalize_accepted_submit_evidence`` before the accessor selector
        # ever runs.
        from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

        terminal = _cohort_candidate_terminal_row(cycle_time)
        terminal.update(
            {
                "model_id": None,
                "run_id": f"cycle_gfs_{stamp}",
                "candidate_id": f"cycle_gfs_{stamp}",
                "init_state_identities": [_cohort_candidate_identity(array_task_id=0)],
                "slurm_comment": "nhms_idem:cycle",
                "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
            }
        )
    elif leg == "other_model":
        # Loader-level fail-to-absent proof: written into the model_a latest
        # view, this foreign row is rejected by the loader's model binding
        # before the accessor selector runs.
        terminal = _cohort_candidate_terminal_row(
            cycle_time, model_id="model_b", array_task_id=1
        )
    else:  # no_journal
        terminal = None
    if terminal is not None:
        _write_json(
            journal_root / f"latest/gfs/{stamp}/model_a.json",
            _latest_view(cycle_time=cycle_time, jobs=[terminal]),
        )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert _full_accessor(repository, cycle_time) is None
    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_a"
        )
        is None
    )


def test_full_identity_accessor_valid_cohort_master_without_terminal_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A VALID current-contract master alone yields no identity.

    A REAL, loader-valid current-contract cohort master written by the
    canonical accepted-submit writer path — with its own ``init_state_identities``
    map — SURVIVES ``_cycle_rows`` for the model and is visible in
    ``rows.pipeline_jobs``.  The row-kind filter
    (``accepted_submit_row_kind == "candidate"``) alone excludes it from
    candidate authority before ``_candidate_row_self_bound_identity`` runs,
    not the loader; the candidate immutable evidence is never invoked on the
    master row.  Because no per-model terminal candidate row exists for it,
    the accessor selector finds no ``candidate`` row, so BOTH the full and
    string accessors return ``None``.

    The outer-owner seam is pinned: before the accessors run, the module's
    ``accepted_submit_candidate_immutable_evidence`` is replaced with a
    sentinel that raises ``AssertionError`` if ever invoked.  Correct code
    stays green (the outer row-kind filter excludes the valid master before
    the inner candidate helper can call the canonical evidence); deleting
    ONLY the outer ``accepted_submit_row_kind == "candidate"`` predicate sends
    the master into the inner helper and this test goes red with the
    sentinel's message.
    """
    from services.orchestrator.accepted_submit_identity import (
        AcceptedSubmitTransition,
    )

    cycle_time = _dt("2026-07-20T00:00:00Z")
    repository, record = _reserved_cohort_master(
        tmp_path,
        member_count=1,
        init_state_identities=[_cohort_init_state_identity(0)],
    )
    assert repository.reserve_pipeline_job(dict(record)) is not None
    commit = repository.commit_pipeline_job_submit_attempt(
        str(record["idempotency_key"]),
        pipeline_job_id=str(record["job_id"]),
        expected_submission_attempt=1,
        slurm_job_id=_PERMANENT_FAILURE_MASTER_SLURM_JOB_ID,
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    assert commit.committed
    job_id = str(record["job_id"])
    source_id = journal_module._source_id_from_job(record)
    with repository._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
        existing = repository._accepted_submit_job_for_id_unlocked(
            job_id, source_id=source_id, cycle_time=cycle_time
        )
        assert existing is not None
        row = {**existing, "status": "succeeded"}
        journal_record = journal_module._journal_record_for_write(
            "pipeline_job",
            row,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=None,
            sequence=repository._next_sequence_unlocked(
                source_id=source_id, cycle_time=cycle_time
            ),
        )
        repository._validate_outgoing_record(
            journal_record,
            source_id=source_id,
            cycle_time=cycle_time,
            record_type="pipeline_job",
            model_id=None,
        )
        repository._append_journal_records_unlocked(
            source_id=source_id,
            cycle_time=cycle_time,
            records=[journal_record],
        )

    # The valid master survives the loader into model_0's row set...
    rows = repository._cycle_rows(
        source_id="gfs", cycle_time=cycle_time, model_id="model_0"
    )
    assert job_id in rows.pipeline_jobs
    assert (
        journal_module.accepted_submit_row_kind(rows.pipeline_jobs[job_id])
        == "master"
    )
    # ...and no per-model terminal candidate row exists for it.
    candidate_ids = [
        job_id_
        for job_id_, job in rows.pipeline_jobs.items()
        if journal_module.accepted_submit_row_kind(job) == "candidate"
    ]
    assert candidate_ids == []

    # Pin the outer-owner seam: the valid master must be excluded by the
    # outer row-kind filter BEFORE the inner candidate helper's canonical
    # immutable evidence can run.  A sentinel replaces the module-level
    # function (pytest restores it automatically); if the outer predicate is
    # ever deleted, the master reaches the inner helper and this raises.
    def _fail_if_candidate_immutable_evidence_invoked(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "candidate immutable evidence must not run on a valid cohort master "
            "(outer accepted_submit_row_kind == 'candidate' filter must exclude it first)"
        )

    monkeypatch.setattr(
        journal_module,
        "accepted_submit_candidate_immutable_evidence",
        _fail_if_candidate_immutable_evidence_invoked,
    )

    # Full and string accessors: no identity.
    assert _full_accessor(repository, cycle_time, model_id="model_0") is None
    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_0"
        )
        is None
    )


def test_full_identity_accessor_rejects_malformed_current_row_without_raising(
    tmp_path: Path,
) -> None:
    """A malformed latest candidate row fails to absent instead of raising."""
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    malformed = _cohort_candidate_terminal_row(cycle_time, identity_sentinel=["not-a-mapping"])
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / f"latest/gfs/{stamp}/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[malformed]),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert _full_accessor(repository, cycle_time) is None
    assert repository.completed_pipeline_init_state_id(
        source_id="gfs", cycle_time=cycle_time, model_id="model_a"
    ) is None


def test_full_identity_accessor_rejects_direct_partition_foreign_model_cycle_run_row(
    tmp_path: Path,
) -> None:
    """CAND-04: a loader-surviving foreign-model cycle-run row is selector-rejected.

    A current-contract candidate row naming ``model_b`` whose ``run_id`` is
    the exact cycle run id (``cycle_gfs_<stamp>``) SURVIVES ``_cycle_rows`` for
    model_a (the direct scan filters by ``_job_matches_candidate``, whose
    cycle-run arm is wide), so the row reaches the accessor selector.  The
    self-bound model guard inside ``_candidate_row_self_bound_identity`` must
    reject the ``model_b`` entry for a ``model_a`` read on BOTH accessors,
    keeping the authority None (no loader rejection involved).
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    foreign = _cohort_candidate_terminal_row(
        cycle_time,
        model_id="model_b",
        array_task_id=1,
        job_id="job_fcst_gfs_2026062800_model_b_forecast_reconciled_17667_1",
        identity=_cohort_candidate_identity(
            model_id="model_b",
            array_task_id=1,
            init_state_id="state_gfs_model_b_2026062800_gfs_2026062712_f012",
        ),
    )
    # The exact cycle run id: the direct-partition cycle-wide scan accepts it
    # even though the row names a foreign model.
    foreign["run_id"] = f"cycle_gfs_{stamp}"
    foreign["candidate_id"] = f"cycle_gfs_{stamp}"
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / f"latest/gfs/{stamp}/model_a.json",
        _latest_view(cycle_time=cycle_time),
    )
    _write_json(
        journal_root / f"pipeline-jobs/{foreign['job_id']}.json",
        _journal_record(
            record_type="pipeline_job",
            source_id="gfs",
            cycle_time=cycle_time,
            model_id=foreign["model_id"],
            payload=foreign,
        ),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    # The row survives the loader into model_a's row set...
    rows = repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id="model_a")
    assert foreign["job_id"] in rows.pipeline_jobs
    # ...and the accessor selector still rejects its foreign-bound entry.
    assert _full_accessor(repository, cycle_time) is None
    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_a"
        )
        is None
    )


def test_full_identity_accessor_masks_foreign_self_bound_row_even_when_foreign_is_newer(
    tmp_path: Path,
) -> None:
    """CAND-04 latest-first: the self-bound model guard outranks mere recency.

    Both rows are current-contract candidates; the older one is the model_a
    entry (legal identity, so it is not loader- or qualifier-rejected), and the
    NEWER one names ``model_b`` but carries the exact cycle run id
    (``cycle_gfs_<stamp>``), which lets it survive the direct-partition
    cycle-wide scan into the selector.  Selection is latest-first, so a wrong
    mutation that filters foreign rows out BEFORE the max would turn the
    older model_a entry into the latest surviving row and leak its legal
    identity on both accessors — the exact regression this oracle forbids.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    foreign = _cohort_candidate_terminal_row(
        cycle_time,
        model_id="model_b",
        array_task_id=1,
        job_id="job_fcst_gfs_2026062800_model_b_forecast_reconciled_17667_1",
        updated_at="2026-06-28T00:06:00Z",
        identity=_cohort_candidate_identity(
            model_id="model_b",
            array_task_id=1,
            init_state_id="state_gfs_model_b_2026062800_gfs_2026062712_f012",
        ),
    )
    # The exact cycle run id: the direct-partition cycle-wide scan accepts it
    # even though the row names a foreign model.
    foreign["run_id"] = f"cycle_gfs_{stamp}"
    foreign["candidate_id"] = f"cycle_gfs_{stamp}"
    terminal = _cohort_candidate_terminal_row(
        cycle_time,
        updated_at="2026-06-28T00:05:00Z",
        identity=_cohort_candidate_identity(),
    )
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / f"latest/gfs/{stamp}/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[terminal]),
    )
    _write_json(
        journal_root / f"pipeline-jobs/{foreign['job_id']}.json",
        _journal_record(
            record_type="pipeline_job",
            source_id="gfs",
            cycle_time=cycle_time,
            model_id=foreign["model_id"],
            payload=foreign,
        ),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    rows = repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id="model_a")
    assert foreign["job_id"] in rows.pipeline_jobs
    assert terminal["job_id"] in rows.pipeline_jobs
    # The foreign row is strictly newer than the model_a terminal row, so a
    # filter-then-max implementation would select the model_a row here.
    from services.orchestrator.chain_source_cycle import _pipeline_job_truth_sort_key

    assert _pipeline_job_truth_sort_key(foreign) > _pipeline_job_truth_sort_key(terminal)
    assert _full_accessor(repository, cycle_time) is None
    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_a"
        )
        is None
    )


def test_full_identity_accessor_ignores_run_manifest_only_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Design D1/D4: the run manifest is no #1185 authority, even when loaded.

    A real journal with a completed ``hydro_run`` recording no identity alias
    and NO qualified candidate row; the run manifest DOES carry an
    init-state identity (wrong-suffix, so any consumer that promoted it would
    judge a stale lineage) at exactly the path candidate-state assembly reads.
    The full and string accessors must stay ``None``, and both §8.7 wirings —
    discovery stale tokens and candidate quarantine — must stay no-judgement on
    the same repository.  The candidate-state assertion is the proof the
    manifest was actually loaded rather than a dead fixture field.
    """
    from services.orchestrator.chain_repository_state import _run_manifest_initial_state_for_run

    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    object_root = tmp_path / "object-store"
    manifest_state_id = "state_gfs_model_a_2026062800_gfs_2026062712_f024"
    _write_json(
        object_root / f"runs/fcst_gfs_{stamp}_model_a/input/manifest.json",
        {"initial_state": {"state_id": manifest_state_id, "quality": "fresh"}},
    )
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / f"latest/gfs/{stamp}/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="complete"),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    # The manifest is really loaded by candidate-state assembly...
    assert _run_manifest_initial_state_for_run(f"fcst_gfs_{stamp}_model_a") == {
        "state_id": manifest_state_id,
        "quality": "fresh",
    }
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state.get("run_manifest_initial_state") == {
        "state_id": manifest_state_id,
        "quality": "fresh",
    }
    # ...but it is no journal identity authority.
    assert _full_accessor(repository, cycle_time) is None
    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_a"
        )
        is None
    )

    discovery, candidate, source_cycle, discovery_context, construction_context = (
        _discovery_and_quarantine_harness(
            tmp_path, active_repository=repository, cycle_time=cycle_time, stamp=stamp
        )
    )
    stale_tokens, quarantine = _discovery_and_quarantine_context(
        discovery_context, construction_context, discovery, candidate, source_cycle
    )
    assert stale_tokens is None
    assert quarantine is None


def test_full_identity_accessor_malformed_duplicate_no_replay_timestamp_fails_to_absent(
    tmp_path: Path,
) -> None:
    """A malformed read-stage timestamp never leaks out of the authority.

    CAND-02 regression: two rows of the SAME ``job_id`` with no replay block
    resolve the duplicate through ``_latest_mapping``, whose shared timestamp
    sort can raise a bare ``ValueError`` for a malformed ``updated_at``.  The
    full accessor must absorb it (fail to absent) instead of leaking it, and
    the delegated string accessor keeps making no judgement.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    first = _cohort_candidate_terminal_row(
        cycle_time,
        job_id="job_fcst_gfs_2026062800_model_a_forecast_reconciled_17667_0",
        updated_at="2026-06-28T00:05:00Z",
        identity=_cohort_candidate_identity(),
    )
    duplicate = _cohort_candidate_terminal_row(
        cycle_time,
        job_id="job_fcst_gfs_2026062800_model_a_forecast_reconciled_17667_0",
        updated_at="not-a-timestamp",
        identity=_cohort_candidate_identity(init_state_id="state_other"),
    )
    latest = _latest_view(cycle_time=cycle_time, jobs=[first, duplicate])
    # No replay block: the duplicate resolution must fall through to the
    # timestamp comparison that raises on the malformed value.
    latest.pop("replay", None)
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / f"latest/gfs/{stamp}/model_a.json",
        latest,
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert _full_accessor(repository, cycle_time) is None
    assert (
        repository.completed_pipeline_init_state_id(
            source_id="gfs", cycle_time=cycle_time, model_id="model_a"
        )
        is None
    )


def test_full_identity_accessor_returns_defensive_copy(tmp_path: Path) -> None:
    """The returned mapping is a copy; mutating it never touches the journal."""
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    terminal = _cohort_candidate_terminal_row(cycle_time, identity=_cohort_candidate_identity())
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / f"latest/gfs/{stamp}/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[terminal]),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    first = _full_accessor(repository, cycle_time)
    assert first is not None
    first["init_state_id"] = "state_tampered"
    second = _full_accessor(repository, cycle_time)
    assert second is not None
    assert second["init_state_id"] == "state_gfs_model_a_2026062800_gfs_2026062712_f012"


def test_full_identity_accessor_ignores_candidate_state_job_limit_truncation(
    tmp_path: Path,
) -> None:
    """The accessor reads internal untruncated rows, not the bounded payload.

    The identity-bearing current candidate row sits BELOW the bounded
    truth-order cutoff: enough newer cohort masters fill every retained slot
    at the default limit (and thus at every smaller limit), so a bounded-reader
    regression can never return the identity from the payload.  The full
    accessor must still return it from the internal untruncated rows.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    terminal = _cohort_candidate_terminal_row(
        cycle_time,
        identity=_cohort_candidate_identity(),
        updated_at="2026-06-28T00:04:00Z",
    )
    # One more terminal row than the default candidate-state job limit, all
    # newer than the identity-bearing terminal.  The masters are model-less
    # cycle-scope cohort rows: they fill bounded-payload slots (and the
    # terminal filter admits their ``forecast`` stage) while never being
    # candidate rows the full accessor can select.
    newer_masters = [
        _cohort_master_job(
            cycle_time,
            job_suffix=f"_observed_{index}",
            updated_at=f"2026-06-28T00:04:{(index % 59) + 1:02d}Z",
        )
        for index in range(1, DEFAULT_CANDIDATE_STATE_JOB_LIMIT + 2)
    ]
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / f"latest/gfs/{stamp}/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[terminal, *newer_masters]),
    )
    repository = FileOrchestrationJournalRepository(journal_root)
    identity_job_id = terminal["job_id"]

    # The bounded payload is truncated and every retained slot is occupied by
    # rows newer than the identity-bearing terminal: at the repository's
    # default candidate-state job limit -- and therefore at any smaller limit,
    # including the explicit ``job_limit=2`` probe below -- the payload can
    # never carry the identity-bearing job id.
    bounded = _candidate_state(repository, cycle_time=cycle_time)
    assert bounded is not None
    assert bounded["state_truncated"] is True
    assert len(bounded["pipeline_jobs"]) == DEFAULT_CANDIDATE_STATE_JOB_LIMIT
    assert identity_job_id not in {
        str(job.get("job_id") or "") for job in bounded["pipeline_jobs"]
    }

    explicit = _candidate_state(repository, cycle_time=cycle_time, job_limit=2, event_limit=2)
    assert explicit is not None
    assert explicit["state_truncated"] is True
    assert identity_job_id not in {
        str(job.get("job_id") or "") for job in explicit["pipeline_jobs"]
    }

    identity = _full_accessor(repository, cycle_time)
    assert identity is not None
    assert identity["init_state_id"] == "state_gfs_model_a_2026062800_gfs_2026062712_f012"


# ---------------------------------------------------------------------------
# #1185: discovery §8.7 scoring and candidate quarantine consume one token
# source.  The old string accessor delegates to the full accessor, so both
# wirings must agree on a real repository fixture (design D4).
# ---------------------------------------------------------------------------


def _discovery_and_quarantine_harness(
    tmp_path: Path,
    *,
    active_repository: Any,
    cycle_time: datetime,
    stamp: str,
    base_lead: int = 12,
) -> tuple[Any, Any, Any, Any, Any]:
    """Build both §8.7 wiring contexts for one real journal repository."""
    from services.orchestrator.scheduler_discovery import (
        SchedulerDiscoveryContext,
    )
    from services.orchestrator.scheduler_discovery import (
        SchedulerSourceCycle as DiscoverySourceCycle,
    )

    discovery = CycleDiscovery(
        cycle_id=f"gfs_{stamp}",
        source_id="gfs",
        cycle_time=cycle_time,
        cycle_hour=0,
        available=True,
        status="discovered",
    )
    candidate = _scheduler_candidate_fixture()
    candidate = scheduler_module._candidate_for(
        discovery=discovery,
        model=scheduler_module.RegisteredSchedulerModel(
            model_id=candidate.model_id,
            basin_id=candidate.basin_id,
            basin_version_id=candidate.basin_version_id,
            river_network_version_id=candidate.river_network_version_id,
            segment_count=3,
            output_segment_count=3,
            model_package_uri="s3://nhms/models/model_a/package/",
            shud_code_version="2.0",
            resource_profile={},
            resource_profile_summary={},
            display_capabilities={},
        ),
        horizon={},
    )
    source_cycle = DiscoverySourceCycle(discovery=discovery, horizon={})
    scheduler = ProductionScheduler(
        _config(tmp_path, now=_dt("2026-06-28T12:00:00Z")),
        registry=FakeRegistry([_model("model_a", "basin_a")]),
        adapters={},
        active_repository=active_repository,
    )
    discovery_context = SchedulerDiscoveryContext(
        config=scheduler.config,
        adapters={},
        active_repository=active_repository,
        floor_to_source_cycle_boundary=lambda value, _sources: value,
        source_horizon_metadata=lambda discovery, adapter: {},
        candidate_factory=lambda *a, **k: candidate,
        candidate_state_provider_caller=lambda *a, **k: None,
        candidate_state_decider=lambda candidate, state: None,
        required_lead_hours_for_candidate=lambda candidate, source_cycle: base_lead,
    )
    construction_context = scheduler_candidates_module.SchedulerCandidateConstructionContext(
        config=scheduler.config,
        active_repository=active_repository,
        canonical_readiness_for_candidate=lambda *a, **k: {"ready": True},
        strict_warm_start_for_candidate=lambda *a, **k: None,
        orchestrator_for=lambda *a, **k: None,
        candidate_factory=lambda *a, **k: candidate,
        candidate_state_provider_caller=scheduler_state_decision_module._call_candidate_state_provider,
        active_slurm_jobs_provider_caller=scheduler_state_decision_module._call_active_slurm_jobs_provider,
        active_slurm_jobs_bounder=lambda *a, **k: [],
        candidate_state_decider=scheduler_state_decision_module._candidate_state_decision,
        candidate_state_identity_mismatch_detector=scheduler_module._candidate_state_has_identity_mismatch,
        candidate_state_scoped_retry_detector=scheduler_module._candidate_state_is_candidate_scoped_retry,
        repaired_state_audit_evidence_builder=scheduler_module._candidate_repaired_state_audit_evidence,
        successor_state_for_candidate=None,
        required_lead_hours_for_candidate=lambda candidate, source_cycle: base_lead,
        max_candidates=scheduler_module.MAX_CANDIDATES,
    )
    return (
        discovery,
        candidate,
        source_cycle,
        discovery_context,
        construction_context,
    )


def _discovery_and_quarantine_context(
    discovery_context: Any,
    construction_context: Any,
    discovery: Any,
    candidate: Any,
    source_cycle: Any,
) -> tuple[Any, Any]:
    """Drive the two §8.7 wirings with their own context types.

    The discovery stale-token filter consumes a ``SchedulerDiscoveryContext``
    while the candidate quarantine consumes a
    ``SchedulerCandidateConstructionContext``; passing one context to both
    would let either wiring silently read the wrong provider surface.
    """
    stale_tokens = scheduler_discovery_module._journal_predecessor_identity_stale_tokens(
        discovery_context, discovery, candidate, horizon={}
    )
    quarantine = scheduler_candidates_module._journal_predecessor_identity_quarantine(
        construction_context,
        candidate,
        source_cycle,
        CandidateStateDecision(
            "skip",
            "terminal_completed_cycle",
            {"terminal_source": "pipeline_job", "terminal_status": "succeeded"},
        ),
    )
    return stale_tokens, quarantine


def test_discovery_and_candidate_quarantine_share_delegated_string_accessor(
    tmp_path: Path,
) -> None:
    """Both §8.7 wirings judge the same cohort token off one real repository.

    The candidate terminal row records a wrong-suffix lineage; the discovery
    filter must report a positive mismatch and the candidate quarantine must
    decline the completed skip.
    """
    from services.orchestrator.scheduler_generation import expected_journal_init_state_tokens

    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    base_lead = 12

    # A wrong-suffix lineage for cycle 2026062800 (same base key, different f-suffix).
    recorded_id = "state_gfs_model_a_2026062800_gfs_2026062712_f024"
    _base, expected_id = expected_journal_init_state_tokens(
        source_id="gfs",
        model_id="model_a",
        candidate_valid_time=cycle_time,
        required_lead_hours=base_lead,
    )
    assert recorded_id != expected_id

    terminal = _cohort_candidate_terminal_row(
        cycle_time,
        identity={
            "array_task_id": 0,
            "model_id": "model_a",
            "init_state_id": recorded_id,
        },
    )
    journal_root = tmp_path / "journal"
    _write_json(
        journal_root / f"latest/gfs/{stamp}/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[terminal]),
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    # The full accessor returns the recorded lineage; the string wrapper the id.
    identity = repository.completed_pipeline_init_state_identity(
        source_id="gfs", cycle_time=cycle_time, model_id="model_a"
    )
    assert identity is not None
    assert identity["init_state_id"] == recorded_id
    assert repository.completed_pipeline_init_state_id(
        source_id="gfs", cycle_time=cycle_time, model_id="model_a"
    ) == recorded_id

    discovery, candidate, source_cycle, discovery_context, construction_context = (
        _discovery_and_quarantine_harness(
            tmp_path, active_repository=repository, cycle_time=cycle_time, stamp=stamp
        )
    )
    stale_tokens = scheduler_discovery_module._journal_predecessor_identity_stale_tokens(
        discovery_context, discovery, candidate, horizon={}
    )
    quarantine = scheduler_candidates_module._journal_predecessor_identity_quarantine(
        construction_context,
        candidate,
        source_cycle,
        CandidateStateDecision(
            "skip",
            "terminal_completed_cycle",
            {"terminal_source": "pipeline_job", "terminal_status": "succeeded"},
        ),
    )
    assert stale_tokens is not None
    assert quarantine is not None
    assert quarantine.reason == "journal_predecessor_identity_mismatch"


def test_discovery_and_quarantine_no_judgement_shapes_unchanged(
    tmp_path: Path,
) -> None:
    """No accessor / no record / suffix-less / different-base stay no-judgement.

    The discovery filter returns no stale tokens and the candidate quarantine
    leaves the completed skip alone for every pre-change shape.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    base_lead = 12

    discovery, candidate, source_cycle, discovery_context, construction_context = (
        _discovery_and_quarantine_harness(
            tmp_path, active_repository=RawCandidateStateRepository({}), cycle_time=cycle_time, stamp=stamp
        )
    )
    stale_tokens = scheduler_discovery_module._journal_predecessor_identity_stale_tokens(
        discovery_context, discovery, candidate, horizon={}
    )
    quarantine = scheduler_candidates_module._journal_predecessor_identity_quarantine(
        construction_context,
        candidate,
        source_cycle,
        CandidateStateDecision(
            "skip",
            "terminal_completed_cycle",
            {"terminal_source": "pipeline_job", "terminal_status": "succeeded"},
        ),
    )
    assert stale_tokens is None
    assert quarantine is None

    # No record: an empty journal with no candidate rows.
    journal_root = tmp_path / "empty-journal"
    _write_json(
        journal_root / f"latest/gfs/{stamp}/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="complete"),
    )
    empty_repository = FileOrchestrationJournalRepository(journal_root)
    discovery, candidate, source_cycle, discovery_context, construction_context = (
        _discovery_and_quarantine_harness(
            tmp_path, active_repository=empty_repository, cycle_time=cycle_time, stamp=stamp
        )
    )
    stale_tokens, quarantine = _discovery_and_quarantine_context(
        discovery_context, construction_context, discovery, candidate, source_cycle
    )
    assert stale_tokens is None
    assert quarantine is None

    # Suffix-less legacy id (equals the expected base key): no judgement.
    _base_key, _expected_token = scheduler_generation_module.expected_journal_init_state_tokens(
        source_id="gfs",
        model_id="model_a",
        candidate_valid_time=cycle_time,
        required_lead_hours=base_lead,
    )
    legacy_terminal = _cohort_candidate_terminal_row(
        cycle_time,
        identity={
            "array_task_id": 0,
            "model_id": "model_a",
            "init_state_id": _base_key,
        },
    )
    legacy_root = tmp_path / "legacy-journal"
    _write_json(
        legacy_root / f"latest/gfs/{stamp}/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[legacy_terminal]),
    )
    legacy_repository = FileOrchestrationJournalRepository(legacy_root)
    discovery, candidate, source_cycle, discovery_context, construction_context = (
        _discovery_and_quarantine_harness(
            tmp_path, active_repository=legacy_repository, cycle_time=cycle_time, stamp=stamp
        )
    )
    stale_tokens, quarantine = _discovery_and_quarantine_context(
        discovery_context, construction_context, discovery, candidate, source_cycle
    )
    assert stale_tokens is None
    assert quarantine is None

    # Different base key (earlier-valid-time fallback): no judgement.
    fallback_terminal = _cohort_candidate_terminal_row(
        cycle_time,
        identity={
            "array_task_id": 0,
            "model_id": "model_a",
            "init_state_id": "state_gfs_model_a_2026062700_gfs_2026062600_f012",
        },
    )
    fallback_root = tmp_path / "fallback-journal"
    _write_json(
        fallback_root / f"latest/gfs/{stamp}/model_a.json",
        _latest_view(cycle_time=cycle_time, jobs=[fallback_terminal]),
    )
    fallback_repository = FileOrchestrationJournalRepository(fallback_root)
    discovery, candidate, source_cycle, discovery_context, construction_context = (
        _discovery_and_quarantine_harness(
            tmp_path, active_repository=fallback_repository, cycle_time=cycle_time, stamp=stamp
        )
    )
    stale_tokens, quarantine = _discovery_and_quarantine_context(
        discovery_context, construction_context, discovery, candidate, source_cycle
    )
    assert stale_tokens is None
    assert quarantine is None


# ---------------------------------------------------------------------------
# §8.7 breaker (#1157): how many PROVEN failed quarantine-convergence attempts
# re-recorded one token.  Two filters compose:
#   - the distinctness key is the forecast cohort MASTER row — reconcile COPIES
#     the identity onto each per-model terminal row under a different
#     ``job_id``, so counting rows instead of masters would double every
#     submission;
#   - the row must carry §8.7 quarantine-rerun provenance for THIS model.
#     Unrelated whitelisted replacements (missing run manifest, missing
#     forecast output) re-record the same token but carry no provenance, and
#     must not pre-arm the breaker into fail-stopping the first quarantine.
# ---------------------------------------------------------------------------

_BREAKER_TOKEN = "state_gfs_model_a_2026062800_gfs_2026062712_f012"
_BREAKER_OTHER_TOKEN = "state_gfs_model_a_2026062800_gfs_2026062800_f000"


def _breaker_identity_entry(
    *, model_id: str = "model_a", init_state_id: str = _BREAKER_TOKEN, array_task_id: int = 0
) -> dict[str, Any]:
    return {
        "array_task_id": array_task_id,
        "model_id": model_id,
        "init_state_id": init_state_id,
    }


def _cohort_master_job(
    cycle_time: datetime,
    *,
    job_suffix: str = "",
    status: str = "succeeded",
    identities: list[dict[str, Any]] | None = None,
    quarantine_rerun_model_ids: list[str] | None = None,
    updated_at: str | None = None,
) -> dict[str, Any]:
    """One forecast cohort MASTER row: model-less, cycle-scoped, identity-bearing.

    ``quarantine_rerun_model_ids=None`` models a row written by something other
    than a §8.7 quarantine rerun — including every journal written before
    #1157, which has no such field at all.
    """
    run_id = f"cycle_gfs_{format_cycle_time(cycle_time)}"
    row = {
        "job_id": f"job_{run_id}_forecast{job_suffix}",
        "run_id": run_id,
        "cycle_id": cycle_id_for("gfs", cycle_time),
        "candidate_id": run_id,
        "job_type": "run_shud_forecast_array",
        "stage": "forecast",
        "status": status,
        "model_id": None,
        "init_state_identities": identities
        if identities is not None
        else [_breaker_identity_entry()],
    }
    if updated_at is not None:
        row["updated_at"] = updated_at
    if quarantine_rerun_model_ids is not None:
        row["journal_predecessor_quarantine_rerun_model_ids"] = list(quarantine_rerun_model_ids)
    return row


def _reconciled_terminal_job(
    cycle_time: datetime,
    *,
    model_id: str = "model_a",
    array_task_id: int = 0,
    identities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The per-model terminal row reconcile copies the master's entry onto."""
    run_id = f"fcst_gfs_{format_cycle_time(cycle_time)}_{model_id}"
    return {
        "job_id": f"job_{run_id}_forecast_reconciled_17667_{array_task_id}",
        "run_id": run_id,
        "cycle_id": cycle_id_for("gfs", cycle_time),
        "candidate_id": (
            f"gfs:{cycle_time.isoformat()}:{model_id}:forecast_gfs_deterministic"
        ),
        "job_type": "run_shud_forecast_array",
        "stage": "forecast",
        "status": "succeeded",
        "model_id": model_id,
        "array_task_id": array_task_id,
        "slurm_job_id": f"17667_{array_task_id}",
        "init_state_identities": identities
        if identities is not None
        else [_breaker_identity_entry(array_task_id=array_task_id)],
    }


def _breaker_journal(
    tmp_path: Path,
    cycle_time: datetime,
    jobs: list[dict[str, Any]],
    *,
    hydro_status: str | None = "complete",
    model_ids: tuple[str, ...] = ("model_a",),
) -> FileOrchestrationJournalRepository:
    journal_root = tmp_path / "journal"
    for model_id in model_ids:
        latest = _latest_view(
            cycle_time=cycle_time,
            model_id=model_id,
            hydro_status=hydro_status,
            jobs=jobs,
        )
        if hydro_status is not None:
            latest["hydro_run"]["init_state_id"] = _BREAKER_TOKEN
        _write_json(
            journal_root / "latest/gfs" / format_cycle_time(cycle_time) / f"{model_id}.json",
            latest,
        )
    return FileOrchestrationJournalRepository(journal_root)


def _breaker_occurrences(
    repository: FileOrchestrationJournalRepository,
    cycle_time: datetime,
    *,
    model_id: str = "model_a",
    init_state_id: str = _BREAKER_TOKEN,
) -> int:
    return repository.completed_pipeline_init_state_id_occurrences(
        source_id="gfs",
        cycle_time=cycle_time,
        model_id=model_id,
        init_state_id=init_state_id,
    )


def test_init_state_occurrences_counts_only_provenance_stamped_masters(
    tmp_path: Path,
) -> None:
    """R1 (#1157 Class B): only PROVEN quarantine reruns count.

    Three same-token masters sit on this cycle: the original defect run (no
    provenance), an unrelated whitelisted replacement (no provenance), and one
    stamped quarantine rerun.  Only the stamped one is a failed convergence
    attempt, so the count is 1 — counting all three would fail-stop a cycle
    whose quarantine had not yet been retried even once.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = _breaker_journal(
        tmp_path,
        cycle_time,
        [
            _cohort_master_job(cycle_time),
            _cohort_master_job(cycle_time, job_suffix="_retry_1"),
            _cohort_master_job(
                cycle_time,
                job_suffix="_retry_2",
                quarantine_rerun_model_ids=["model_a"],
            ),
        ],
    )

    assert _breaker_occurrences(repository, cycle_time) == 1
    # A stamped rerun that recorded a DIFFERENT token does not count for this one.
    assert _breaker_occurrences(repository, cycle_time, init_state_id=_BREAKER_OTHER_TOKEN) == 0


def test_init_state_occurrences_ignores_another_models_provenance(tmp_path: Path) -> None:
    """The stamp is per-model: a cohort rerunning model_b does not arm model_a.

    A single cohort submission can carry a quarantine rerun for one member and
    ordinary work for the others, so a bare "this cohort was a rerun" reading
    would fail-stop innocent models of the same cycle.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = _breaker_journal(
        tmp_path,
        cycle_time,
        [
            _cohort_master_job(
                cycle_time,
                identities=[
                    _breaker_identity_entry(),
                    _breaker_identity_entry(model_id="model_b", array_task_id=1),
                ],
                quarantine_rerun_model_ids=["model_b"],
            )
        ],
        model_ids=("model_a", "model_b"),
    )

    assert _breaker_occurrences(repository, cycle_time, model_id="model_a") == 0
    assert _breaker_occurrences(repository, cycle_time, model_id="model_b") == 1


def test_init_state_occurrences_counts_one_submission_once(tmp_path: Path) -> None:
    """The distinctness pin: master + its reconcile-copied terminal row = 1.

    ``tests/test_file_orchestration_journal.py`` already pins that reconcile
    copies each task's entry onto a per-model terminal row with its own
    ``job_id``; counting rows would read one stamped rerun as two.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = _breaker_journal(
        tmp_path,
        cycle_time,
        [
            _cohort_master_job(cycle_time, quarantine_rerun_model_ids=["model_a"]),
            _reconciled_terminal_job(cycle_time),
        ],
    )

    assert _breaker_occurrences(repository, cycle_time) == 1


@pytest.mark.parametrize(
    "leg",
    [
        "no_journal",
        "blank_token",
        "terminal_row_only",
        "unsucceeded_master",
        "unreadable_identities",
        "legacy_rows_without_provenance_field",
        "unreadable_provenance",
    ],
)
def test_init_state_occurrences_returns_zero_for_uncountable_shapes(
    tmp_path: Path,
    leg: str,
) -> None:
    """Never raises; every uncountable shape is 0 (breaker stays disengaged)."""
    cycle_time = _dt("2026-06-28T00:00:00Z")
    jobs: list[dict[str, Any]] = []
    if leg == "terminal_row_only":
        # A reconcile copy with no surviving master: not a submission record.
        jobs = [_reconciled_terminal_job(cycle_time)]
    elif leg == "unsucceeded_master":
        jobs = [_cohort_master_job(cycle_time, status="running", quarantine_rerun_model_ids=["model_a"])]
    elif leg == "unreadable_identities":
        # Corrupt map shapes: a scalar instead of a list, and a non-mapping entry.
        jobs = [
            _cohort_master_job(cycle_time, identities=[], quarantine_rerun_model_ids=["model_a"]),
            _cohort_master_job(
                cycle_time,
                job_suffix="_retry_1",
                identities=[],
                quarantine_rerun_model_ids=["model_a"],
            ),
        ]
        jobs[0]["init_state_identities"] = _BREAKER_TOKEN
        jobs[1]["init_state_identities"] = ["not-a-mapping"]
    elif leg == "legacy_rows_without_provenance_field":
        # Deploy safety: every journal written before #1157 looks like this.
        # Two same-token masters used to arm the breaker; now they must not.
        jobs = [
            _cohort_master_job(cycle_time),
            _cohort_master_job(cycle_time, job_suffix="_retry_1"),
        ]
    elif leg == "unreadable_provenance":
        jobs = [_cohort_master_job(cycle_time, quarantine_rerun_model_ids=[])]
        jobs[0]["journal_predecessor_quarantine_rerun_model_ids"] = "model_a"

    repository = (
        FileOrchestrationJournalRepository(tmp_path / "journal")
        if leg == "no_journal"
        else _breaker_journal(tmp_path, cycle_time, jobs)
    )

    token = "" if leg == "blank_token" else _BREAKER_TOKEN
    assert _breaker_occurrences(repository, cycle_time, init_state_id=token) == 0


def test_init_state_occurrences_is_not_capped_by_candidate_state_job_limit(
    tmp_path: Path,
) -> None:
    """Truncation pin (#1157 D3): the count reads the journal, not the payload.

    ``candidate_state`` bounds its job list, so counting from that payload
    would silently undercount a busy cycle and never engage the breaker.  Here
    the bounded payload is provably truncated while the accessor still sees
    the stamped rerun master.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    filler = [
        _reconciled_terminal_job(cycle_time, array_task_id=index, identities=[])
        for index in range(1, 8)
    ]
    repository = _breaker_journal(
        tmp_path,
        cycle_time,
        [
            _cohort_master_job(cycle_time),
            _cohort_master_job(
                cycle_time,
                job_suffix="_retry_1",
                quarantine_rerun_model_ids=["model_a"],
            ),
            *filler,
        ],
    )

    bounded = _candidate_state(repository, cycle_time=cycle_time, job_limit=2, event_limit=2)
    assert bounded is not None
    assert bounded["state_truncated"] is True
    assert len(bounded["pipeline_jobs"]) == 2

    assert _breaker_occurrences(repository, cycle_time) == 1


def test_next_current_master_retry_identity_is_stable_after_helper_consolidation() -> None:
    from services.orchestrator.file_orchestration_journal import _next_current_master_retry_identity

    assert _next_current_master_retry_identity({"job_id": "job_forecast", "retry_count": 0}) == (
        "job_forecast_retry_1",
        1,
    )
    assert _next_current_master_retry_identity({"job_id": "job_forecast_retry_2", "retry_count": 0}) == (
        "job_forecast_retry_3",
        3,
    )
    assert _next_current_master_retry_identity({"job_id": "job_forecast_retry_1_retry_2"}) == (
        "job_forecast_retry_1_retry_3",
        3,
    )
    assert _next_current_master_retry_identity({"job_id": "job_forecast", "retry_count": 5}) == (
        "job_forecast_retry_6",
        6,
    )
    assert _next_current_master_retry_identity({"job_id": "job_retry_x", "retry_count": 0}) == (
        "job_retry_x_retry_1",
        1,
    )


# --- #1165 per-cycle event-log segment rotation ----------------------------


def _rotation_job(cycle_time: datetime, **overrides: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "job_id": "job_rotation",
        "run_id": f"fcst_gfs_{format_cycle_time(cycle_time)}_model_a",
        "cycle_id": cycle_id_for("gfs", cycle_time),
        "job_type": "run_shud_forecast_array",
        "model_id": "model_a",
        "status": "queued",
        "stage": "forecast",
        "idempotency_key": f"gfs:{cycle_id_for('gfs', cycle_time)}:model_a:forecast",
        "candidate_id": f"gfs:{cycle_time.isoformat()}:model_a:forecast_gfs_deterministic",
    }
    record.update(overrides)
    return record


def _segment_paths(journal_root: Path, cycle_time: datetime) -> list[str]:
    directory = journal_root / "journal" / "gfs"
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else []


def _tight_repository(journal_root: Path, path: Path, *, headroom: int) -> FileOrchestrationJournalRepository:
    """Repository whose byte limit sits just above the current cycle log.

    Mirrors the live node-22 geometry (#1165): the incident file was 727 bytes
    under the 16 MiB limit, so the next event line could not be appended.
    """
    return FileOrchestrationJournalRepository(journal_root, max_bytes=path.stat().st_size + headroom)


def _single_file_oracle(journal_root: Path, oracle_root: Path, cycle_time: datetime) -> Path:
    """Copy of a rotated journal whose segments are concatenated into one file."""
    import shutil

    shutil.copytree(journal_root, oracle_root)
    directory = oracle_root / "journal" / "gfs"
    cycle_segment = format_cycle_time(cycle_time)
    base = directory / f"{cycle_segment}.jsonl"
    merged = base.read_bytes()
    for index in range(1, 8):
        continuation = directory / f"{cycle_segment}.{index}.jsonl"
        if not continuation.exists():
            break
        merged += continuation.read_bytes()
        continuation.unlink()
    base.write_bytes(merged)
    return oracle_root


def test_file_journal_append_rolls_over_to_a_continuation_segment(tmp_path: Path) -> None:
    """2.1 — an append that cannot fit the newest segment starts the next one.

    Counterfactual: without rollover the append raises
    ``file_journal_byte_limit_exceeded`` and the cycle can never record another
    event, which is exactly the live outage this issue unblocks.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    for index in range(3):
        repository.upsert_pipeline_job(_rotation_job(cycle_time, error_message=f"pad-{index}"))

    base = journal_root / "journal" / "gfs" / f"{format_cycle_time(cycle_time)}.jsonl"
    frozen_base = base.read_bytes()
    tight = _tight_repository(journal_root, base, headroom=16)
    tight.upsert_pipeline_job(_rotation_job(cycle_time, error_message="rolled-over"))

    continuation = journal_root / "journal" / "gfs" / f"{format_cycle_time(cycle_time)}.1.jsonl"
    assert _segment_paths(journal_root, cycle_time) == [
        f"{format_cycle_time(cycle_time)}.1.jsonl",
        f"{format_cycle_time(cycle_time)}.jsonl",
    ]
    # History is never rewritten and every segment stays inside the limit.
    assert base.read_bytes() == frozen_base
    assert base.stat().st_size <= tight.max_bytes
    assert 0 < continuation.stat().st_size <= tight.max_bytes
    assert b"rolled-over" in continuation.read_bytes()

    oracle = FileOrchestrationJournalRepository(
        _single_file_oracle(journal_root, tmp_path / "oracle", cycle_time)
    )
    cycle_id = cycle_id_for("gfs", cycle_time)
    rotated_jobs = tight.query_pipeline_jobs_by_cycle(cycle_id)
    assert rotated_jobs == oracle.query_pipeline_jobs_by_cycle(cycle_id)
    assert [job.get("error_message") for job in rotated_jobs] == ["rolled-over"]
    assert _candidate_state(tight, cycle_time=cycle_time) == _candidate_state(oracle, cycle_time=cycle_time)


def test_file_journal_single_segment_cycle_replays_byte_identically(tmp_path: Path) -> None:
    """2.3 — a cycle that never overflows keeps pre-rotation replay values."""
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    for index in range(3):
        repository.upsert_pipeline_job(_rotation_job(cycle_time, error_message=f"pad-{index}"))

    base = journal_root / "journal" / "gfs" / f"{format_cycle_time(cycle_time)}.jsonl"
    assert _segment_paths(journal_root, cycle_time) == [f"{format_cycle_time(cycle_time)}.jsonl"]
    records = repository._read_jsonl(base)
    assert [record[journal_module._REPLAY_ORDER_FIELD] for record in records] == [1, 2, 3]
    assert [record["sequence"] for record in records] == [1, 2, 3]
    event = repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_rotation",
        event_type="status_changed",
        status_from="queued",
        status_to="running",
    )
    assert event["event_id"] == 4
    assert _segment_paths(journal_root, cycle_time) == [f"{format_cycle_time(cycle_time)}.jsonl"]


def test_file_journal_oversized_record_fails_closed_without_creating_a_segment(
    tmp_path: Path,
) -> None:
    """2.4 — a record larger than the limit still writes nothing at all."""
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root, max_bytes=32)

    with pytest.raises(FileOrchestrationJournalError) as caught:
        repository.upsert_pipeline_job(_rotation_job(cycle_time))
    assert caught.value.reason == "file_journal_byte_limit_exceeded"
    assert caught.value.field == f"journal/gfs/{format_cycle_time(cycle_time)}.jsonl"
    assert _segment_paths(journal_root, cycle_time) == []

    with repository._locked_cycle_write(source_id="gfs", cycle_time=cycle_time):
        with pytest.raises(FileOrchestrationJournalError) as batch_caught:
            repository._append_journal_records_unlocked(
                source_id="gfs",
                cycle_time=cycle_time,
                records=[
                    _journal_record(
                        record_type="pipeline_job",
                        source_id="gfs",
                        cycle_time=cycle_time,
                        payload=_active_job(cycle_time),
                        sequence=1,
                    )
                ],
            )
    assert batch_caught.value.reason == "file_journal_byte_limit_exceeded"
    assert _segment_paths(journal_root, cycle_time) == []


def _oversized_record(cycle_time: datetime, *, sequence: int, padding: int) -> dict[str, Any]:
    payload = _active_job(cycle_time)
    payload["error_message"] = "x" * padding
    return _journal_record(
        record_type="pipeline_job",
        source_id="gfs",
        cycle_time=cycle_time,
        payload=payload,
        sequence=sequence,
    )


def test_file_journal_oversized_record_never_rolls_a_non_empty_segment_over(
    tmp_path: Path,
) -> None:
    """2.4 (non-empty base) — an oversized payload must not trigger rollover.

    A fresh cycle short-circuits the rollover branch on ``existing`` alone, so
    the geometry that reaches the ``len(pending) <= max_bytes`` guard at all
    is a near-full NON-EMPTY segment.  What this leg pins is the observable
    outcome: the byte-limit reason, an untouched base, and no empty
    continuation file.  It does not by itself prove the guard is load-bearing
    — dropping the guard still raises the same reason here, because the
    byte-limit check runs before any write.  The reason-distinguishing kill
    lives in the segment-cap sibling below.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_segment = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    repository.upsert_pipeline_job(_rotation_job(cycle_time, error_message="pad-0"))

    base = journal_root / "journal" / "gfs" / f"{cycle_segment}.jsonl"
    frozen_base = base.read_bytes()
    continuation = journal_root / "journal" / "gfs" / f"{cycle_segment}.1.jsonl"
    tight = _tight_repository(journal_root, base, headroom=16)
    oversized = _oversized_record(cycle_time, sequence=2, padding=tight.max_bytes)
    assert len(json.dumps(oversized, sort_keys=True, separators=(",", ":")).encode()) > tight.max_bytes

    with tight._locked_cycle_write(source_id="gfs", cycle_time=cycle_time):
        with pytest.raises(FileOrchestrationJournalError) as single_caught:
            tight._append_journal_record_unlocked(
                source_id="gfs",
                cycle_time=cycle_time,
                record=oversized,
            )
    assert single_caught.value.reason == "file_journal_byte_limit_exceeded"
    assert base.read_bytes() == frozen_base
    assert not continuation.exists()
    assert _segment_paths(journal_root, cycle_time) == [f"{cycle_segment}.jsonl"]

    with tight._locked_cycle_write(source_id="gfs", cycle_time=cycle_time):
        with pytest.raises(FileOrchestrationJournalError) as batch_caught:
            tight._append_journal_records_unlocked(
                source_id="gfs",
                cycle_time=cycle_time,
                records=[oversized],
            )
    assert batch_caught.value.reason == "file_journal_byte_limit_exceeded"
    assert base.read_bytes() == frozen_base
    assert not continuation.exists()
    assert _segment_paths(journal_root, cycle_time) == [f"{cycle_segment}.jsonl"]


def test_file_journal_oversized_record_at_the_segment_cap_stays_byte_limit(
    tmp_path: Path,
) -> None:
    """2.4 (cap) — segment exhaustion stays distinguishable from an oversized record.

    At the cap the two fail-closed reasons collide: only the
    ``len(pending) <= max_bytes`` guard keeps an oversized record reporting
    ``file_journal_byte_limit_exceeded`` instead of masquerading as
    ``file_journal_segment_limit_exceeded``.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_segment = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    filled = [
        _journal_record(
            record_type="pipeline_job",
            source_id="gfs",
            cycle_time=cycle_time,
            payload=_active_job(cycle_time),
            sequence=sequence,
        )
        for sequence in (1, 2, 3)
    ]
    names = [
        f"{cycle_segment}.jsonl",
        f"{cycle_segment}.1.jsonl",
        f"{cycle_segment}.2.jsonl",
    ]
    for name, record in zip(names, filled, strict=True):
        _write_jsonl(journal_root / "journal" / "gfs" / name, [record])
    last = journal_root / "journal" / "gfs" / names[-1]
    frozen = {name: (journal_root / "journal" / "gfs" / name).read_bytes() for name in names}
    tight = _tight_repository(journal_root, last, headroom=16)
    oversized = _oversized_record(cycle_time, sequence=4, padding=tight.max_bytes)

    with tight._locked_cycle_write(source_id="gfs", cycle_time=cycle_time):
        with pytest.raises(FileOrchestrationJournalError) as caught:
            tight._append_journal_record_unlocked(
                source_id="gfs",
                cycle_time=cycle_time,
                record=oversized,
            )
    assert caught.value.reason == "file_journal_byte_limit_exceeded"
    assert caught.value.reason != "file_journal_segment_limit_exceeded"
    assert {name: (journal_root / "journal" / "gfs" / name).read_bytes() for name in names} == frozen
    assert _segment_paths(journal_root, cycle_time) == sorted(names)

    # A record that DOES fit a fresh segment is what the cap rejects.
    with tight._locked_cycle_write(source_id="gfs", cycle_time=cycle_time):
        with pytest.raises(FileOrchestrationJournalError) as cap_caught:
            tight._append_journal_record_unlocked(
                source_id="gfs",
                cycle_time=cycle_time,
                record=_journal_record(
                    record_type="pipeline_job",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    payload=_active_job(cycle_time),
                    sequence=4,
                ),
            )
    assert cap_caught.value.reason == "file_journal_segment_limit_exceeded"
    assert _segment_paths(journal_root, cycle_time) == sorted(names)


def test_file_journal_batch_append_rolls_the_whole_batch_into_one_segment(
    tmp_path: Path,
) -> None:
    """2.5 — a batch that fits a fresh segment lands there in one piece."""
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    repository.upsert_pipeline_job(_rotation_job(cycle_time, error_message="pad-0"))

    base = journal_root / "journal" / "gfs" / f"{format_cycle_time(cycle_time)}.jsonl"
    frozen_base = base.read_bytes()
    batch = [
        _journal_record(
            record_type="pipeline_job",
            source_id="gfs",
            cycle_time=cycle_time,
            payload=_active_job(cycle_time),
            sequence=sequence,
        )
        for sequence in (2, 3)
    ]
    tight = _tight_repository(journal_root, base, headroom=16)
    with tight._locked_cycle_write(source_id="gfs", cycle_time=cycle_time):
        tight._append_journal_records_unlocked(source_id="gfs", cycle_time=cycle_time, records=batch)

    continuation = journal_root / "journal" / "gfs" / f"{format_cycle_time(cycle_time)}.1.jsonl"
    assert base.read_bytes() == frozen_base
    assert len(continuation.read_text(encoding="utf-8").strip().splitlines()) == 2
    assert [record["sequence"] for record in tight._read_jsonl(continuation, segment_index=1)] == [2, 3]
    assert [
        record[journal_module._REPLAY_ORDER_FIELD]
        for record in tight._read_jsonl(continuation, segment_index=1)
    ] == [
        journal_module.MAX_FILE_JOURNAL_RECORDS + 1,
        journal_module.MAX_FILE_JOURNAL_RECORDS + 2,
    ]


def test_file_journal_cross_segment_sequences_and_event_ids_never_reuse(
    tmp_path: Path,
) -> None:
    """2.6 — the sequence and event-id floors read every segment."""
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    for index in range(3):
        repository.upsert_pipeline_job(_rotation_job(cycle_time, error_message=f"pad-{index}"))

    base = journal_root / "journal" / "gfs" / f"{format_cycle_time(cycle_time)}.jsonl"
    base_sequences = [record["sequence"] for record in repository._read_jsonl(base)]
    tight = _tight_repository(journal_root, base, headroom=16)
    tight.upsert_pipeline_job(_rotation_job(cycle_time, error_message="rolled-over"))
    event = tight.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id="job_rotation",
        event_type="status_changed",
        status_from="queued",
        status_to="running",
    )

    continuation = journal_root / "journal" / "gfs" / f"{format_cycle_time(cycle_time)}.1.jsonl"
    continuation_sequences = [record["sequence"] for record in tight._read_jsonl(continuation, segment_index=1)]
    all_sequences = [*base_sequences, *continuation_sequences]
    assert base_sequences == [1, 2, 3]
    assert continuation_sequences == [4, 5]
    assert all_sequences == sorted(all_sequences)
    assert len(set(all_sequences)) == len(all_sequences)
    # Exact values, not a bound: a floor that missed the continuation segment
    # would hand out an id the segment already used.
    assert event["event_id"] == 5
    assert tight._next_sequence_unlocked(source_id="gfs", cycle_time=cycle_time) == 6
    assert tight._next_accepted_submit_event_id_unlocked(source_id="gfs", cycle_time=cycle_time) == 6

    # A continuation-segment event id far above every base id: the floor is
    # 100 only if the segment was read. A base-only floor would answer 6.
    segment_event = _journal_record(
        record_type="pipeline_event",
        source_id="gfs",
        cycle_time=cycle_time,
        payload={
            "event_id": 99,
            "entity_type": "pipeline_job",
            "entity_id": "job_rotation",
            "event_type": "status_changed",
            "status_from": "queued",
            "status_to": "running",
            "created_at": "2026-06-28T00:05:00Z",
        },
        sequence=2,
    )
    segment_event["event_id"] = 99
    segment_event["entity_id"] = "job_rotation"
    with tight._locked_cycle_write(source_id="gfs", cycle_time=cycle_time):
        tight._append_journal_records_unlocked(
            source_id="gfs",
            cycle_time=cycle_time,
            records=[segment_event],
        )

    assert tight._next_accepted_submit_event_id_unlocked(source_id="gfs", cycle_time=cycle_time) == 100


def test_file_journal_cycle_rows_cache_observes_continuation_segments(tmp_path: Path) -> None:
    """2.7 — rollover alone invalidates the cycle rows cache of another reader.

    The base segment is frozen after rollover, so a fingerprint that watched
    only the base file would serve its stale rows forever.  The second write
    is therefore JOURNAL-ONLY: no latest view and no direct job file is
    rewritten, so the newly created continuation segment is the sole on-disk
    change the fingerprint could possibly notice.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    writer = FileOrchestrationJournalRepository(journal_root)
    for index in range(3):
        writer.upsert_pipeline_job(_rotation_job(cycle_time, error_message=f"pad-{index}"))

    base = journal_root / "journal" / "gfs" / f"{format_cycle_time(cycle_time)}.jsonl"
    reader = FileOrchestrationJournalRepository(journal_root)
    assert reader._cycle_rows(
        source_id="gfs", cycle_time=cycle_time, model_id="model_a"
    ).pipeline_jobs["job_rotation"]["error_message"] == "pad-2"

    # Reuse the last durable record so the continuation carries a real row,
    # then append it as a bare journal write (the 2.5 seam).
    continuation_record = json.loads(json.dumps(writer._read_jsonl(base)[-1]))
    continuation_record.pop(journal_module._REPLAY_ORDER_FIELD, None)
    continuation_record["sequence"] = continuation_record["sequence"] + 1
    continuation_record["payload"]["error_message"] = "rolled-over"
    unchanged_before = {
        path: path.stat().st_mtime_ns
        for path in sorted(
            [
                *(journal_root / "latest").rglob("*.json"),
                *(journal_root / "pipeline-jobs").rglob("*.json"),
            ]
        )
    }
    frozen_base = base.read_bytes()

    tight = _tight_repository(journal_root, base, headroom=16)
    with tight._locked_cycle_write(source_id="gfs", cycle_time=cycle_time):
        tight._append_journal_records_unlocked(
            source_id="gfs",
            cycle_time=cycle_time,
            records=[continuation_record],
        )

    # Nothing but the new segment changed on disk.
    continuation = journal_root / "journal" / "gfs" / f"{format_cycle_time(cycle_time)}.1.jsonl"
    assert continuation.exists()
    assert base.read_bytes() == frozen_base
    assert unchanged_before
    assert {path: path.stat().st_mtime_ns for path in unchanged_before} == unchanged_before

    assert reader._cycle_rows(
        source_id="gfs", cycle_time=cycle_time, model_id="model_a"
    ).pipeline_jobs["job_rotation"]["error_message"] == "rolled-over"


def _hammer_until(
    work: Callable[[], None],
    *,
    stop: threading.Event,
    deadline: float,
    failures: list[BaseException],
    batch: int = 1,
) -> Callable[[], None]:
    def loop() -> None:
        try:
            while not stop.is_set() and time.monotonic() < deadline:
                for _ in range(batch):
                    work()
        except BaseException as error:  # noqa: BLE001 - an unguarded cache race is what this asserts
            failures.append(error)
            stop.set()

    return loop


@contextlib.contextmanager
def _fine_grained_thread_switching() -> Iterator[None]:
    """Drop the GIL switch interval to 1µs for a cache-race hammer.

    ``sys.setswitchinterval`` is a process-global knob, so it is restored on
    the way out.  At the 5ms default a pure-memory hammer takes tens of
    milliseconds to interleave badly enough to crash; at 1µs the same cohort
    crashes in ~1ms, which is what buys the deadlines below their margin.
    """
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)


def _join_all(threads: list[threading.Thread], *, stop: threading.Event) -> None:
    """Join a hammer cohort under one absolute budget.

    A per-thread ``join(timeout=30)`` multiplies by the cohort size (4 threads
    deadlocked = 120s), and asserting inside the join loop leaves the later
    threads unjoined and still running.  One shared deadline, ``stop`` set the
    moment it lapses so a merely slow (not deadlocked) cohort can drain, and
    daemon threads so a truly wedged worker cannot outlive the interpreter.
    """
    limit = time.monotonic() + 30.0
    for thread in threads:
        thread.daemon = True
        thread.start()
    timed_out = False
    for thread in threads:
        thread.join(timeout=max(0.0, limit - time.monotonic()))
        if thread.is_alive():
            stop.set()
            timed_out = True
    stuck = [thread.name for thread in threads if thread.is_alive()]
    assert not timed_out, (
        f"cohort exceeded the 30s budget; still alive: {stuck or 'none (drained after stop)'}"
        " — cache/write lock order deadlock"
    )


def test_file_journal_cycle_rows_cache_sweep_excludes_concurrent_stores(tmp_path: Path) -> None:
    """#1380 red proof — the stale-key sweep is the widest unguarded window.

    ``_apply_record_to_cycle_rows_cache`` is the only full-table iteration
    over ``_cycle_rows_cache``; every append inside a write window runs it
    while other cohort threads sharing the repository keep storing and
    evicting through ``_cache_cycle_rows``.  The cache is filled to its
    production capacity first, so every store also evicts.  Unguarded, the
    sweep thread raises ``RuntimeError: dictionary changed size during
    iteration`` (or the equivalent ``dictionary keys changed`` variant —
    the first is the message the 2026-08-14 production pass recorded).

    Measured on this cohort with ``_cache_lock`` neutralised at runtime
    (6 runs each): 42-73ms to red at the default 5ms switch interval, 0.6-1.3ms
    at 1µs.  The default interval only clears 0.7s/73ms ≈ 9.6x, so the hammer
    runs under ``_fine_grained_thread_switching`` and the 0.7s deadline keeps
    a ≥500x margin over the measured worst case.
    """
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_segment = format_cycle_time(cycle_time)
    other_segment = format_cycle_time(_dt("2026-06-28T06:00:00Z"))
    rows = journal_module._CycleRows()
    for index in range(journal_module.MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES):
        repository._cache_cycle_rows(("gfs", other_segment, f"model_{index}", ("gfs",)), rows, fingerprint=None)
    repository._cache_cycle_rows(("gfs", cycle_segment, None, None), rows, fingerprint=None)
    record = _journal_record(
        record_type="forecast_cycle",
        source_id="gfs",
        cycle_time=cycle_time,
        model_id=None,
        payload={
            "cycle_id": cycle_id_for("gfs", cycle_time),
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "status": "raw_complete",
        },
    )

    failures: list[BaseException] = []
    stop = threading.Event()
    stored = itertools.count()

    def store_cycle_rows() -> None:
        key = ("gfs", other_segment, f"churn_{next(stored)}", ("gfs",))
        repository._cache_cycle_rows(key, rows, fingerprint=None)

    def sweep_stale_keys() -> None:
        repository._apply_record_to_cycle_rows_cache(source_id="gfs", cycle_time=cycle_time, record=record)

    with _fine_grained_thread_switching():
        deadline = time.monotonic() + 0.7
        _join_all(
            [
                threading.Thread(
                    target=_hammer_until(store_cycle_rows, stop=stop, deadline=deadline, failures=failures, batch=50),
                    name="store-a",
                ),
                threading.Thread(
                    target=_hammer_until(store_cycle_rows, stop=stop, deadline=deadline, failures=failures, batch=50),
                    name="store-b",
                ),
                threading.Thread(
                    target=_hammer_until(sweep_stale_keys, stop=stop, deadline=deadline, failures=failures, batch=50),
                    name="sweep",
                ),
            ],
            stop=stop,
        )

    assert not failures, f"{type(failures[0]).__name__}: {failures[0]}"
    assert len(repository._cycle_rows_cache) <= journal_module.MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES


def test_file_journal_read_caches_survive_concurrent_readers_and_a_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1380 — one repository instance shared by concurrent orchestration threads.

    The scheduler builds a single ``active_repository`` per pass and hands it
    to every per-cohort orchestrator in the submission thread pool, so the
    read-side caches take concurrent lookups and evictions through the real
    read paths while a writer holds the cycle write window.  Cache capacities
    are squeezed to 2 so every read evicts.  Unlike the sweep test above this
    one is IO-bound (a ``_cycle_rows`` miss costs milliseconds), so it is a
    lock-order and end-to-end guard rather than a probabilistic race prover:
    a reversed lock order deadlocks it.  Measured with ``_cache_lock``
    neutralised at runtime: green 6/6 for the full deadline at both the 5ms
    default and a 1µs switch interval, i.e. it has no time-to-red to size a
    margin against.  The ``_read_bytes_cache`` race is pinned by the direct
    mutator hammer below instead.
    """
    monkeypatch.setattr(journal_module, "MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES", 2)
    monkeypatch.setattr(journal_module, "MAX_FILE_JOURNAL_READ_CACHE_ENTRIES", 2)
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    cycle_times = [_dt(f"2026-06-28T{hour:02d}:00:00Z") for hour in range(4)]
    writer_cycle = _dt("2026-06-29T00:00:00Z")
    for cycle_time in [*cycle_times, writer_cycle]:
        repository.ensure_forecast_cycle(source_id="gfs", cycle_time=cycle_time)
        repository.upsert_pipeline_job(_rotation_job(cycle_time))
    # Readers read every journal file including the writer's own cycle: the
    # #1600 fix absorbs the mid-open atomic replacement at the read
    # chokepoint, so the same-cycle read/write hammer must stay green
    # (spec: "Two threads read and write the same cycle").  The carve-out
    # that filtered the writer's segment out was removed with it.
    journal_files = sorted(
        path for path in journal_root.rglob("*") if path.is_file() and path.suffix in {".json", ".jsonl"}
    )
    assert journal_files

    failures: list[BaseException] = []
    stop = threading.Event()
    deadline = time.monotonic() + 0.6

    def read_cycle_rows() -> None:
        for cycle_time in cycle_times:
            for model_id in (None, "model_a"):
                repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=model_id)

    def read_journal_bytes() -> None:
        for path in journal_files:
            repository._read_bytes_limited_cached(path)

    def append_records() -> None:
        repository.update_forecast_cycle_status(
            source_id="gfs", cycle_time=writer_cycle, status="raw_complete"
        )

    _join_all(
        [
            threading.Thread(
                target=_hammer_until(read_cycle_rows, stop=stop, deadline=deadline, failures=failures),
                name="cycle-rows-a",
            ),
            threading.Thread(
                target=_hammer_until(read_cycle_rows, stop=stop, deadline=deadline, failures=failures),
                name="cycle-rows-b",
            ),
            threading.Thread(
                target=_hammer_until(read_journal_bytes, stop=stop, deadline=deadline, failures=failures),
                name="read-bytes",
            ),
            threading.Thread(
                target=_hammer_until(append_records, stop=stop, deadline=deadline, failures=failures),
                name="writer",
            ),
        ],
        stop=stop,
    )

    assert not failures, f"{type(failures[0]).__name__}: {failures[0]}"


def test_file_journal_read_bytes_cache_mutators_stay_atomic_under_contention(tmp_path: Path) -> None:
    """#1380 red proof — the read-bytes cache, driven at its mutator seam.

    ``_read_bytes_cache_store`` / ``_read_bytes_cache_drop`` /
    ``_read_bytes_cache_mark_validated`` are the entire mutation surface of
    ``_read_bytes_cache``, and the store path advances ``next(iter(...))``
    over the same dict while evicting.  The end-to-end reader test above
    cannot reach that window — every iteration pays for real IO — so this one
    drives the three mutators directly with the cache pre-filled to its
    production capacity, which makes every store evict.

    Measured with ``_cache_lock`` neutralised at runtime (a no-op context
    manager, i.e. the pre-fix shape): red in 0.5-3.2ms, 14/14, with
    ``RuntimeError: dictionary changed size during iteration`` or its
    ``dictionary keys changed`` sibling — a ≥150x margin under the 0.5s
    deadline.  The oracle is that crash, not the cache accounting:
    ``_read_bytes_cache_total`` does not measurably drift pre-fix.

    ``_direct_jobs_cycle_cache`` is not hammered here — see the note in
    tasks.md D.2.
    """
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    content = b"x" * 64
    signature = (1, len(content), 1)
    capacity = journal_module.MAX_FILE_JOURNAL_READ_CACHE_ENTRIES
    recycled_key = "recycled"
    for index in range(capacity - 1):
        repository._read_bytes_cache_store(f"seed-{index}", signature, content)
    repository._read_bytes_cache_store(recycled_key, signature, content)
    assert len(repository._read_bytes_cache) == capacity

    failures: list[BaseException] = []
    stop = threading.Event()
    stored = itertools.count()

    def store_new_key() -> None:
        repository._read_bytes_cache_store(f"churn-{next(stored)}", signature, content)

    def drop_and_restore() -> None:
        # Net-zero on cache size, so every concurrent store keeps evicting.
        repository._read_bytes_cache_drop(recycled_key)
        repository._read_bytes_cache_store(recycled_key, signature, content)

    def mark_validated() -> None:
        # ``content`` is the identical object that was stored, so this takes
        # the read-modify-write branch rather than returning early.
        repository._read_bytes_cache_mark_validated(recycled_key, content)

    with _fine_grained_thread_switching():
        deadline = time.monotonic() + 0.5
        _join_all(
            [
                threading.Thread(
                    target=_hammer_until(store_new_key, stop=stop, deadline=deadline, failures=failures, batch=50),
                    name="store-a",
                ),
                threading.Thread(
                    target=_hammer_until(store_new_key, stop=stop, deadline=deadline, failures=failures, batch=50),
                    name="store-b",
                ),
                threading.Thread(
                    target=_hammer_until(drop_and_restore, stop=stop, deadline=deadline, failures=failures, batch=50),
                    name="drop-restore",
                ),
                threading.Thread(
                    target=_hammer_until(mark_validated, stop=stop, deadline=deadline, failures=failures, batch=50),
                    name="mark-validated",
                ),
            ],
            stop=stop,
        )

    assert not failures, f"{type(failures[0]).__name__}: {failures[0]}"
    assert len(repository._read_bytes_cache) <= capacity


def _segment_job_record(
    cycle_time: datetime,
    *,
    job_id: str,
    sequence: int,
    status: str = "queued",
    slurm_job_id: str | None = "3001",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload = _active_job(cycle_time)
    payload["job_id"] = job_id
    payload["status"] = status
    payload["slurm_job_id"] = slurm_job_id
    payload["idempotency_key"] = idempotency_key or payload["idempotency_key"]
    return _journal_record(
        record_type="pipeline_job",
        source_id="gfs",
        cycle_time=cycle_time,
        payload=payload,
        sequence=sequence,
    )


def test_file_journal_enumeration_readers_tolerate_continuation_segments(
    tmp_path: Path,
) -> None:
    """2.8 — every journal-wide walker resolves a segment to its base cycle.

    Counterfactual: without the canonical parser
    ``_journal_identity_from_path`` raises ``file_journal_invalid_cycle_time``
    on ``2026062800.1`` and the pipeline-job queries degrade to blocked rows,
    while the cycle source discovery silently skips the segment.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_segment = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    _write_jsonl(
        journal_root / "journal" / "gfs" / f"{cycle_segment}.jsonl",
        [_segment_job_record(cycle_time, job_id="job_base", sequence=1, slurm_job_id="3001")],
    )
    _write_jsonl(
        journal_root / "journal" / "gfs" / f"{cycle_segment}.1.jsonl",
        [
            _segment_job_record(
                cycle_time,
                job_id="job_continuation",
                sequence=2,
                status="reserved",
                slurm_job_id=None,
                idempotency_key="gfs:gfs_2026062800:model_a:continuation",
            )
        ],
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    by_cycle = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
    assert all(job.get("file_journal") is None for job in by_cycle)
    assert {job["job_id"] for job in by_cycle} == {"job_base", "job_continuation"}
    by_run = repository.query_pipeline_jobs_by_run(f"cycle_gfs_{cycle_segment}")
    assert {job["job_id"] for job in by_run} == {"job_base", "job_continuation"}
    by_slurm = repository.query_pipeline_job_by_slurm_id("3001")
    assert by_slurm is not None and by_slurm["job_id"] == "job_base"

    # Cycle source discovery resolves a continuation segment to its base cycle
    # instead of failing on the unparseable "2026062800.1" stem.
    assert repository._cycle_source_ids(cycle_time=cycle_time) == ["gfs"]

    # Reconcile-inventory backfill walks the same surface.
    reserved = repository.query_reserved_unbound_jobs()
    assert [job.job_id for job in reserved] == ["job_continuation"]


def test_file_journal_rollback_scope_iteration_tolerates_continuation_segments(
    tmp_path: Path,
) -> None:
    """2.8 (rollback-scope leg) — quiescence discovery reads segments too."""
    from services.orchestrator.file_orchestration_migration import prepare_file_journal_rollback

    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_segment = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # The rollback fence requires a durable migration marker to invalidate.
    assert FileOrchestrationJournalRepository(journal_root).query_inflight_jobs() == []
    prepare_file_journal_rollback(
        journal_root=journal_root,
        workspace_root=workspace,
        scheduler_state="stopped",
        active_scheduler_processes=0,
        checked_at=datetime.now(UTC),
        checked_by="issue-1165-segment-rollback-scope",
        target_writer_generation="a" * 40,
    )
    _write_jsonl(
        journal_root / "journal" / "gfs" / f"{cycle_segment}.jsonl",
        [
            _segment_job_record(
                cycle_time,
                job_id="job_base",
                sequence=1,
                status="succeeded",
                slurm_job_id="3001",
            )
        ],
    )
    _write_jsonl(
        journal_root / "journal" / "gfs" / f"{cycle_segment}.1.jsonl",
        [
            _segment_job_record(
                cycle_time,
                job_id="job_continuation",
                sequence=2,
                status="reserved",
                slurm_job_id=None,
                idempotency_key="gfs:gfs_2026062800:model_a:continuation",
            )
        ],
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert [job.job_id for job in repository.query_rollback_unsettled_jobs()] == ["job_continuation"]


def test_file_journal_backfill_replays_segments_in_segment_order(tmp_path: Path) -> None:
    """2.13 — the reconcile inventory follows segment order, not path order.

    ``sorted()`` puts ``<cycle>.1.jsonl`` before ``<cycle>.jsonl``, and the
    inventory sync is last-write-wins with no replay arbitration, so a
    path-ordered backfill would resurrect a terminated reservation anchor and
    delete a live one.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_segment = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    key = "gfs:gfs_2026062800:model_a:continuation"
    # settled_in_continuation: base reserves, continuation terminates.
    # revived_in_continuation: base terminates, continuation re-reserves.
    _write_jsonl(
        journal_root / "journal" / "gfs" / f"{cycle_segment}.jsonl",
        [
            _segment_job_record(
                cycle_time,
                job_id="job_settled",
                sequence=1,
                status="reserved",
                slurm_job_id=None,
                idempotency_key=key,
            ),
            _segment_job_record(
                cycle_time,
                job_id="job_revived",
                sequence=2,
                status="succeeded",
                slurm_job_id="3001",
            ),
        ],
    )
    _write_jsonl(
        journal_root / "journal" / "gfs" / f"{cycle_segment}.1.jsonl",
        [
            _segment_job_record(
                cycle_time,
                job_id="job_settled",
                sequence=3,
                status="succeeded",
                slurm_job_id="3002",
            ),
            _segment_job_record(
                cycle_time,
                job_id="job_revived",
                sequence=4,
                status="reserved",
                slurm_job_id=None,
                idempotency_key=key,
            ),
        ],
    )
    repository = FileOrchestrationJournalRepository(journal_root)

    assert [path.name for path in repository._iter_migration_journal_paths()] == [
        f"{cycle_segment}.jsonl",
        f"{cycle_segment}.1.jsonl",
    ]
    assert [job.job_id for job in repository.query_reserved_unbound_jobs()] == ["job_revived"]
    inventory = sorted(path.name for path in (journal_root / "reconcile-inventory").iterdir())
    assert inventory == ["job_revived.json"]


def test_file_journal_segment_bound_fails_closed_at_the_cap(tmp_path: Path) -> None:
    """2.11 — segments per cycle are bounded, with a distinct reason.

    The bound is 3 total because a replay reads every segment: 3 x 16 MiB
    stays under the 64 MiB read-cache budget, while a 4th segment would evict
    the entire process read cache on every replay.
    """
    assert journal_module.MAX_FILE_JOURNAL_CYCLE_SEGMENTS == 3
    assert (
        journal_module.MAX_FILE_JOURNAL_CYCLE_SEGMENTS * journal_module.MAX_FILE_JOURNAL_JSON_BYTES
        < journal_module.MAX_FILE_JOURNAL_READ_CACHE_BYTES
    )
    assert journal_module._LATEST_REPLAY_ORDER_SENTINEL == (
        journal_module.MAX_FILE_JOURNAL_CYCLE_SEGMENTS * journal_module.MAX_FILE_JOURNAL_RECORDS + 1
    )

    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_segment = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(journal_root)
    for index in range(3):
        repository.upsert_pipeline_job(_rotation_job(cycle_time, error_message=f"pad-{index}"))
    base = journal_root / "journal" / "gfs" / f"{cycle_segment}.jsonl"
    tight = _tight_repository(journal_root, base, headroom=16)

    with pytest.raises(FileOrchestrationJournalError) as caught:
        for index in range(64):
            tight.upsert_pipeline_job(_rotation_job(cycle_time, error_message=f"fill-{index}"))
    assert caught.value.reason == "file_journal_segment_limit_exceeded"
    assert caught.value.field == f"journal/gfs/{cycle_segment}.jsonl"
    assert _segment_paths(journal_root, cycle_time) == [
        f"{cycle_segment}.1.jsonl",
        f"{cycle_segment}.2.jsonl",
        f"{cycle_segment}.jsonl",
    ]
    directory = journal_root / "journal" / "gfs"
    assert all(
        (directory / name).stat().st_size <= tight.max_bytes
        for name in _segment_paths(journal_root, cycle_time)
    )


def test_file_journal_segment_containment_and_foreign_names(tmp_path: Path) -> None:
    """2.12 — containment, foreign names and gapped segments.

    A gapped segment is an integrity fault.  Inside the probe window the
    cycle-level enumeration and the recursive walkers must agree on it instead
    of one ignoring what the other reads; outside the window only the walkers
    can see the file, and that residual asymmetry is pinned here too.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_segment = format_cycle_time(cycle_time)

    # Non-numeric suffixes keep today's identity-parsing behaviour.
    foreign_root = tmp_path / "foreign"
    _write_jsonl(
        foreign_root / "journal" / "gfs" / f"{cycle_segment}.x.jsonl",
        [_segment_job_record(cycle_time, job_id="job_foreign", sequence=1)],
    )
    foreign = FileOrchestrationJournalRepository(foreign_root)
    assert foreign.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))[0]["file_journal"][
        "reason"
    ] == "file_journal_invalid_cycle_time"
    assert foreign._cycle_source_ids(cycle_time=cycle_time) == []

    # An out-of-window orphan segment fails closed in every reader that walks
    # the directory.  The cycle-level reader probes exact paths only (index 0
    # through MAX_FILE_JOURNAL_CYCLE_SEGMENTS) and therefore cannot see index
    # 5 at all -- the disclosed bounded-window residual.  Both halves are
    # pinned so a future silent flip in either direction goes red.
    orphan_root = tmp_path / "orphan"
    _write_jsonl(
        orphan_root / "journal" / "gfs" / f"{cycle_segment}.jsonl",
        [_segment_job_record(cycle_time, job_id="job_base", sequence=1)],
    )
    _write_jsonl(
        orphan_root / "journal" / "gfs" / f"{cycle_segment}.5.jsonl",
        [_segment_job_record(cycle_time, job_id="job_orphan", sequence=2)],
    )
    orphan = FileOrchestrationJournalRepository(orphan_root)
    assert orphan.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))[0]["file_journal"][
        "reason"
    ] == "file_journal_segment_gap"
    with pytest.raises(FileOrchestrationJournalError) as discovery_caught:
        orphan._cycle_source_ids(cycle_time=cycle_time)
    assert discovery_caught.value.reason == "file_journal_segment_gap"
    orphan_rows = orphan._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=None)
    assert sorted(orphan_rows.pipeline_jobs) == ["job_base"]

    # A hole at the last in-window slot hides an out-of-window segment behind
    # it: the cycle-level reader returns the (0, 1, 2) prefix silently while
    # the walkers still reject the cycle, this time on the cap rule.
    over_cap_root = tmp_path / "over_cap"
    for index, job_id in ((0, "job_base"), (1, "job_s1"), (2, "job_s2"), (4, "job_s4")):
        suffix = "" if index == 0 else f".{index}"
        _write_jsonl(
            over_cap_root / "journal" / "gfs" / f"{cycle_segment}{suffix}.jsonl",
            [_segment_job_record(cycle_time, job_id=job_id, sequence=index + 1)],
        )
    over_cap = FileOrchestrationJournalRepository(over_cap_root)
    over_cap_rows = over_cap._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=None)
    assert sorted(over_cap_rows.pipeline_jobs) == ["job_base", "job_s1", "job_s2"]
    assert over_cap.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))[0][
        "file_journal"
    ]["reason"] == "file_journal_segment_limit_exceeded"
    with pytest.raises(FileOrchestrationJournalError) as over_cap_caught:
        over_cap._cycle_source_ids(cycle_time=cycle_time)
    assert over_cap_caught.value.reason == "file_journal_segment_limit_exceeded"

    # The same rule applies to the cycle-level exact-path enumeration.
    gapped_root = tmp_path / "gapped"
    _write_jsonl(
        gapped_root / "journal" / "gfs" / f"{cycle_segment}.jsonl",
        [_segment_job_record(cycle_time, job_id="job_base", sequence=1)],
    )
    _write_jsonl(
        gapped_root / "journal" / "gfs" / f"{cycle_segment}.2.jsonl",
        [_segment_job_record(cycle_time, job_id="job_gapped", sequence=2)],
    )
    gapped = FileOrchestrationJournalRepository(gapped_root)
    with pytest.raises(FileOrchestrationJournalError) as gap_caught:
        gapped._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=None)
    assert gap_caught.value.reason == "file_journal_segment_gap"
    assert gapped.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))[0]["file_journal"][
        "reason"
    ] == "file_journal_segment_gap"

    # Segment paths keep the no-follow containment discipline.
    symlink_root = tmp_path / "symlink"
    _write_jsonl(
        symlink_root / "journal" / "gfs" / f"{cycle_segment}.jsonl",
        [_segment_job_record(cycle_time, job_id="job_base", sequence=1)],
    )
    outside = tmp_path / "outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    (symlink_root / "journal" / "gfs" / f"{cycle_segment}.1.jsonl").symlink_to(outside)
    symlinked = FileOrchestrationJournalRepository(symlink_root)
    with pytest.raises(FileOrchestrationJournalError) as symlink_caught:
        symlinked._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id=None)
    assert symlink_caught.value.reason == "file_journal_unreadable"


def test_file_journal_latest_view_still_wins_ties_in_the_last_segment(
    tmp_path: Path,
) -> None:
    """2.14 — the latest-view sentinel stays above every reachable segment line.

    The fixed stride puts a segment-2 line at 200_001, above the pre-rotation
    sentinel (100_001).  This pins that the sentinel was raised in lockstep
    with the stride: leave it at the old value and the journal line outranks
    the latest view, silently inverting precedence.  A single-segment
    byte-identity test can never reach that ordering.
    """
    cycle_time = _dt("2026-06-28T00:00:00Z")
    cycle_segment = format_cycle_time(cycle_time)
    journal_root = tmp_path / "journal"
    stale = _active_job(cycle_time)
    stale["status"] = "queued"
    latest_job = dict(stale)
    latest_job["status"] = "running"
    latest = _latest_view(cycle_time=cycle_time, hydro_status="running", jobs=[latest_job])
    latest["replay"] = {"latest_sequence": 7}
    _write_json(journal_root / "latest" / "gfs" / cycle_segment / "model_a.json", latest)
    for index in range(journal_module.MAX_FILE_JOURNAL_CYCLE_SEGMENTS):
        name = f"{cycle_segment}.jsonl" if index == 0 else f"{cycle_segment}.{index}.jsonl"
        _write_jsonl(
            journal_root / "journal" / "gfs" / name,
            [
                _journal_record(
                    record_type="pipeline_job",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    payload=stale,
                    sequence=7,
                )
            ],
        )
    repository = FileOrchestrationJournalRepository(journal_root)

    rows = repository._cycle_rows(source_id="gfs", cycle_time=cycle_time, model_id="model_a")
    replayed = rows.pipeline_jobs[stale["job_id"]]
    assert replayed[journal_module._REPLAY_ORDER_FIELD] == journal_module._LATEST_REPLAY_ORDER_SENTINEL
    assert replayed["status"] == "running"


# ---------------------------------------------------------------------------
# #1183: forward-only init-state accounting on accepted-submit cohort rows.
# ---------------------------------------------------------------------------


def _cohort_init_state_identity(index: int) -> dict[str, Any]:
    return {
        "array_task_id": index,
        "model_id": f"model_{index}",
        "init_state_id": f"state_gfs_model_{index}_2026072000_gfs_2026071912_f012",
        "init_state_checksum": f"sha256:{index}" + "a" * 63,
        "init_state_uri": f"s3://nhms/states/gfs/model_{index}/2026072000/state.cfg.ic",
        "init_state_valid_time": "2026-07-20T00:00:00Z",
    }


def _reserved_cohort_master(
    tmp_path: Path,
    *,
    member_count: int = 3,
    init_state_identities: Any = None,
) -> tuple[Any, dict[str, Any]]:
    """One versioned accepted-submit forecast master reserved as the chain does."""

    from packages.common.source_identity import normalize_source_id
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        forecast_cohort_digest,
    )
    from services.orchestrator.chain_config import scenario_for_source

    cycle_time = _dt("2026-07-20T00:00:00Z")
    canonical_source_id = normalize_source_id("gfs")
    scenario_id = scenario_for_source(canonical_source_id)
    record: dict[str, Any] = {
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "job_id": "job_cycle_gfs_2026072000_forecast",
        "run_id": "cycle_gfs_2026072000",
        "source_id": canonical_source_id,
        "cycle_id": "gfs_2026072000",
        "job_type": "run_shud_forecast_array",
        "model_id": None,
        "stage": "forecast",
        "idempotency_key": "cycle_gfs_2026072000:forecast",
        "slurm_comment": "nhms_idem:cycle_gfs_2026072000:forecast",
        "submit_outcome": None,
        "restart_stage": "forecast",
        "cohort_members": [
            {
                "array_task_id": index,
                "candidate_id": f"{canonical_source_id}:2026-07-20T00:00:00Z:model_{index}:{scenario_id}",
                "run_id": f"fcst_gfs_2026072000_model_{index}",
                "model_id": f"model_{index}",
                "basin_id": f"basin_{index}",
                "scenario_id": scenario_id,
                "restart_stage": "forecast",
            }
            for index in range(member_count)
        ],
        "submission_attempt": 1,
        "submission_attempt_started_at": cycle_time,
        "expected_slurm_user": None,
        "expected_slurm_account": None,
        "slurm_ownership_required": False,
        "created_at": cycle_time,
        "updated_at": cycle_time,
    }
    if init_state_identities is not None:
        record["init_state_identities"] = init_state_identities
    record["cohort_digest"] = forecast_cohort_digest(record)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    return repository, record


def _bind_and_project_cohort(repository: Any, record: Mapping[str, Any], *, member_count: int) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

    current = repository.query_candidate_state(str(record["idempotency_key"]))
    assert current is not None
    commit = repository.commit_pipeline_job_submit_attempt(
        str(record["idempotency_key"]),
        pipeline_job_id=str(current["job_id"]),
        expected_submission_attempt=int(current.get("submission_attempt") or 1),
        slurm_job_id="17667",
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    assert commit.committed
    repository.project_forecast_cohort_tasks(
        str(record["job_id"]),
        master_slurm_job_id="17667",
        projections=[
            {
                **{key: member[key] for key in ("candidate_id", "run_id", "model_id", "array_task_id")},
                "array_task_outcome": "succeeded",
                "task_slurm_job_id": f"17667_{member['array_task_id']}",
                "restart_stage": "forecast",
                "native_shud_resubmitted": False,
            }
            for member in record["cohort_members"]
        ],
        complete=True,
        master_status="succeeded",
        master_error_code=None,
        reconciliation_decision="matched_bound",
    )
    del member_count


@pytest.mark.parametrize("records_identity", [True, False])
def test_cohort_terminal_rows_carry_their_own_reservation_time_init_state(
    tmp_path: Path,
    records_identity: bool,
) -> None:
    """#1183 task 1.3: the identity is captured once, per model, at reservation.

    Terminal accounting runs on the reconcile side with Slurm facts only, so
    each per-model row copies ITS OWN entry out of the master map by
    ``array_task_id`` — a scalar would stamp 17 of 18 models with a foreign
    lineage.  A pre-change master recorded nothing, and its terminal rows must
    still be written, absent-tolerantly, with no migration.
    """

    member_count = 3
    identities = (
        [_cohort_init_state_identity(index) for index in range(member_count)]
        if records_identity
        else None
    )
    repository, record = _reserved_cohort_master(
        tmp_path,
        member_count=member_count,
        init_state_identities=identities,
    )
    reserved = repository.reserve_pipeline_job(record)

    assert reserved is not None
    expected_master_map = (
        [
            {**_cohort_init_state_identity(index), "init_state_uri": "[object-uri]"}
            for index in range(member_count)
        ]
        if records_identity
        else []
    )
    assert reserved["init_state_identities"] == expected_master_map

    _bind_and_project_cohort(repository, record, member_count=member_count)

    for index in range(member_count):
        terminal = repository.get_pipeline_job(
            f"job_fcst_gfs_2026072000_model_{index}_forecast_reconciled_17667_{index}"
        )
        assert terminal is not None
        assert terminal["status"] == "succeeded"
        if records_identity:
            assert terminal["init_state_identities"] == [expected_master_map[index]]
        else:
            assert terminal["init_state_identities"] == []
    # The master's own map is never rewritten by the accounting pass.
    assert repository.get_pipeline_job(str(record["job_id"]))["init_state_identities"] == (
        expected_master_map
    )


def test_sparse_cohort_map_stamps_only_its_own_task_not_the_list_position(
    tmp_path: Path,
) -> None:
    """A mixed warm/cold cohort: lookup is BY task id, never by list position.

    Only task 2 resolved a warm start, so the master's map has a single entry
    whose ``array_task_id`` is 2 while its list position is 0. A positional
    read would stamp task 0 with task 2's lineage — the exact silent
    mis-attribution the per-task lookup exists to prevent (#1183).
    """

    member_count = 3
    warm_task_id = 2
    repository, record = _reserved_cohort_master(
        tmp_path,
        member_count=member_count,
        init_state_identities=[_cohort_init_state_identity(warm_task_id)],
    )
    reserved = repository.reserve_pipeline_job(record)
    assert reserved is not None
    expected_entry = {
        **_cohort_init_state_identity(warm_task_id),
        "init_state_uri": "[object-uri]",
    }
    assert reserved["init_state_identities"] == [expected_entry]
    assert reserved["init_state_identities"][0]["array_task_id"] == warm_task_id

    _bind_and_project_cohort(repository, record, member_count=member_count)

    for index in range(member_count):
        terminal = repository.get_pipeline_job(
            f"job_fcst_gfs_2026072000_model_{index}_forecast_reconciled_17667_{index}"
        )
        assert terminal is not None
        if index == warm_task_id:
            assert terminal["init_state_identities"] == [expected_entry]
        else:
            assert terminal["init_state_identities"] == []


def test_candidate_row_rejects_init_state_identity_bound_to_another_task(
    tmp_path: Path,
) -> None:
    """A per-model terminal row's single entry must name ITS task and model.

    Without the expected-task/model binding, a mis-copied entry would persist a
    foreign basin's warm-start lineage on this row and read back as a valid
    continuity claim (#1183).
    """

    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    candidate_row = {
        "job_id": "job_fcst_gfs_2026072000_model_0_forecast_reconciled_17667_0",
        "run_id": "fcst_gfs_2026072000_model_0",
        "cycle_id": "gfs_2026072000",
        "job_type": "run_shud_forecast_array",
        "slurm_job_id": "17667_0",
        "array_task_id": 0,
        "model_id": "model_0",
        "status": "succeeded",
        "stage": "forecast",
        "candidate_id": "gfs:2026-07-20T00:00:00Z:model_0:forecast_gfs_deterministic",
        "submit_outcome": "accepted",
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "restart_stage": "forecast",
        "native_shud_resubmitted": False,
    }

    # Its own entry is accepted verbatim.
    own = repository._pipeline_job_row(
        {**candidate_row, "init_state_identities": [_cohort_init_state_identity(0)]}
    )
    assert own["init_state_identities"] == [_cohort_init_state_identity(0)]

    with pytest.raises(FileOrchestrationJournalError) as foreign_task:
        repository._pipeline_job_row(
            {
                **candidate_row,
                "init_state_identities": [{**_cohort_init_state_identity(0), "array_task_id": 2}],
            }
        )
    assert foreign_task.value.field == "init_state_identities.array_task_id"

    with pytest.raises(FileOrchestrationJournalError) as foreign_model:
        repository._pipeline_job_row(
            {
                **candidate_row,
                "init_state_identities": [{**_cohort_init_state_identity(0), "model_id": "model_2"}],
            }
        )
    assert foreign_model.value.field == "init_state_identities.model_id"


@pytest.mark.parametrize(
    ("payload", "expected_field"),
    [
        pytest.param("not-a-list", "init_state_identities", id="not_a_sequence"),
        pytest.param([["array_task_id", 0]], "init_state_identities", id="entry_not_a_mapping"),
        pytest.param(
            [{**_cohort_init_state_identity(0), "init_state_quality": "fresh"}],
            "init_state_identities.init_state_quality",
            id="unknown_field",
        ),
        pytest.param(
            [{key: value for key, value in _cohort_init_state_identity(0).items() if key != "init_state_id"}],
            "init_state_identities.init_state_id",
            id="partial_identity_without_state_id",
        ),
        pytest.param(
            [{**_cohort_init_state_identity(0), "init_state_checksum": ""}],
            "init_state_identities.init_state_checksum",
            id="empty_field_value",
        ),
        pytest.param(
            [{**_cohort_init_state_identity(0), "array_task_id": "0"}],
            "init_state_identities.array_task_id",
            id="non_integer_task_id",
        ),
        pytest.param(
            [_cohort_init_state_identity(0), _cohort_init_state_identity(0)],
            "init_state_identities.array_task_id",
            id="duplicate_task_id",
        ),
        pytest.param(
            [{**_cohort_init_state_identity(0), "array_task_id": 99}],
            "init_state_identities.array_task_id",
            id="task_id_outside_cohort",
        ),
        pytest.param(
            [{**_cohort_init_state_identity(0), "model_id": "model_2"}],
            "init_state_identities.model_id",
            id="model_id_not_the_members_model",
        ),
    ],
)
def test_cohort_init_state_identity_invariant_gate_rejects_malformed_payloads(
    tmp_path: Path,
    payload: Any,
    expected_field: str,
) -> None:
    """A recorded identity is either complete and correctly keyed, or refused."""

    repository, record = _reserved_cohort_master(tmp_path, init_state_identities=payload)

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.reserve_pipeline_job(record)

    assert error.value.field == expected_field
    assert repository.get_pipeline_job(str(record["job_id"])) is None


@pytest.mark.parametrize(
    ("payload", "expected_field"),
    [
        pytest.param("not-a-list", "init_state_identities", id="not_a_sequence"),
        pytest.param([["array_task_id", 0]], "init_state_identities", id="entry_not_a_mapping"),
        pytest.param(
            [{key: value for key, value in _cohort_init_state_identity(0).items() if key != "init_state_id"}],
            "init_state_identities.init_state_id",
            id="partial_identity_without_state_id",
        ),
        pytest.param(
            [{**_cohort_init_state_identity(0), "array_task_id": 99}],
            "init_state_identities.array_task_id",
            id="task_id_outside_cohort",
        ),
        pytest.param(
            [{**_cohort_init_state_identity(0), "model_id": "model_2"}],
            "init_state_identities.model_id",
            id="model_id_not_the_members_model",
        ),
    ],
)
def test_cohort_init_state_identity_gate_rejects_malformed_ordinary_upsert(
    tmp_path: Path,
    payload: Any,
    expected_field: str,
) -> None:
    """The invariant gates must also fire on the ordinary-upsert path (B1).

    Some shapes are sanitized by the durable bounding step into a well-formed
    but DIVERGENT map; those are refused by the frozen check instead of the
    shape gate. Either way the write is rejected, never silently dropped.
    """

    member_count = 3
    repository, record = _reserved_cohort_master(
        tmp_path,
        member_count=member_count,
        init_state_identities=[_cohort_init_state_identity(index) for index in range(member_count)],
    )
    assert repository.reserve_pipeline_job(record) is not None
    durable = repository.get_pipeline_job(str(record["job_id"]))

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.upsert_pipeline_job({**durable, "init_state_identities": payload})

    assert error.value.field in {expected_field, "init_state_identities"}
    assert repository.get_pipeline_job(str(record["job_id"])) == durable


def test_cohort_digest_and_member_projection_ignore_init_state_identity(tmp_path: Path) -> None:
    """F3: historical rows' digest validation must be bit-for-bit unaffected."""

    from services.orchestrator.accepted_submit_identity import (
        _MEMBER_FIELDS,
        forecast_cohort_digest,
        forecast_cohort_identity_is_valid,
        ordered_cohort_members,
    )

    _repository, historical = _reserved_cohort_master(tmp_path)
    with_identity = {
        "init_state_identities": [_cohort_init_state_identity(index) for index in range(3)],
        **historical,
    }

    assert "init_state_identities" not in _MEMBER_FIELDS
    assert forecast_cohort_digest(with_identity) == forecast_cohort_digest(historical)
    assert with_identity["cohort_digest"] == historical["cohort_digest"]
    assert ordered_cohort_members(with_identity["cohort_members"]) == ordered_cohort_members(
        historical["cohort_members"]
    )
    assert forecast_cohort_identity_is_valid(historical) is True
    assert forecast_cohort_identity_is_valid(with_identity) is True


def test_cohort_init_state_identity_is_frozen_and_out_of_the_cycle_scope_projection(
    tmp_path: Path,
) -> None:
    """Frozen from reservation onward, and never replicated into 18 candidate states.

    The master's map is one entry per member; the cycle-scope projection is
    copied into EVERY candidate of the cycle, so the map stays out of it by
    design (the per-model terminal row carries the single entry a reader
    needs).
    """

    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_MASTER_ORDINARY_UPSERT_FIELDS,
    )

    assert "init_state_identities" in ACCEPTED_SUBMIT_MASTER_ORDINARY_UPSERT_FIELDS
    assert "init_state_identities" not in journal_module._CYCLE_SCOPE_JOB_PROJECTION_KEYS

    member_count = 3
    repository, record = _reserved_cohort_master(
        tmp_path,
        member_count=member_count,
        init_state_identities=[_cohort_init_state_identity(index) for index in range(member_count)],
    )
    reserved = repository.reserve_pipeline_job(record)
    assert reserved is not None
    durable = repository.get_pipeline_job(str(record["job_id"]))

    with pytest.raises(FileOrchestrationJournalError) as forged_error:
        repository.upsert_pipeline_job(
            {
                **durable,
                "init_state_identities": [
                    {**_cohort_init_state_identity(0), "init_state_id": "state_forged"}
                ],
            }
        )

    # Reject, never silently keep: a rewrite attempt is an authority violation,
    # and a merge that never copies the incoming value would compare the
    # persisted map against itself and pass (#1183 cross-review B1).
    assert forged_error.value.reason == "file_journal_evidence_invariant_invalid"
    assert forged_error.value.field == "init_state_identities"
    assert repository.get_pipeline_job(str(record["job_id"])) == durable

    # An exact replay of the reserved map is still an idempotent read.
    replayed = repository.upsert_pipeline_job(
        {
            **durable,
            "init_state_identities": [
                _cohort_init_state_identity(index) for index in range(member_count)
            ],
        }
    )
    assert replayed["init_state_identities"] == durable["init_state_identities"]
    assert repository.get_pipeline_job(str(record["job_id"])) == durable

    _bind_and_project_cohort(repository, record, member_count=member_count)
    state = repository.candidate_state(
        source_id="gfs",
        cycle_time=_dt("2026-07-20T00:00:00Z"),
        model_id="model_0",
        run_id="fcst_gfs_2026072000_model_0",
        forcing_version_id="fv_gfs_2026072000_model_0",
        candidate_id="GFS:2026-07-20T00:00:00Z:model_0:forecast_gfs_deterministic",
    )
    assert state is not None
    projected_master = next(
        job for job in state["pipeline_jobs"] if job["job_id"] == record["job_id"]
    )
    terminal_row = next(
        job
        for job in state["pipeline_jobs"]
        if job["job_id"] == "job_fcst_gfs_2026072000_model_0_forecast_reconciled_17667_0"
    )

    assert "init_state_identities" not in projected_master
    assert terminal_row["init_state_identities"] == [
        {**_cohort_init_state_identity(0), "init_state_uri": "[object-uri]"}
    ]


# ---------------------------------------------------------------------------
# #1187: derived per-model rows freeze the mapping exactly like the master row.
# ---------------------------------------------------------------------------


_TERMINAL_TASK_JOB_ID = "job_fcst_gfs_2026072000_model_0_forecast_reconciled_17667_0"


def _durable_pipeline_job_payloads(journal_root: Path, job_id: str) -> list[dict[str, Any]]:
    """Every durable jsonl payload written for one job id, oldest first.

    The public read sanitizes ``*_uri`` values, so an assertion made against
    ``get_pipeline_job`` cannot tell a laundered placeholder apart from a
    correctly persisted URI. Only the jsonl payload can.
    """

    payloads: list[dict[str, Any]] = []
    for path in sorted((journal_root / "journal").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("record_type") != "pipeline_job":
                continue
            payload = record.get("payload") or {}
            if payload.get("job_id") == job_id:
                payloads.append(payload)
    return payloads


def _projected_cohort_with_identities(tmp_path: Path, *, member_count: int = 3) -> tuple[Any, dict[str, Any]]:
    """Reserve, bind and project one cohort whose master carries identities."""

    repository, record = _reserved_cohort_master(
        tmp_path,
        member_count=member_count,
        init_state_identities=[_cohort_init_state_identity(index) for index in range(member_count)],
    )
    assert repository.reserve_pipeline_job(record) is not None
    _bind_and_project_cohort(repository, record, member_count=member_count)
    return repository, record


def test_per_model_row_public_round_trip_cannot_launder_the_durable_state_uri(
    tmp_path: Path,
) -> None:
    """#1187 J9: a public round-trip must not wash ``s3://`` into a placeholder.

    ``get_pipeline_job`` → ``upsert_pipeline_job`` on a derived per-model row
    carries the display-sanitized mapping back into the write path. Without the
    per-model freeze the merge accepts it and the durable lineage evidence is
    replaced by ``[object-uri]``.
    """

    repository, _record = _projected_cohort_with_identities(tmp_path)
    durable_before = _durable_pipeline_job_payloads(tmp_path / "journal", _TERMINAL_TASK_JOB_ID)[-1]
    assert durable_before["init_state_identities"] == [_cohort_init_state_identity(0)]

    public_row = repository.get_pipeline_job(_TERMINAL_TASK_JOB_ID)
    assert public_row["init_state_identities"] == [
        {**_cohort_init_state_identity(0), "init_state_uri": "[object-uri]"}
    ]

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.upsert_pipeline_job(dict(public_row))

    assert error.value.reason == "file_journal_evidence_invariant_invalid"
    assert error.value.field == "init_state_identities"
    assert _durable_pipeline_job_payloads(tmp_path / "journal", _TERMINAL_TASK_JOB_ID)[-1] == (
        durable_before
    )


@pytest.mark.parametrize(
    ("payload", "case"),
    [
        pytest.param(None, "explicit_none", id="explicit_none"),
        pytest.param([], "explicit_empty", id="explicit_empty"),
        pytest.param(
            [{**_cohort_init_state_identity(0), "init_state_id": "state_forged"}],
            "wrong_content",
            id="structurally_valid_wrong_content",
        ),
    ],
)
def test_per_model_row_rejects_a_divergent_init_state_mapping(
    tmp_path: Path,
    payload: Any,
    case: str,
) -> None:
    """#1187 J10/J11: erasing and forging are both divergence, both refused.

    An explicit ``None``/``[]`` would flatten the lineage evidence; a payload
    that is structurally valid but names a different state is the case a
    placeholder-tolerant merge could never catch. Both must be rejected with
    the durable mapping intact.
    """

    repository, _record = _projected_cohort_with_identities(tmp_path)
    durable_before = _durable_pipeline_job_payloads(tmp_path / "journal", _TERMINAL_TASK_JOB_ID)[-1]
    assert durable_before["init_state_identities"] == [_cohort_init_state_identity(0)]
    public_before = repository.get_pipeline_job(_TERMINAL_TASK_JOB_ID)

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.upsert_pipeline_job(
            {**public_before, "init_state_identities": payload}
        )

    assert error.value.reason == "file_journal_evidence_invariant_invalid"
    assert error.value.field == "init_state_identities"
    assert _durable_pipeline_job_payloads(tmp_path / "journal", _TERMINAL_TASK_JOB_ID)[-1] == (
        durable_before
    )
    assert repository.get_pipeline_job(_TERMINAL_TASK_JOB_ID) == public_before
    del case


def test_per_model_freeze_is_typed_and_only_covers_contract_current_rows(
    tmp_path: Path,
) -> None:
    """#1187 J16: the new gate's own negative oracle, plus its historical edge.

    Half of this test is the gate firing with its exact typed error and zero
    durable write; the other half is the boundary it must NOT cross — a
    marker-free per-model row predates this contract, normalization does not
    own its mapping, and gating it would change historical rows' behaviour.
    """

    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CANDIDATE_IMMUTABLE_FIELDS,
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD,
        accepted_submit_row_kind,
    )

    assert ACCEPTED_SUBMIT_CANDIDATE_IMMUTABLE_FIELDS == ("init_state_identities",)

    # Correctly keyed to this row's own task, so the shape gate passes and only
    # the freeze can reject it.
    divergent = [{**_cohort_init_state_identity(0), "init_state_checksum": "sha256:" + "b" * 64}]

    repository, _record = _projected_cohort_with_identities(tmp_path)
    durable_before = _durable_pipeline_job_payloads(tmp_path / "journal", _TERMINAL_TASK_JOB_ID)[-1]
    public_before = repository.get_pipeline_job(_TERMINAL_TASK_JOB_ID)
    assert public_before[ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD] == ACCEPTED_SUBMIT_CONTRACT_VERSION

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.upsert_pipeline_job(
            {**public_before, "status": "failed", "init_state_identities": divergent}
        )
    assert error.value.reason == "file_journal_evidence_invariant_invalid"
    assert error.value.field == "init_state_identities"
    assert _durable_pipeline_job_payloads(tmp_path / "journal", _TERMINAL_TASK_JOB_ID)[-1] == (
        durable_before
    )

    # The historical shape: same row kind, no contract marker. Its mapping is
    # outside the accepted-submit contract, so the write still goes through.
    legacy_job_id = "job_fcst_gfs_2026072000_model_0_forecast_legacy"
    legacy = {
        key: value
        for key, value in public_before.items()
        if key != ACCEPTED_SUBMIT_CONTRACT_VERSION_FIELD
    }
    legacy["job_id"] = legacy_job_id
    legacy["init_state_identities"] = [_cohort_init_state_identity(0)]
    assert repository.upsert_pipeline_job(dict(legacy)) is not None
    # The marker, not the row kind, is what holds this row outside the gate.
    assert accepted_submit_row_kind(repository.get_pipeline_job(legacy_job_id)) == "candidate"

    rewritten = repository.upsert_pipeline_job({**legacy, "init_state_identities": divergent})
    assert rewritten["init_state_identities"] == [
        {**divergent[0], "init_state_uri": "[object-uri]"}
    ]


def test_per_model_upsert_that_omits_the_mapping_keeps_it_silently(tmp_path: Path) -> None:
    """#1187 J17: the freeze must not fail closed on the constructor's default.

    ``_pipeline_job_row`` injects this field unconditionally with an empty
    default, so a gate that compared the incoming row instead of the merged one
    would reject every ordinary per-model upsert that simply does not mention
    the mapping. Today those writes succeed and keep the persisted value; that
    must stay true.
    """

    repository, _record = _projected_cohort_with_identities(tmp_path)
    durable_before = _durable_pipeline_job_payloads(tmp_path / "journal", _TERMINAL_TASK_JOB_ID)[-1]
    assert durable_before["init_state_identities"] == [_cohort_init_state_identity(0)]

    public_before = repository.get_pipeline_job(_TERMINAL_TASK_JOB_ID)
    without_mapping = {
        key: value for key, value in public_before.items() if key != "init_state_identities"
    }
    without_mapping["status"] = "failed"
    without_mapping["error_code"] = "SLURM_TASK_FAILED"

    updated = repository.upsert_pipeline_job(without_mapping)

    assert updated["status"] == "failed"
    durable_after = _durable_pipeline_job_payloads(tmp_path / "journal", _TERMINAL_TASK_JOB_ID)[-1]
    assert durable_after["status"] == "failed"
    assert durable_after["init_state_identities"] == durable_before["init_state_identities"]
    assert durable_after["init_state_identities"] == [_cohort_init_state_identity(0)]


def test_master_public_snapshot_replay_still_fails_closed(tmp_path: Path) -> None:
    """#1187 J12 (design D-B2): the public view is not a valid write payload.

    The public read replaces object URIs with display placeholders, so an
    unmodified replay of a public master snapshot presents a mapping the caller
    does not actually hold. That is refused rather than special-cased — a
    caller needing replay needs a durable read, not a laxer write gate.
    """

    member_count = 3
    repository, record = _reserved_cohort_master(
        tmp_path,
        member_count=member_count,
        init_state_identities=[_cohort_init_state_identity(index) for index in range(member_count)],
    )
    assert repository.reserve_pipeline_job(record) is not None
    job_id = str(record["job_id"])
    public_master = repository.get_pipeline_job(job_id)
    assert public_master["init_state_identities"] == [
        {**_cohort_init_state_identity(index), "init_state_uri": "[object-uri]"}
        for index in range(member_count)
    ]

    with pytest.raises(FileOrchestrationJournalError) as error:
        repository.upsert_pipeline_job(dict(public_master))

    assert error.value.reason == "file_journal_evidence_invariant_invalid"
    assert error.value.field == "init_state_identities"
    # Documentation, not an oracle: at this entry point no single-guard mutation
    # can falsify this line. The contract-current structural master arm always
    # ends at ``file_orchestration_journal.py:1757``
    # (``return _public_scheduler_row(existing)``), so the write below it is
    # unreachable for this row and the row cannot change. Only a two-site
    # mutation (also deleting that early return) flips it. Kept because deleting
    # an assertion mid-review reads as weakening the oracle — but the evidence
    # narrative must not claim it proves zero-write. The same caveat applies to
    # the byte-identical half of #1180 J5/J7/J8.
    assert repository.get_pipeline_job(job_id) == public_master


def test_hydro_run_row_records_initial_state_quality(tmp_path: Path) -> None:
    """#1164: the journal row carries the initial-state quality face.

    Absence of ``init_state_id`` no longer implies a cold start — a packaged-IC
    bootstrap has no state id either — so the decision has to be readable from
    the row itself.
    """
    cycle_time = _dt("2026-07-05T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=cycle_time)

    run = repository.create_hydro_run_from_basin(
        {"source_id": "gfs"},
        {
            "run_id": "fcst_gfs_2026070500_model_a",
            "run_type": "forecast",
            "scenario_id": "scenario_a",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "start_time": cycle_time.isoformat(),
            "end_time": cycle_time.isoformat(),
            "model": {"model_id": "model_a", "basin_version_id": "basin_version_a"},
            "forcing": {"forcing_version_id": "forc_gfs_2026070500_model_a"},
            "initial_state": {
                "state_id": None,
                "ic_file_uri": None,
                "quality": "packaged_calibrated_state",
                "packaged_ic_checksum": "b" * 64,
            },
            "outputs": {
                "run_manifest_uri": "s3://nhms/manifests/run.json",
                "output_uri": "s3://nhms/runs/output",
                "log_uri": "s3://nhms/logs/run.log",
            },
        },
    )

    assert run["init_state_id"] is None
    assert run["quality"] == "packaged_calibrated_state"


# ---------------------------------------------------------------------------
# #1312: permanent-failure marking on file-journal master-row geometry.
# ---------------------------------------------------------------------------


_PERMANENT_FAILURE_MASTER_SLURM_JOB_ID = "17667"


def _cohort_task_projections(
    record: Mapping[str, Any],
    *,
    outcome: str = "failed",
    error_code: str | None = None,
    outcomes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Task projections for one cohort; ``outcomes`` drives a mixed cohort."""

    resolved = list(outcomes) if outcomes is not None else [outcome] * len(record["cohort_members"])
    assert len(resolved) == len(record["cohort_members"])
    return [
        {
            **{key: member[key] for key in ("candidate_id", "run_id", "model_id", "array_task_id")},
            "array_task_outcome": resolved[index],
            "task_slurm_job_id": (
                f"{_PERMANENT_FAILURE_MASTER_SLURM_JOB_ID}_{member['array_task_id']}"
            ),
            "restart_stage": "forecast",
            "native_shud_resubmitted": False,
            "error_code": None if resolved[index] == "succeeded" else error_code,
        }
        for index, member in enumerate(record["cohort_members"])
    ]


def _project_cohort_failure(
    repository: Any,
    record: Mapping[str, Any],
    *,
    error_code: str,
) -> dict[str, int]:
    return repository.project_forecast_cohort_tasks(
        str(record["job_id"]),
        master_slurm_job_id=_PERMANENT_FAILURE_MASTER_SLURM_JOB_ID,
        projections=_cohort_task_projections(record, error_code=error_code),
        complete=True,
        master_status="failed",
        master_error_code=error_code,
        reconciliation_decision="matched_bound",
    )


def _bind_cohort_master(repository: Any, record: Mapping[str, Any]) -> None:
    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

    assert repository.reserve_pipeline_job(dict(record)) is not None
    commit = repository.commit_pipeline_job_submit_attempt(
        str(record["idempotency_key"]),
        pipeline_job_id=str(record["job_id"]),
        expected_submission_attempt=1,
        slurm_job_id=_PERMANENT_FAILURE_MASTER_SLURM_JOB_ID,
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    assert commit.committed


def _terminally_failed_cohort_master(
    tmp_path: Path,
    *,
    error_code: str = "OUT_OF_MEMORY",
    member_count: int = 2,
) -> tuple[Any, dict[str, Any]]:
    """One current-contract master driven to a real terminal FAILED projection."""

    repository, record = _reserved_cohort_master(tmp_path, member_count=member_count)
    _bind_cohort_master(repository, record)
    _project_cohort_failure(repository, record, error_code=error_code)
    persisted = repository.get_pipeline_job(str(record["job_id"]))
    assert persisted is not None
    assert persisted["status"] == "failed"
    return repository, record


def _retry_attempt_failed_cohort_master(
    tmp_path: Path,
    *,
    error_code: str = "NODE_FAILURE",
    attempt: int = 2,
    member_count: int = 2,
) -> tuple[Any, dict[str, Any]]:
    """One terminal master whose attempt lives where production puts it: the job id.

    A cohort master is never re-reserved in place — every retry mints a fresh
    ``<base>_retry_<n>`` reservation whose durable ``retry_count`` stays 0 (the
    journal's clean-reservation invariant), so the effective attempt is only
    recoverable from the id suffix.  That is exactly why the chain recomputes it
    with ``effective_retry_attempt`` before handing the job to the retry service
    (``chain_forecast_execution._retry_job_for_stage_result``).  Building the row
    this way keeps the exhausted-budget tests on a geometry the journal can
    actually persist instead of a hand-forced ``retry_count`` on a base-id row.

    The reservation identity mirrors what the chain would derive for that job id:
    ``_cycle_stage_idempotency_key`` appends the id suffix to the base key.
    """

    from services.orchestrator.accepted_submit_identity import forecast_cohort_digest
    from services.orchestrator.retry_identity import RETRY_JOB_ID_MARKER, effective_retry_attempt

    repository, record = _reserved_cohort_master(tmp_path, member_count=member_count)
    suffix = f"{RETRY_JOB_ID_MARKER}{attempt}"
    record["job_id"] = f"{record['job_id']}{suffix}"
    record["idempotency_key"] = f"{record['idempotency_key']}:{suffix.lstrip('_')}"
    record["slurm_comment"] = f"nhms_idem:{record['idempotency_key']}"
    record["cohort_digest"] = forecast_cohort_digest(record)
    _bind_cohort_master(repository, record)
    _project_cohort_failure(repository, record, error_code=error_code)
    persisted = repository.get_pipeline_job(str(record["job_id"]))
    assert persisted is not None
    assert persisted["status"] == "failed"
    assert int(persisted.get("retry_count") or 0) == 0
    assert effective_retry_attempt(persisted["job_id"], persisted.get("retry_count")) == attempt
    return repository, record


def _submission_failed_cohort_master(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    """One master rejected at submit time, i.e. persisted ``submission_failed``."""

    repository, record = _reserved_cohort_master(tmp_path, member_count=2)
    assert repository.reserve_pipeline_job(dict(record)) is not None
    reserved = repository.get_pipeline_job(str(record["job_id"]))
    rejected = repository.reject_pipeline_job_submit_attempt(
        str(record["idempotency_key"]),
        pipeline_job_id=str(record["job_id"]),
        expected_submission_attempt=int(reserved["submission_attempt"]),
        finished_at=_dt("2026-07-20T00:30:00Z"),
        error_code="SBATCH_REJECTED",
        error_message="scheduler rejected the forecast array",
        stage="forecast",
        job_type="run_shud_forecast_array",
    )
    assert rejected.outcome == "applied"
    return repository, record


def _partially_failed_cohort_master(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    """One master whose cohort projected a mixed succeeded/failed outcome."""

    repository, record = _reserved_cohort_master(tmp_path, member_count=2)
    _bind_cohort_master(repository, record)
    repository.project_forecast_cohort_tasks(
        str(record["job_id"]),
        master_slurm_job_id=_PERMANENT_FAILURE_MASTER_SLURM_JOB_ID,
        projections=_cohort_task_projections(
            record,
            outcomes=["succeeded", "failed"],
            error_code="OUT_OF_MEMORY",
        ),
        complete=True,
        master_status="partially_failed",
        master_error_code="OUT_OF_MEMORY",
        reconciliation_decision="matched_bound",
    )
    return repository, record


def _succeeded_cohort_master(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    """One master whose cohort projected every task as succeeded."""

    repository, record = _reserved_cohort_master(tmp_path, member_count=2)
    _bind_cohort_master(repository, record)
    repository.project_forecast_cohort_tasks(
        str(record["job_id"]),
        master_slurm_job_id=_PERMANENT_FAILURE_MASTER_SLURM_JOB_ID,
        projections=_cohort_task_projections(record, outcome="succeeded"),
        complete=True,
        master_status="succeeded",
        master_error_code=None,
        reconciliation_decision="matched_bound",
    )
    return repository, record


def _cancelled_cohort_master(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    """One master persisted as ``cancelled``.

    ``cancelled`` is a legal persisted master status
    (``ACCEPTED_SUBMIT_MASTER_STATUSES``) that no current typed API produces —
    the cancellation flow parks masters on ``reconcile_unverified`` and lets
    task accounting decide.  The row is therefore written through the journal's
    own write sequence (same outgoing validator as production writes), so an
    impossible shape would fail here rather than fake a passing test.
    """

    repository, record = _terminally_failed_cohort_master(
        tmp_path,
        error_code="SLURM_JOB_CANCELLED",
    )
    job_id = str(record["job_id"])
    source_id = journal_module._source_id_from_job(record)
    cycle_time = journal_module._cycle_time_from_job(record)
    with repository._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
        existing = repository._accepted_submit_job_for_id_unlocked(
            job_id,
            source_id=source_id,
            cycle_time=cycle_time,
        )
        row = {**existing, "status": "cancelled"}
        journal_record = journal_module._journal_record_for_write(
            "pipeline_job",
            row,
            source_id=source_id,
            cycle_time=cycle_time,
            model_id=None,
            sequence=repository._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time),
        )
        repository._validate_outgoing_record(
            journal_record,
            source_id=source_id,
            cycle_time=cycle_time,
            record_type="pipeline_job",
            model_id=None,
        )
        repository._append_journal_records_unlocked(
            source_id=source_id,
            cycle_time=cycle_time,
            records=[journal_record],
        )
        repository._write_pipeline_job_direct_unlocked(row, journal_record)
    return repository, record


_PERMANENT_FAILURE_SOURCE_BUILDERS = {
    "failed": _terminally_failed_cohort_master,
    "submission_failed": _submission_failed_cohort_master,
    "partially_failed": _partially_failed_cohort_master,
    "succeeded": _succeeded_cohort_master,
    "cancelled": _cancelled_cohort_master,
}


def _cohort_master_in_status(tmp_path: Path, status: str) -> tuple[Any, dict[str, Any]]:
    repository, record = _PERMANENT_FAILURE_SOURCE_BUILDERS[status](tmp_path)
    persisted = repository.get_pipeline_job(str(record["job_id"]))
    assert persisted is not None and persisted["status"] == status
    return repository, record


def _master_events(repository: Any, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    source_id = journal_module._source_id_from_job(record)
    cycle_time = journal_module._cycle_time_from_job(record)
    rows = repository._cycle_rows(source_id=source_id, cycle_time=cycle_time, model_id=None)
    return [
        dict(event)
        for event in rows.pipeline_events
        if str(event.get("entity_id") or "") == str(record["job_id"])
    ]


def _permanently_failed_events(repository: Any, record: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        event
        for event in _master_events(repository, record)
        if str(event.get("event_type") or "") == "permanently_failed"
    ]


@pytest.mark.parametrize("error_code", ["OUT_OF_MEMORY", "INVALID_MANIFEST"])
def test_master_row_declined_for_auto_retry_is_marked_permanently_failed(
    tmp_path: Path,
    error_code: str,
) -> None:
    """#1312 seam 2 + seam 10: the dormant journal arm lands the mark itself.

    ``handle_failed_job``'s master branch used to return the row untouched when
    auto retry was declined, so the SHALL of ``job-retry-mechanism`` never held
    on master-row geometry.
    """

    repository, record = _terminally_failed_cohort_master(tmp_path, error_code=error_code)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    job = repository.get_pipeline_job(str(record["job_id"]))

    handled = service.handle_failed_job(job)

    persisted = repository.get_pipeline_job(str(record["job_id"]))
    reopened = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(str(record["job_id"]))
    assert handled.job_id == str(record["job_id"])
    assert handled.status == "permanently_failed"
    assert persisted["status"] == "permanently_failed"
    assert reopened["status"] == "permanently_failed"
    assert persisted["error_code"] == error_code
    events = _permanently_failed_events(repository, record)
    assert len(events) == 1
    assert events[0]["status_from"] == "failed"
    assert events[0]["status_to"] == "permanently_failed"
    assert events[0]["details"]["automatic_retry_stopped"] is True
    assert events[0]["details"]["last_error"] == error_code


@pytest.mark.parametrize("error_kind", ["orchestrator_error", "journal_error"])
def test_master_row_decline_survives_a_failed_permanent_failure_mark(
    tmp_path: Path,
    error_kind: str,
) -> None:
    """#1312 C-1, dormant arm: a journal failure falls back, it does not raise.

    ``handle_failed_job`` used to return the row untouched on a decline; the
    mark is new journal I/O on that path, so a journal write failure must
    degrade to exactly that pre-#1312 return value (the mark is idempotent and
    the next pass re-lands it) instead of escaping into the caller.

    Degrading is not the same as going silent (C-P2): the spec's write-failure
    THEN covers *every* decline exit, so this arm owes the same
    ``permanent_failure_mark_failed`` operator signal the orchestrator-cycle arm
    emits.

    Both halves of the catch tuple are injected (#1312 round-4, mutant o1): the
    journal's write sequence raises ``FileOrchestrationJournalError`` from its
    own validators but ``OrchestratorError`` from the append/durability layer
    (``_ensure_root_unlocked``), so pinning only one class leaves narrowing the
    tuple to the other one undetected.
    """

    repository, record = _terminally_failed_cohort_master(tmp_path, error_code="OUT_OF_MEMORY")
    expected_reason = (
        "FILE_JOURNAL_WRITE_FAILED" if error_kind == "orchestrator_error" else "file_journal_write_failed"
    )

    def _refuse(job_id: str, **_kwargs: Any) -> Any:
        if error_kind == "orchestrator_error":
            raise OrchestratorError(
                "FILE_JOURNAL_WRITE_FAILED",
                "failed to append file orchestration journal records",
                {"job_id": job_id},
            )
        raise FileOrchestrationJournalError("file_journal_write_failed", field=job_id)

    repository.mark_pipeline_job_permanently_failed = _refuse  # type: ignore[method-assign]
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    job = repository.get_pipeline_job(str(record["job_id"]))

    handled = service.handle_failed_job(job)

    assert handled.job_id == str(record["job_id"])
    assert handled.status == "failed"
    del repository.mark_pipeline_job_permanently_failed
    assert repository.get_pipeline_job(str(record["job_id"]))["status"] == "failed"
    assert _permanently_failed_events(repository, record) == []
    mark_failures = [
        event
        for event in _master_events(repository, record)
        if str(event.get("event_type") or "") == "permanent_failure_mark_failed"
    ]
    assert len(mark_failures) == 1
    assert mark_failures[0]["details"]["reason"] == expected_reason
    assert mark_failures[0]["details"]["retry_mark_pending"] is True
    assert (mark_failures[0]["status_from"], mark_failures[0]["status_to"]) == ("failed", "failed")


def test_master_row_with_transient_code_and_budget_keeps_retry_identity(tmp_path: Path) -> None:
    """#1312 seam 6: the reverse control — no mark on a retryable master row."""

    repository, record = _terminally_failed_cohort_master(tmp_path, error_code="NODE_FAILURE")
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    job = repository.get_pipeline_job(str(record["job_id"]))

    handled = service.handle_failed_job(job)

    persisted = repository.get_pipeline_job(str(record["job_id"]))
    assert handled.status == "pending"
    assert handled.job_id != str(record["job_id"])
    assert persisted["status"] == "failed"
    assert _permanently_failed_events(repository, record) == []


def test_master_row_with_exhausted_transient_budget_is_marked_permanently_failed(tmp_path: Path) -> None:
    """#1312 seam 7 (design D6): the exhausted-budget half of the decline arm.

    Production geometry, not a forced counter: the third attempt of a cohort
    master is a ``..._forecast_retry_2`` reservation whose durable
    ``retry_count`` is still 0, and the caller resolves the effective attempt
    from the id suffix with ``effective_retry_attempt`` before handing the job
    over (``chain_forecast_execution._retry_job_for_stage_result``).  Stamping
    ``retry_count=2`` onto a base-id master would exercise a row shape the
    journal never writes.
    """

    from services.orchestrator.retry_identity import effective_retry_attempt

    repository, record = _retry_attempt_failed_cohort_master(
        tmp_path,
        error_code="NODE_FAILURE",
        attempt=2,
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=2, backoff_schedule=[0]))
    persisted_before = repository.get_pipeline_job(str(record["job_id"]))
    job = {
        **persisted_before,
        "retry_count": effective_retry_attempt(
            persisted_before["job_id"],
            persisted_before.get("retry_count"),
        ),
    }

    handled = service.handle_failed_job(job)

    persisted = repository.get_pipeline_job(str(record["job_id"]))
    events = _permanently_failed_events(repository, record)
    assert handled.status == "permanently_failed"
    assert persisted["status"] == "permanently_failed"
    # The mark relabels; it never writes the caller's resolved attempt back.
    assert int(persisted.get("retry_count") or 0) == 0
    assert len(events) == 1
    assert events[0]["details"]["failure"]["limit_exhausted"] is True


def test_permanent_failure_transition_preserves_accepted_submit_accounting(tmp_path: Path) -> None:
    """#1312 seam 4: the typed transition relabels, it does not re-account."""

    repository, record = _terminally_failed_cohort_master(tmp_path)
    before = repository.get_pipeline_job(str(record["job_id"]))

    result = repository.mark_pipeline_job_permanently_failed(
        str(record["job_id"]),
        error_code="OUT_OF_MEMORY",
        error_message="array tasks ran out of memory",
        finished_at=_dt("2026-07-20T01:00:00Z"),
        event_details={"automatic_retry_stopped": True},
    )

    after = repository.get_pipeline_job(str(record["job_id"]))
    assert result.outcome == "applied"
    assert result.wrote is True
    assert after["status"] == "permanently_failed"
    for field in (
        "reconciliation_decision",
        "submit_outcome",
        "matched_slurm_job_id",
        "reconciliation_source",
        "reconciliation_reason_class",
        "identity_blocked_streak",
        "candidate_projections",
        "slurm_job_id",
        "submission_attempt",
        "cohort_digest",
        "cohort_members",
        "idempotency_key",
    ):
        assert after[field] == before[field], field
    assert (
        after["reconciliation_decision"],
        after["submit_outcome"],
        after["matched_slurm_job_id"],
    ) == ("matched_bound", "accepted", _PERMANENT_FAILURE_MASTER_SLURM_JOB_ID)
    assert after["error_code"] == "OUT_OF_MEMORY"
    assert len(_permanently_failed_events(repository, record)) == 1


@pytest.mark.parametrize("live_status", ["reserved", "running"])
def test_permanent_failure_transition_reports_stale_for_live_master_rows(
    tmp_path: Path,
    live_status: str,
) -> None:
    """#1312 seam 4: a non-terminal source is a stale caller, never an error."""

    repository, record = _reserved_cohort_master(tmp_path, member_count=2)
    if live_status == "reserved":
        assert repository.reserve_pipeline_job(dict(record)) is not None
    else:
        _bind_cohort_master(repository, record)
        assert repository.transition_pipeline_job_runtime_status(
            str(record["job_id"]),
            "running",
        ).committed
    before = repository.get_pipeline_job(str(record["job_id"]))
    events_before = _master_events(repository, record)

    result = repository.mark_pipeline_job_permanently_failed(
        str(record["job_id"]),
        error_code="OUT_OF_MEMORY",
    )

    after = repository.get_pipeline_job(str(record["job_id"]))
    assert result.outcome == "stale"
    assert result.committed is False
    assert after == before
    assert _master_events(repository, record) == events_before


@pytest.mark.parametrize("source_status", ["failed", "submission_failed"])
def test_permanent_failure_transition_marks_every_markable_failure_source(
    tmp_path: Path,
    source_status: str,
) -> None:
    """#1312 seam 4: the whole legal source domain, one real builder each.

    ``failed`` alone would leave ``submission_failed`` unproven, so a narrowed
    source set would still pass the suite while silently dropping half of the
    terminal-failure domain.
    """

    repository, record = _cohort_master_in_status(tmp_path, source_status)

    result = repository.mark_pipeline_job_permanently_failed(
        str(record["job_id"]),
        error_code="INVALID_MANIFEST",
        error_message="permanent failure recorded by the retry decline",
        finished_at=_dt("2026-07-20T02:00:00Z"),
        event_details={"automatic_retry_stopped": True},
    )

    after = repository.get_pipeline_job(str(record["job_id"]))
    assert result.outcome == "applied"
    assert after["status"] == "permanently_failed"
    assert after["error_code"] == "INVALID_MANIFEST"
    events = _permanently_failed_events(repository, record)
    assert len(events) == 1
    assert events[0]["status_from"] == source_status
    assert events[0]["status_to"] == "permanently_failed"


@pytest.mark.parametrize("source_status", ["succeeded", "cancelled", "partially_failed"])
def test_permanent_failure_transition_declines_non_failure_terminal_sources(
    tmp_path: Path,
    source_status: str,
) -> None:
    """#1312 seam 4: terminal but not failed is still outside the source set.

    A widened source set would relabel a succeeded or cancelled master as
    permanently failed — a fabricated failure, not a recorded one.
    ``partially_failed`` sits here too (C-P1): its cohort is still advancing
    downstream under the #1202 partial-advance contract, so marking it would
    convert a partial into a whole-job permanent failure.
    """

    repository, record = _cohort_master_in_status(tmp_path, source_status)
    before = repository.get_pipeline_job(str(record["job_id"]))
    events_before = _master_events(repository, record)

    result = repository.mark_pipeline_job_permanently_failed(
        str(record["job_id"]),
        error_code="INVALID_MANIFEST",
        error_message="permanent failure recorded by the retry decline",
        finished_at=_dt("2026-07-20T02:00:00Z"),
    )

    assert result.outcome == "stale"
    assert result.committed is False
    assert repository.get_pipeline_job(str(record["job_id"])) == before
    assert _master_events(repository, record) == events_before
    assert _permanently_failed_events(repository, record) == []


def test_permanent_failure_transition_declines_released_reservations(tmp_path: Path) -> None:
    """#1312: ``reservation_lost`` is outside the markable source set.

    This is the ``identity_mismatch_released`` sub-shape.  The decline now comes
    from ``PERMANENT_FAILURE_SOURCE_STATUSES`` alone (the decision-specific
    guard is gone), which is what keeps the mark from corrupting an accounting
    tuple whose decision may only coexist with ``status="reservation_lost"``.
    """

    repository, record = _reserved_cohort_master(tmp_path, member_count=2)
    assert repository.reserve_pipeline_job(dict(record)) is not None
    reserved = repository.get_pipeline_job(str(record["job_id"]))
    assert repository.release_identity_blocked_reservation(
        str(record["job_id"]),
        accepted_submit_contract_version=reserved["accepted_submit_contract_version"],
        expected_submission_attempt=int(reserved["submission_attempt"]),
        expected_submission_attempt_started_at=reserved["submission_attempt_started_at"],
        identity_blocked_streak=3,
    ) == 1
    before = repository.get_pipeline_job(str(record["job_id"]))
    assert before["status"] == "reservation_lost"

    result = repository.mark_pipeline_job_permanently_failed(str(record["job_id"]))

    assert result.outcome == "stale"
    assert repository.get_pipeline_job(str(record["job_id"])) == before
    assert _permanently_failed_events(repository, record) == []


def test_permanent_failure_transition_declines_an_abandoned_reservation(tmp_path: Path) -> None:
    """#1312 seam 4: the ``absence_retry_permitted`` sub-shape stays reclaimable.

    A lost reservation is reclaim-pending, not permanently failed.  Marking it
    would slam the reclaim door shut, because
    ``reclaim_pipeline_job_reservation`` keys off the literal
    ``status == "reservation_lost"``.  The mark is declined as stale and the
    door is proved still open afterwards.
    """

    repository, record = _reserved_cohort_master(tmp_path, member_count=2)
    assert repository.reserve_pipeline_job(dict(record)) is not None
    reserved = repository.get_pipeline_job(str(record["job_id"]))
    assert repository.permit_pipeline_job_retry(
        str(record["job_id"]),
        accepted_submit_contract_version=reserved["accepted_submit_contract_version"],
        expected_submission_attempt=int(reserved["submission_attempt"]),
        expected_submission_attempt_started_at=reserved["submission_attempt_started_at"],
    ) == 1
    before = repository.get_pipeline_job(str(record["job_id"]))
    assert before["status"] == "reservation_lost"
    assert before["reconciliation_decision"] == "absence_retry_permitted"
    events_before = _master_events(repository, record)

    result = repository.mark_pipeline_job_permanently_failed(
        str(record["job_id"]),
        error_code="INVALID_MANIFEST",
    )

    assert result.outcome == "stale"
    assert result.committed is False
    assert repository.get_pipeline_job(str(record["job_id"])) == before
    assert _master_events(repository, record) == events_before
    assert _permanently_failed_events(repository, record) == []
    reclaimed = repository.reclaim_pipeline_job_reservation(
        {
            **before,
            "expected_submission_attempt": before["submission_attempt"],
            "expected_submission_attempt_started_at": before["submission_attempt_started_at"],
            "status": "reserved",
            "submission_attempt": int(before["submission_attempt"]) + 1,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "matched_slurm_job_id": None,
        }
    )
    assert reclaimed is not None
    assert reclaimed["status"] == "reserved"


def test_generic_status_update_still_refuses_master_rows_after_the_typed_transition(
    tmp_path: Path,
) -> None:
    """#1312 seam 4: the new API does not loosen the master write ban."""

    repository, record = _terminally_failed_cohort_master(tmp_path)

    with pytest.raises(FileOrchestrationJournalError) as raised:
        repository.update_pipeline_job_status(
            str(record["job_id"]),
            "permanently_failed",
        )

    assert raised.value.reason == "file_journal_authority_transition_requires_typed_api"
    assert repository.get_pipeline_job(str(record["job_id"]))["status"] == "failed"


def test_master_permanent_failure_marking_is_idempotent_against_stale_snapshots(
    tmp_path: Path,
) -> None:
    """#1312 seam 5, direction A: a stale ``failed`` snapshot re-marks nothing.

    The service returns a namespace either way, so the re-drive is also pinned
    at the typed-API level (#1312 round-4, mutant i): a second mark on an
    already-marked row must report ``idempotent``/committed, not ``stale``.
    Dropping the idempotent short-circuit keeps the row and the event count
    correct — it only degrades the outcome an operator/caller reads back.
    """

    repository, record = _terminally_failed_cohort_master(tmp_path)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    stale_snapshot = dict(repository.get_pipeline_job(str(record["job_id"])))

    first = service.mark_permanently_failed(stale_snapshot)
    second = service.mark_permanently_failed(stale_snapshot)
    third = service.handle_failed_job(stale_snapshot)
    re_marked = repository.mark_pipeline_job_permanently_failed(
        str(record["job_id"]),
        error_code="OUT_OF_MEMORY",
        error_message="array tasks ran out of memory",
    )

    assert first.status == "permanently_failed"
    assert second.status == "permanently_failed"
    assert third.status == "permanently_failed"
    assert re_marked.outcome == "idempotent"
    assert re_marked.committed is True
    assert re_marked.wrote is False
    assert re_marked.row is not None and re_marked.row["status"] == "permanently_failed"
    assert repository.get_pipeline_job(str(record["job_id"]))["status"] == "permanently_failed"
    assert len(_permanently_failed_events(repository, record)) == 1


def test_master_permanent_failure_marking_ignores_a_stale_marked_snapshot(tmp_path: Path) -> None:
    """#1312 seam 5, direction B: a snapshot claiming the mark cannot skip it.

    The service-level snapshot gate would otherwise return early and leave the
    persisted row on ``failed`` forever (design D4 / round-2 P2-E).
    """

    repository, record = _terminally_failed_cohort_master(tmp_path)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    lying_snapshot = {
        **repository.get_pipeline_job(str(record["job_id"])),
        "status": "permanently_failed",
    }

    marked = service.mark_permanently_failed(lying_snapshot)

    assert marked.status == "permanently_failed"
    assert repository.get_pipeline_job(str(record["job_id"]))["status"] == "permanently_failed"
    assert len(_permanently_failed_events(repository, record)) == 1


def test_cohort_projection_keeps_a_permanently_failed_master_sticky(tmp_path: Path) -> None:
    """#1312 seam 9 core (design D9): the mark survives re-projection.

    ``project_forecast_cohort_tasks`` derives ``master_status`` from the task
    projections, so without terminal stickiness the next resume pass rewrote the
    mark back to ``failed`` and oscillated with a fresh status-change event.
    """

    repository, record = _terminally_failed_cohort_master(tmp_path)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    service.mark_permanently_failed(repository.get_pipeline_job(str(record["job_id"])))
    events_before = _master_events(repository, record)

    counts = _project_cohort_failure(repository, record, error_code="OUT_OF_MEMORY")

    after = repository.get_pipeline_job(str(record["job_id"]))
    assert after["status"] == "permanently_failed"
    assert counts == {"total": 0, "pipeline_status": 0, "pipeline_event": 0}
    assert _master_events(repository, record) == events_before
    assert len(_permanently_failed_events(repository, record)) == 1


def test_cohort_projection_writes_through_new_evidence_without_losing_the_mark(
    tmp_path: Path,
) -> None:
    """#1312 (design D9): stickiness holds on the WRITE branch, not just the no-op.

    The companion test above re-projects byte-identical evidence, so the whole
    cohort write short-circuits and the sticky line is never exercised.  Here
    the second pass carries genuinely new evidence (finished_at / exit_code /
    log_uri / error message / task error code), so the cohort row really is
    rewritten — and the mark still has to survive that rewrite.
    """

    repository, record = _terminally_failed_cohort_master(tmp_path)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    service.mark_permanently_failed(repository.get_pipeline_job(str(record["job_id"])))
    marked = repository.get_pipeline_job(str(record["job_id"]))
    assert marked["status"] == "permanently_failed"
    log_uri = "s3://nhms/logs/cycle_gfs_2026072000/forecast-accounting-pass-2.log"
    finished_at = _dt("2026-07-21T03:00:00Z")

    counts = repository.project_forecast_cohort_tasks(
        str(record["job_id"]),
        master_slurm_job_id=_PERMANENT_FAILURE_MASTER_SLURM_JOB_ID,
        projections=_cohort_task_projections(record, error_code="SLURM_ARRAY_TASK_FAILED"),
        complete=True,
        master_status="failed",
        master_error_code="SLURM_ARRAY_TASK_FAILED",
        reconciliation_decision="matched_bound",
        finished_at=finished_at,
        exit_code=137,
        master_error_message="second accounting pass re-observed the same failure",
        log_uri=log_uri,
    )

    after = repository.get_pipeline_job(str(record["job_id"]))
    reopened = FileOrchestrationJournalRepository(repository.root).get_pipeline_job(str(record["job_id"]))
    # The write branch really ran: one cohort row plus its status-change event.
    assert counts == {"total": 2, "pipeline_status": 1, "pipeline_event": 1}
    # ``log_uri`` is stored through the durable redaction filter, so the proof
    # is that the field moved at all, not that it kept the literal URI.
    assert after["log_uri"] not in (None, "", marked["log_uri"])
    assert after["exit_code"] == 137
    assert after["finished_at"] == journal_module._format_utc(finished_at)
    assert after["finished_at"] != marked["finished_at"]
    # ... and the mark survived it, in memory and on reopen.
    assert after["status"] == "permanently_failed"
    assert reopened["status"] == "permanently_failed"
    assert (
        after["reconciliation_decision"],
        after["submit_outcome"],
        after["matched_slurm_job_id"],
    ) == (
        marked["reconciliation_decision"],
        marked["submit_outcome"],
        marked["matched_slurm_job_id"],
    )
    assert len(_permanently_failed_events(repository, record)) == 1


def test_cohort_projection_still_writes_terminal_status_for_unmarked_masters(tmp_path: Path) -> None:
    """#1312: stickiness is scoped to ``permanently_failed`` (design non-goal)."""

    repository, record = _reserved_cohort_master(tmp_path, member_count=2)
    _bind_cohort_master(repository, record)

    _project_cohort_failure(repository, record, error_code="OUT_OF_MEMORY")

    assert repository.get_pipeline_job(str(record["job_id"]))["status"] == "failed"


def _non_master_permanently_failed_details(
    tmp_path: Path,
    *,
    error_code: str | None,
    retry_count: int = 0,
    max_retries: int = 3,
) -> dict[str, Any]:
    """Drive one plain (non-master) journal row to its permanent-failure event."""

    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_auto_retry_skipped")
    record["retry_count"] = retry_count
    repository.reserve_pipeline_job(record)
    repository.update_pipeline_job_status(
        "job_auto_retry_skipped",
        "failed",
        error_code=error_code,
        error_message="stage failed",
        finished_at=cycle_time,
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=max_retries, backoff_schedule=[0]))

    handled = service.handle_failed_job(repository.get_pipeline_job("job_auto_retry_skipped"))

    assert handled.status == "permanently_failed"
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    events = [event for event in state["pipeline_events"] if event["event_type"] == "permanently_failed"]
    assert len(events) == 1
    return dict(events[0]["details"])


@pytest.mark.parametrize("error_code", _spec_non_transient_error_codes())
def test_file_journal_permanently_failed_event_carries_auto_retry_skipped(
    tmp_path: Path,
    error_code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1314: the file-journal plane's non-master production point emits the payload."""

    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        details = _non_master_permanently_failed_details(tmp_path, error_code=error_code)

    assert details["auto_retry_skipped"] is True
    assert details["reason"] == "non_transient_error"
    assert details["error_code"] == error_code
    assert details["failure"]["retryable"] is False
    assert details["automatic_retry_stopped"] is True
    assert _auto_retry_skipped_warnings(caplog) == []


def test_file_journal_permanently_failed_event_flags_an_unknown_error_code_and_warns_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1314: the spec warning fires once per appended event, from the shared helper."""

    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        details = _non_master_permanently_failed_details(tmp_path, error_code=_UNKNOWN_ERROR_CODE)

    assert details["auto_retry_skipped"] is True
    assert details["reason"] == "unknown_error_code_defaulted_non_transient"
    assert details["error_code"] == _UNKNOWN_ERROR_CODE
    assert _auto_retry_skipped_warnings(caplog) == [_unknown_error_code_warning(_UNKNOWN_ERROR_CODE)]


def test_file_journal_permanently_failed_event_omits_auto_retry_skipped_when_budget_exhausted(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1314 discrimination rule: an exhausted TRANSIENT code is not guard-blocked."""

    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        details = _non_master_permanently_failed_details(tmp_path, error_code="SLURM_TIMEOUT", retry_count=3)

    assert "auto_retry_skipped" not in details
    assert details["failure"]["limit_exhausted"] is True
    assert _auto_retry_skipped_warnings(caplog) == []


def test_file_journal_permanently_failed_event_omits_auto_retry_skipped_without_an_error_code(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1314 edge: a missing code is not a code, so there is nothing to audit-classify."""

    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        details = _non_master_permanently_failed_details(tmp_path, error_code=None)

    assert "auto_retry_skipped" not in details
    assert details["failure"]["reason_code"] == "UNKNOWN_FAILURE"
    assert details["failure"]["limit_exhausted"] is False
    assert _auto_retry_skipped_warnings(caplog) == []


@pytest.mark.parametrize("error_code", _spec_non_transient_error_codes())
def test_master_permanently_failed_event_carries_auto_retry_skipped(
    tmp_path: Path,
    error_code: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1314: the master production point feeds the payload through the typed transition."""

    repository, record = _terminally_failed_cohort_master(tmp_path, error_code=error_code)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    job = repository.get_pipeline_job(str(record["job_id"]))

    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        handled = service.handle_failed_job(job)

    events = _permanently_failed_events(repository, record)
    assert handled.status == "permanently_failed"
    assert len(events) == 1
    details = events[0]["details"]
    assert details["auto_retry_skipped"] is True
    assert details["reason"] == "non_transient_error"
    assert details["error_code"] == error_code
    assert details["failure"]["retryable"] is False
    assert _auto_retry_skipped_warnings(caplog) == []


def test_master_permanently_failed_event_flags_an_unknown_error_code_and_warns_once(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository, record = _terminally_failed_cohort_master(tmp_path, error_code=_UNKNOWN_ERROR_CODE)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    job = repository.get_pipeline_job(str(record["job_id"]))

    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        handled = service.handle_failed_job(job)

    events = _permanently_failed_events(repository, record)
    assert handled.status == "permanently_failed"
    assert len(events) == 1
    details = events[0]["details"]
    assert details["auto_retry_skipped"] is True
    assert details["reason"] == "unknown_error_code_defaulted_non_transient"
    assert details["error_code"] == _UNKNOWN_ERROR_CODE
    assert _auto_retry_skipped_warnings(caplog) == [_unknown_error_code_warning(_UNKNOWN_ERROR_CODE)]


def test_master_permanently_failed_event_omits_auto_retry_skipped_when_budget_exhausted(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1314 discrimination rule on master geometry (production attempt shape)."""

    from services.orchestrator.retry_identity import effective_retry_attempt

    repository, record = _retry_attempt_failed_cohort_master(
        tmp_path,
        error_code="NODE_FAILURE",
        attempt=2,
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=2, backoff_schedule=[0]))
    persisted_before = repository.get_pipeline_job(str(record["job_id"]))
    job = {
        **persisted_before,
        "retry_count": effective_retry_attempt(
            persisted_before["job_id"],
            persisted_before.get("retry_count"),
        ),
    }

    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        handled = service.handle_failed_job(job)

    events = _permanently_failed_events(repository, record)
    assert handled.status == "permanently_failed"
    assert len(events) == 1
    assert "auto_retry_skipped" not in events[0]["details"]
    assert events[0]["details"]["failure"]["limit_exhausted"] is True
    assert _auto_retry_skipped_warnings(caplog) == []


def test_duplicate_master_mark_leaves_no_orphan_auto_retry_skipped_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1314 decision 3: the warning is append-gated, so an idempotent re-mark is silent."""

    repository, record = _terminally_failed_cohort_master(tmp_path, error_code=_UNKNOWN_ERROR_CODE)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    stale_snapshot = dict(repository.get_pipeline_job(str(record["job_id"])))

    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        first = service.mark_permanently_failed(stale_snapshot)
        warnings_after_first = _auto_retry_skipped_warnings(caplog)
        second = service.mark_permanently_failed(stale_snapshot)

    assert first.status == "permanently_failed"
    assert second.status == "permanently_failed"
    assert len(_permanently_failed_events(repository, record)) == 1
    assert warnings_after_first == [_unknown_error_code_warning(_UNKNOWN_ERROR_CODE)]
    assert _auto_retry_skipped_warnings(caplog) == warnings_after_first


def test_duplicate_non_master_mark_warns_once_per_appended_event(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Non-master twin of the master duplicate-mark test — pinning what the plane does.

    The master branch has a persisted-row idempotency oracle, so a re-mark appends
    nothing and warns nothing.  The non-master branch has no such oracle: its gate
    reads the CALLER's snapshot, so re-driving the same stale ``failed`` snapshot
    appends a second ``permanently_failed`` event (the durable status update itself
    no-ops).  The duplicate event predates #1314; what #1314 owes is that warnings
    track APPENDED events one-for-one, and that is what this pins — a warning count
    that drifts from the event count in either direction is the regression.
    Production callers re-read the row between passes, so the shape is reachable
    only by re-using a snapshot across marks.
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_auto_retry_skipped_twin")
    repository.reserve_pipeline_job(record)
    repository.update_pipeline_job_status(
        "job_auto_retry_skipped_twin",
        "failed",
        error_code=_UNKNOWN_ERROR_CODE,
        error_message="stage failed",
        finished_at=cycle_time,
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    stale_snapshot = dict(repository.get_pipeline_job("job_auto_retry_skipped_twin"))

    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        first = service.mark_permanently_failed(stale_snapshot)
        second = service.mark_permanently_failed(stale_snapshot)

    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    events = [event for event in state["pipeline_events"] if event["event_type"] == "permanently_failed"]
    warnings = _auto_retry_skipped_warnings(caplog)
    assert first.status == "permanently_failed"
    assert second.status == "permanently_failed"
    assert len(events) == 2
    assert all(event["details"]["reason"] == "unknown_error_code_defaulted_non_transient" for event in events)
    assert warnings == [_unknown_error_code_warning(_UNKNOWN_ERROR_CODE)] * len(events)
    # Re-driving after a fresh read is what production does, and it is inert.
    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        service.mark_permanently_failed(repository.get_pipeline_job("job_auto_retry_skipped_twin"))
    assert _auto_retry_skipped_warnings(caplog) == warnings


def test_stale_master_mark_leaves_no_orphan_auto_retry_skipped_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#1314 decision 3: a live master returns ``stale`` without appending — no warning."""

    repository, record = _reserved_cohort_master(tmp_path, member_count=2)
    _bind_cohort_master(repository, record)
    assert repository.transition_pipeline_job_runtime_status(str(record["job_id"]), "running").committed
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    stale_snapshot = {
        **repository.get_pipeline_job(str(record["job_id"])),
        "status": "failed",
        "error_code": _UNKNOWN_ERROR_CODE,
    }

    with caplog.at_level(logging.WARNING, logger=_RETRY_MODULE_LOGGER):
        marked = service.mark_permanently_failed(stale_snapshot)

    assert marked.status == "running"
    assert repository.get_pipeline_job(str(record["job_id"]))["status"] == "running"
    assert _permanently_failed_events(repository, record) == []
    assert _auto_retry_skipped_warnings(caplog) == []


# ---------------------------------------------------------------------------
# #1167: containment-aware existence probes over the journal tree.
# ---------------------------------------------------------------------------


_PROBE_CYCLE = _dt("2026-06-28T00:00:00Z")
_PROBE_CYCLE_SEGMENT = format_cycle_time(_PROBE_CYCLE)
_COHORT_CYCLE = _dt("2026-07-20T00:00:00Z")
_COHORT_CYCLE_SEGMENT = format_cycle_time(_COHORT_CYCLE)


def _symlink_over_directory(path: Path, *, stash: Path) -> None:
    """Swap one real journal directory for a symlink to a real, empty directory.

    The decoy and the displaced original both live outside the journal root so
    the scene isolates the parent-component probe: every path component up to
    the symlink is a real directory, and the symlink's target simply does not
    contain the entry being probed.
    """

    stash.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        path.rename(stash / f"stashed-{path.name}")
    decoy = stash / f"decoy-{path.name}"
    decoy.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(decoy, target_is_directory=True)


def _journal_tree_bytes(root: Path) -> dict[str, bytes]:
    """Durable journal content only; the empty flock files are not records.

    Following symlinks during the walk is load-bearing: the tampered scenes
    replace ``journal/<source>`` with a symlink, and a write that leaked
    through it lands in the decoy.  A non-following walk stops at the symlink
    and reports the leaked bytes as "nothing changed".  Symlinked directories
    are traversed, so the bytes behind them count; a symlinked FILE is dropped
    outright and its target bytes are invisible here — no fixture plants one
    inside ``root`` today, and a zero-write assertion in a scene that does
    would need a different helper.
    Precondition: the tamper fixtures keep their decoys OUTSIDE ``root``, so
    the tree has no symlink cycle; a cycle would not hang the walk — the
    kernel's symlink limit ends the descent and ``os.walk``'s default
    ``onerror`` swallows it — but the snapshot would silently gain duplicated
    keys from the repeated descent; no cycle detection is added here on
    purpose.
    """

    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(
            Path(dirpath) / name
            for dirpath, _dirnames, filenames in os.walk(root, followlinks=True)
            for name in filenames
        )
        if not path.is_symlink() and path.is_file() and ".locks" not in path.parts
    }


def test_probe_fails_loud_when_the_journal_source_parent_is_a_symlink(tmp_path: Path) -> None:
    """E1 — a symlinked parent component is no longer reported as 'absent'.

    ``os.stat(follow_symlinks=False)`` does not follow the FINAL component but
    happily follows a symlinked parent, so the segment probe used to look into
    the decoy, find no cycle file and let the read return ``[]`` — a tampered
    or misconfigured tree downgraded to "this cycle has no records".
    """

    root = tmp_path / "journal"
    _write_jsonl(
        root / "journal" / "gfs" / f"{_PROBE_CYCLE_SEGMENT}.jsonl",
        [_segment_job_record(_PROBE_CYCLE, job_id="job_probe_base", sequence=1)],
    )
    _symlink_over_directory(root / "journal" / "gfs", stash=tmp_path / "outside")

    repository = FileOrchestrationJournalRepository(root)
    with pytest.raises(FileOrchestrationJournalError) as caught:
        repository._cycle_journal_records(source_id="gfs", cycle_time=_PROBE_CYCLE)
    assert caught.value.reason == "file_journal_unreadable"

    # The public read surfaces it the way it surfaces a corrupt journal file:
    # a blocked row carrying the reason, not a silently empty status list.
    blocked = FileOrchestrationJournalRepository(root).list_stage_statuses(
        source_id="gfs",
        cycle_time=_PROBE_CYCLE,
    )
    assert [row["file_journal"]["reason"] for row in blocked] == ["file_journal_unreadable"]


def test_symlinked_journal_source_parent_fails_a_sequence_write_loud(tmp_path: Path) -> None:
    """E2 — the sequence-floor sibling of the same probe idiom, on a write.

    A silently skipped slot would underestimate the floor and let a replay
    sequence be reused, which is unrecoverable state corruption.
    """

    root = tmp_path / "journal"
    _write_jsonl(
        root / "journal" / "gfs" / f"{_PROBE_CYCLE_SEGMENT}.jsonl",
        [_segment_job_record(_PROBE_CYCLE, job_id="job_probe_base", sequence=1)],
    )
    _symlink_over_directory(root / "journal" / "gfs", stash=tmp_path / "outside")
    repository = FileOrchestrationJournalRepository(root)
    before = _journal_tree_bytes(root)

    with pytest.raises(FileOrchestrationJournalError) as caught:
        repository.insert_pipeline_event(
            entity_type="forecast_cycle",
            entity_id=cycle_id_for("gfs", _PROBE_CYCLE),
            event_type="cycle_note",
            status_from=None,
            status_to=None,
        )

    assert caught.value.reason == "file_journal_unreadable"
    assert _journal_tree_bytes(root) == before


def test_symlink_occupying_a_segment_slot_fails_loud_on_read_and_on_the_floor(
    tmp_path: Path,
) -> None:
    """E3 — an end symlink in a probed slot is loud on both consumers.

    The read side already reached the hardened reader with this token, so only
    the origin of the error moves.  The floor probe is the real change: it used
    to ``lstat`` the slot, see "not a regular file" and skip it, so a tampered
    tree silently reset the floor to 1 while a segment carrying sequence 7 sat
    right there.
    """

    root = tmp_path / "journal"
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_jsonl(
        outside / "planted.jsonl",
        [_segment_job_record(_PROBE_CYCLE, job_id="job_planted", sequence=7)],
    )
    (root / "journal" / "gfs").mkdir(parents=True)
    (root / "journal" / "gfs" / f"{_PROBE_CYCLE_SEGMENT}.jsonl").symlink_to(outside / "planted.jsonl")
    repository = FileOrchestrationJournalRepository(root)

    with pytest.raises(FileOrchestrationJournalError) as read_caught:
        repository._cycle_journal_records(source_id="gfs", cycle_time=_PROBE_CYCLE)
    assert read_caught.value.reason == "file_journal_unreadable"

    with pytest.raises(FileOrchestrationJournalError) as floor_caught:
        FileOrchestrationJournalRepository(root)._next_sequence(
            source_id="gfs",
            cycle_time=_PROBE_CYCLE,
        )
    assert floor_caught.value.reason == "file_journal_unreadable"


def test_symlinked_latest_source_parent_fails_the_sequence_write_loud(tmp_path: Path) -> None:
    """E4 — the third copy of the idiom, the ``latest/`` directory probe.

    The latest view holds the highest replay sentinel of the cycle, so a
    symlinked ``latest/<source>`` that made the cycle directory look absent
    dropped the floor from 10 back to 2 and handed the next write a sequence
    the cycle had already used.
    """

    root = tmp_path / "journal"
    _write_jsonl(
        root / "journal" / "gfs" / f"{_PROBE_CYCLE_SEGMENT}.jsonl",
        [_segment_job_record(_PROBE_CYCLE, job_id="job_probe_base", sequence=1)],
    )
    latest_view = _latest_view(cycle_time=_PROBE_CYCLE, jobs=[_active_job(_PROBE_CYCLE)])
    latest_view["replay"]["latest_sequence"] = 9
    _write_json(root / "latest" / "gfs" / _PROBE_CYCLE_SEGMENT / "model_a.json", latest_view)
    assert (
        FileOrchestrationJournalRepository(root)._next_sequence(
            source_id="gfs",
            cycle_time=_PROBE_CYCLE,
        )
        == 10
    )

    _symlink_over_directory(root / "latest" / "gfs", stash=tmp_path / "outside")
    repository = FileOrchestrationJournalRepository(root)
    before = _journal_tree_bytes(root)

    with pytest.raises(FileOrchestrationJournalError) as floor_caught:
        repository._next_sequence(source_id="gfs", cycle_time=_PROBE_CYCLE)
    assert floor_caught.value.reason == "file_journal_unreadable"

    with pytest.raises(FileOrchestrationJournalError) as caught:
        repository.insert_pipeline_event(
            entity_type="forecast_cycle",
            entity_id=cycle_id_for("gfs", _PROBE_CYCLE),
            event_type="cycle_note",
            status_from=None,
            status_to=None,
        )

    assert caught.value.reason == "file_journal_unreadable"
    assert _journal_tree_bytes(root) == before


def test_genuine_absence_under_real_directories_stays_a_legal_empty_read(tmp_path: Path) -> None:
    """E5 — the guard-rail: ``FileNotFoundError`` is absence, never a fault.

    ``stat_no_follow`` raises the same ``FileNotFoundError`` for a missing
    entry and for a missing PARENT component, so mapping either one to loud
    would kill every cold-start read of a brand new source.
    """

    root = tmp_path / "journal"
    (root / "journal" / "gfs").mkdir(parents=True)
    repository = FileOrchestrationJournalRepository(root)
    assert repository._cycle_journal_records(source_id="gfs", cycle_time=_PROBE_CYCLE) == []
    assert repository._next_sequence(source_id="gfs", cycle_time=_PROBE_CYCLE) == 1
    assert repository.list_stage_statuses(source_id="gfs", cycle_time=_PROBE_CYCLE) == []

    cold = FileOrchestrationJournalRepository(tmp_path / "never-initialized")
    assert cold._cycle_journal_records(source_id="gfs", cycle_time=_PROBE_CYCLE) == []
    assert cold._next_sequence(source_id="gfs", cycle_time=_PROBE_CYCLE) == 1


def test_directory_occupying_a_segment_slot_still_reaches_the_hardened_reader(
    tmp_path: Path,
) -> None:
    """E6 — a safe but non-regular occupant stays the reader's call, not the probe's."""

    root = tmp_path / "journal"
    (root / "journal" / "gfs" / f"{_PROBE_CYCLE_SEGMENT}.jsonl").mkdir(parents=True)
    repository = FileOrchestrationJournalRepository(root)

    assert repository._journal_segment_exists(
        root / "journal" / "gfs" / f"{_PROBE_CYCLE_SEGMENT}.jsonl"
    )
    with pytest.raises(FileOrchestrationJournalError) as caught:
        repository._cycle_journal_records(source_id="gfs", cycle_time=_PROBE_CYCLE)
    assert caught.value.reason == "file_journal_unreadable"


def test_sequence_floor_probe_is_containment_aware_at_its_own_seam(tmp_path: Path) -> None:
    """E3 sibling — the floor probe pinned directly, not through its caller.

    ``_cycle_segment_paths`` pre-filters every path this probe receives today,
    so no end-to-end lane can distinguish it from a bare ``os.stat``.  The pin
    lives at the seam so a #1165-family change to that pre-filtering cannot
    silently hand the probe an uncontained path.
    """

    root = tmp_path / "journal"
    _write_jsonl(
        root / "journal" / "gfs" / f"{_PROBE_CYCLE_SEGMENT}.jsonl",
        [_segment_job_record(_PROBE_CYCLE, job_id="job_probe_base", sequence=1)],
    )
    _symlink_over_directory(root / "journal" / "gfs", stash=tmp_path / "outside")
    repository = FileOrchestrationJournalRepository(root)

    with pytest.raises(journal_module._JournalProbeContainmentError):
        repository._sequence_regular_file_exists(root / "journal" / "gfs" / f"{_PROBE_CYCLE_SEGMENT}.jsonl")

    # The other half of the seam's contract: under a chain of real directories
    # a missing entry is still plain absence, never a fault.
    honest = FileOrchestrationJournalRepository(tmp_path / "honest")
    (tmp_path / "honest" / "journal" / "gfs").mkdir(parents=True)
    assert (
        honest._sequence_regular_file_exists(
            tmp_path / "honest" / "journal" / "gfs" / f"{_PROBE_CYCLE_SEGMENT}.jsonl"
        )
        is False
    )


def _reserved_cohort_master_on_disk(base: Path) -> tuple[Any, dict[str, Any]]:
    repository, record = _reserved_cohort_master(base, member_count=2)
    assert repository.reserve_pipeline_job(dict(record)) is not None
    return repository, record


def _corrupt_cohort_cycle_log(repository: Any) -> None:
    (repository.root / "journal" / "gfs" / f"{_COHORT_CYCLE_SEGMENT}.jsonl").write_text(
        "{not json\n",
        encoding="utf-8",
    )


def _d7_upsert(repository: Any, record: Mapping[str, Any]) -> Any:
    # A member row, not the master: its direct file resolves the id without a
    # global scan, so the lane reaches the segment probe.
    return repository.upsert_pipeline_job(
        {
            "job_id": "job_fcst_gfs_2026072000_model_0_forecast_reconciled_17667_0",
            "run_id": "fcst_gfs_2026072000_model_0",
            "source_id": "gfs",
            "cycle_id": "gfs_2026072000",
            "job_type": "run_shud_forecast",
            "model_id": "model_0",
            "stage": "forecast",
            "status": "failed",
            "error_code": "NODE_FAILURE",
            "created_at": _COHORT_CYCLE,
            "updated_at": _COHORT_CYCLE,
        }
    )


def _d7_reject(repository: Any, record: Mapping[str, Any]) -> Any:
    return repository.reject_pipeline_job_submit_attempt(
        str(record["idempotency_key"]),
        pipeline_job_id=str(record["job_id"]),
        expected_submission_attempt=1,
        finished_at=_dt("2026-07-20T01:00:00Z"),
        error_code="SBATCH_REJECTED",
        error_message="scheduler rejected the forecast array",
        stage="forecast",
        job_type="run_shud_forecast_array",
    )


def _d7_mark_permanently_failed(repository: Any, record: Mapping[str, Any]) -> Any:
    return repository.mark_pipeline_job_permanently_failed(str(record["job_id"]))


def _d7_permit_retry(repository: Any, record: Mapping[str, Any]) -> Any:
    return repository.permit_pipeline_job_retry(
        str(record["job_id"]),
        accepted_submit_contract_version=None,
        expected_submission_attempt=1,
    )


def _d7_insert_event(repository: Any, record: Mapping[str, Any]) -> Any:
    return repository.insert_pipeline_event(
        entity_type="forecast_cycle",
        entity_id=cycle_id_for("gfs", _COHORT_CYCLE),
        event_type="cycle_note",
        status_from=None,
        status_to=None,
    )


def _d7_update_status(repository: Any, record: Mapping[str, Any]) -> Any:
    return repository.update_pipeline_job_status(str(record["job_id"]), "failed")


def _d7_project(repository: Any, record: Mapping[str, Any]) -> Any:
    return _project_cohort_failure(repository, record, error_code="NODE_FAILURE")


_D7_WRITE_LANES: dict[str, tuple[Any, Any]] = {
    "upsert_pipeline_job": (_terminally_failed_cohort_master, _d7_upsert),
    "reject_pipeline_job_submit_attempt": (_reserved_cohort_master_on_disk, _d7_reject),
    "mark_pipeline_job_permanently_failed": (_reserved_cohort_master_on_disk, _d7_mark_permanently_failed),
    "permit_pipeline_job_retry": (_reserved_cohort_master_on_disk, _d7_permit_retry),
    "insert_pipeline_event": (_reserved_cohort_master_on_disk, _d7_insert_event),
    "update_pipeline_job_status": (_reserved_cohort_master_on_disk, _d7_update_status),
    "project_forecast_cohort_tasks": (_terminally_failed_cohort_master, _d7_project),
}


@pytest.mark.parametrize("lane", sorted(_D7_WRITE_LANES))
def test_symlinked_parent_fails_every_public_write_lane_in_reader_fault_parity(
    tmp_path: Path,
    lane: str,
) -> None:
    """E7a/E7b/E7d and design D7 — one parity-table row per public write lane.

    The contract is the pair, not either half: a containment fault reaches the
    caller as the SAME exception type a corrupt journal file already reaches it
    with, so no lane gains a new exception type and none goes quiet.  Only the
    reason token and the type are pinned — the message text differs by platform
    (Linux ELOOP vs macOS ENOTDIR) and the originating frame is not contractual.

    Before this change these lanes surfaced, respectively, ``OrchestratorError``
    from the eventual safe_fs write, ``file_journal_authority_transition_requires
    _typed_api`` derived from the empty read, or a silent no-op.
    """

    build, action = _D7_WRITE_LANES[lane]

    tampered_root = tmp_path / "symlinked"
    tampered_root.mkdir()
    tampered, tampered_record = build(tampered_root)
    _symlink_over_directory(tampered.root / "journal" / "gfs", stash=tmp_path / "outside")
    before = _journal_tree_bytes(tampered.root)
    with pytest.raises(FileOrchestrationJournalError) as tampered_caught:
        action(tampered, tampered_record)
    assert tampered_caught.value.reason == "file_journal_unreadable"
    assert _journal_tree_bytes(tampered.root) == before

    corrupt_root = tmp_path / "corrupt"
    corrupt_root.mkdir()
    corrupt, corrupt_record = build(corrupt_root)
    _corrupt_cohort_cycle_log(corrupt)
    with pytest.raises(FileOrchestrationJournalError) as corrupt_caught:
        action(corrupt, corrupt_record)
    assert corrupt_caught.value.reason == "file_journal_malformed_json"


def test_a_write_that_silently_no_opped_under_a_symlinked_parent_now_fails_loud(
    tmp_path: Path,
) -> None:
    """E7c / D7 row 3 — the one success-to-failure flip, and the point of #1167.

    A reserved master is not permanently-failable, so ``stale`` is the honest
    answer on an intact tree.  Under a symlinked parent the journal read came
    back empty and the method returned that same ``stale`` — the caller could
    not distinguish a tampered tree from an honest refusal.
    """

    honest_root = tmp_path / "honest"
    honest_root.mkdir()
    honest, honest_record = _reserved_cohort_master_on_disk(honest_root)
    assert honest.mark_pipeline_job_permanently_failed(str(honest_record["job_id"])).outcome == "stale"

    tampered_root = tmp_path / "tampered"
    tampered_root.mkdir()
    tampered, tampered_record = _reserved_cohort_master_on_disk(tampered_root)
    _symlink_over_directory(tampered.root / "journal" / "gfs", stash=tmp_path / "outside")
    before = _journal_tree_bytes(tampered.root)

    with pytest.raises(FileOrchestrationJournalError) as caught:
        tampered.mark_pipeline_job_permanently_failed(str(tampered_record["job_id"]))

    assert caught.value.reason == "file_journal_unreadable"
    assert _journal_tree_bytes(tampered.root) == before


def test_probe_faults_are_exactly_as_loud_as_reader_faults_on_a_swallow_lane(
    tmp_path: Path,
) -> None:
    """E7e — parity on the lanes that already absorb journal faults.

    The carrier must not inherit the public journal error.  At head every
    probe consumption sits directly inside a choke frame's ``try``, so no
    broad handler on this lane can reach a carrier before conversion; the
    ``issubclass`` line is a forward-looking guard against FUTURE shapes —
    conversion moved outward, or a broad handler introduced between a probe
    and its choke frame — where a subclass would be swallowed into the
    silent-empty failure mode this change exists to remove.  (The
    fixture-review-era measurement of that hazard was taken against the
    pre-choke-frame code shape.)  The observables below pin the other half —
    the fault stays absorbed to the SAME answer a corrupt journal file
    already yields, so no lane gains loudness here.  They are pinned
    absolutely rather than against each other because a common-mode drift
    (both lanes degrading to ``[]``) would satisfy an equality.
    """

    assert not issubclass(journal_module._JournalProbeContainmentError, FileOrchestrationJournalError)

    tampered_root = tmp_path / "symlinked"
    tampered_root.mkdir()
    tampered, _ = _terminally_failed_cohort_master(tampered_root)
    _symlink_over_directory(tampered.root / "journal" / "gfs", stash=tmp_path / "outside")

    corrupt_root = tmp_path / "corrupt"
    corrupt_root.mkdir()
    corrupt, _ = _terminally_failed_cohort_master(corrupt_root)
    _corrupt_cohort_cycle_log(corrupt)

    for repository in (tampered, corrupt):
        assert repository._cycle_materialization_model_ids_unlocked(
            source_id="gfs",
            cycle_time=_COHORT_CYCLE,
        ) == ["model_0", "model_1"]
        assert (
            repository.has_completed_pipeline(
                source_id="gfs",
                cycle_time=_COHORT_CYCLE,
                model_id="model_0",
            )
            is False
        )


# ---------------------------------------------------------------------------
# #1592 / #1589: durable attribution write boundary.
#
# Every assertion below reaches the DURABLE layer — the journal jsonl payload
# and the ``pipeline-jobs/`` direct row file.  ``get_pipeline_job`` re-sanitizes
# on the way out, so a placeholder and a correctly persisted URI are
# indistinguishable there: an assertion on the public row has no discriminating
# power for either issue.
# ---------------------------------------------------------------------------


_REAL_MASTER_LOG_URI = "s3://nhms/logs/cycle_gfs_2026072000/forecast-master.log"
_LAUNDERED_URI = "[object-uri]"


def _durable_master_payload(repository: Any, record: Mapping[str, Any]) -> dict[str, Any]:
    """Newest durable jsonl payload written for the cohort master row."""

    payloads = _durable_pipeline_job_payloads(repository.root, str(record["job_id"]))
    assert payloads, "expected at least one durable pipeline_job payload for the master"
    return payloads[-1]


def _direct_row_payload(repository: Any, job_id: str) -> dict[str, Any]:
    """The ``pipeline-jobs/<job_id>.json`` direct row payload (non-candidate rows)."""

    path = repository.root / "pipeline-jobs" / f"{job_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))["payload"]


def _raw_journal_text(repository: Any) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((repository.root / "journal").rglob("*.jsonl"))
    )


def _bound_cohort_master(tmp_path: Path, *, member_count: int = 2) -> tuple[Any, dict[str, Any]]:
    repository, record = _reserved_cohort_master(tmp_path, member_count=member_count)
    _bind_cohort_master(repository, record)
    return repository, record


def _reproject_cohort(
    repository: Any,
    record: Mapping[str, Any],
    *,
    error_code: str = "OUT_OF_MEMORY",
    outcomes: list[str] | None = None,
    **evidence: Any,
) -> dict[str, int]:
    return repository.project_forecast_cohort_tasks(
        str(record["job_id"]),
        master_slurm_job_id=_PERMANENT_FAILURE_MASTER_SLURM_JOB_ID,
        projections=_cohort_task_projections(record, error_code=error_code, outcomes=outcomes),
        complete=True,
        master_status="failed",
        master_error_code=error_code,
        reconciliation_decision="matched_bound",
        **evidence,
    )


def _mark_master_permanently_failed(
    repository: Any,
    record: Mapping[str, Any],
    *,
    error_code: str = "RETRY_LIMIT_EXHAUSTED",
    error_message: str = "automatic retry declined for this cohort master",
) -> dict[str, Any]:
    result = repository.mark_pipeline_job_permanently_failed(
        str(record["job_id"]),
        error_code=error_code,
        error_message=error_message,
    )
    assert result.outcome == "applied"
    marked = _durable_master_payload(repository, record)
    assert marked["status"] == "permanently_failed"
    return marked


# --- J1 / J2: the record constructor itself ------------------------------


def test_journal_record_for_write_strips_object_uri_placeholders() -> None:
    """#1592 J1: the single record constructor is the anti-laundering boundary.

    Every durable journal record is built here, so a write path that never goes
    through ``_append_validated_record_unlocked`` (the cohort projection loop
    and ``_write_pipeline_job_unlocked``) inherits the guarantee instead of
    having to re-declare it.
    """

    record = journal_module._journal_record_for_write(
        "pipeline_job",
        {
            "job_id": "job_strip_probe",
            "log_uri": "[object-uri]",
            "output_uri": "[uri]",
            "init_state_identities": [{"array_task_id": 0, "init_state_uri": "[object-uri]"}],
        },
        source_id="gfs",
        cycle_time=_dt("2026-07-20T00:00:00Z"),
        model_id=None,
        sequence=1,
    )

    payload = record["payload"]
    assert payload["log_uri"] is None
    assert payload["output_uri"] is None
    assert payload["init_state_identities"] == [{"array_task_id": 0, "init_state_uri": None}]


def test_journal_record_for_write_keeps_deliberately_persisted_placeholders() -> None:
    """#1592 J2 (must-preserve 4): the strip stays narrow.

    ``[local-path]`` and ``[redacted]`` are deliberately persisted evidence for
    runtime-root and secret redaction; only the object-URI placeholder set is
    laundering.  A whole-value match also has to leave placeholder text that is
    merely embedded in a longer string alone.
    """

    record = journal_module._journal_record_for_write(
        "pipeline_job",
        {
            "job_id": "job_keep_probe",
            "workspace_path": "[local-path]",
            "runtime_root": "[local-path]",
            "authorization": "[redacted]",
            "message": "accounting read [object-uri] and [local-path]",
        },
        source_id="gfs",
        cycle_time=_dt("2026-07-20T00:00:00Z"),
        model_id=None,
        sequence=1,
    )

    payload = record["payload"]
    assert payload["workspace_path"] == "[local-path]"
    assert payload["runtime_root"] == "[local-path]"
    assert payload["authorization"] == "[redacted]"
    assert payload["message"] == "accounting read [object-uri] and [local-path]"


# --- J3 / J4: the two durable bypasses, end to end -----------------------


def test_cohort_projection_cannot_launder_a_placeholder_into_durable_state(tmp_path: Path) -> None:
    """#1592 J3: bypass A (``project_forecast_cohort_tasks`` payload loop).

    The loop calls ``_journal_record_for_write`` directly, so before this change
    a caller that round-tripped a public row laundered ``[object-uri]`` straight
    into the durable master row and its direct row file.
    """

    repository, record = _bound_cohort_master(tmp_path)

    _reproject_cohort(repository, record, log_uri=_LAUNDERED_URI)

    durable = _durable_master_payload(repository, record)
    direct = _direct_row_payload(repository, str(record["job_id"]))
    assert durable["log_uri"] is None
    assert direct["log_uri"] is None
    assert _LAUNDERED_URI not in _raw_journal_text(repository)


def test_deferred_cohort_projection_cannot_launder_a_placeholder(tmp_path: Path) -> None:
    """#1592 J4: bypass B (defer leg → ``_write_pipeline_job_unlocked``).

    Negative-discriminating on its own — ``None`` also satisfies "no literal
    persisted", which is exactly the outcome design D3 forbids when the row
    already held a real value.  Its displacement mirror below (J4b) is what
    actually pins the defer leg.
    """

    repository, record = _bound_cohort_master(tmp_path)

    result = repository.defer_forecast_cohort_projection(
        str(record["job_id"]),
        reconciliation_decision="accounting_unavailable",
        reconciliation_reason_class="coverage_incomplete",
        error_code="SLURM_TASK_ACCOUNTING_INCOMPLETE",
        error_message="terminal Slurm array task accounting was incomplete",
        log_uri=_LAUNDERED_URI,
    )

    assert result.outcome == "applied"
    durable = _durable_master_payload(repository, record)
    direct = _direct_row_payload(repository, str(record["job_id"]))
    assert durable["log_uri"] is None
    assert direct["log_uri"] is None
    assert _LAUNDERED_URI not in _raw_journal_text(repository)


def test_deferred_projection_placeholder_does_not_displace_a_real_log_uri(tmp_path: Path) -> None:
    """#1589 J4b (design D3, defer leg): a withheld value is not an overwrite.

    Pass 1 parks the row on ``reconcile_unverified`` with a real log URI; that
    status is NOT terminal, so the defer leg's whole-row short circuit does not
    stop pass 2.  Pass 2 carries the display placeholder — the raw
    ``is not None`` predicate treated it as a supplied value, displacing the
    real URI, which the write-boundary strip then turned into ``None``.  Net
    loss of durable evidence, i.e. worse than laundering.
    """

    repository, record = _bound_cohort_master(tmp_path)
    first = repository.defer_forecast_cohort_projection(
        str(record["job_id"]),
        reconciliation_decision="accounting_unavailable",
        reconciliation_reason_class="coverage_incomplete",
        error_code="SLURM_TASK_ACCOUNTING_INCOMPLETE",
        error_message="terminal Slurm array task accounting was incomplete",
        log_uri=_REAL_MASTER_LOG_URI,
    )
    assert first.outcome == "applied"
    assert _durable_master_payload(repository, record)["status"] == "reconcile_unverified"
    assert _durable_master_payload(repository, record)["log_uri"] == _REAL_MASTER_LOG_URI

    repository.defer_forecast_cohort_projection(
        str(record["job_id"]),
        reconciliation_decision="accounting_unavailable",
        reconciliation_reason_class="coverage_incomplete",
        error_code="SLURM_MASTER_IDENTITY_MISMATCH",
        error_message="a later deferred pass re-observed the same master",
        log_uri=_LAUNDERED_URI,
    )

    assert _durable_master_payload(repository, record)["log_uri"] == _REAL_MASTER_LOG_URI
    assert _direct_row_payload(repository, str(record["job_id"]))["log_uri"] == _REAL_MASTER_LOG_URI


# --- J5: the projection leg's displacement oracle (core of this change) --


def test_projection_placeholder_does_not_displace_a_real_log_uri(tmp_path: Path) -> None:
    """#1589 J5 (design D3, projection leg): the interlock case.

    The durable master already holds a real log URI.  A later reprojection
    round-trips a public row, so ``log_uri`` arrives as ``[object-uri]``.  With
    the raw predicate that placeholder displaces the real URI and the new write
    boundary then withholds it — the real URI is lost outright, which is
    strictly worse than the literal this change set out to remove.
    """

    repository, record = _terminally_failed_cohort_master(tmp_path)
    _reproject_cohort(repository, record, log_uri=_REAL_MASTER_LOG_URI)
    assert _durable_master_payload(repository, record)["log_uri"] == _REAL_MASTER_LOG_URI
    _mark_master_permanently_failed(repository, record)

    _reproject_cohort(
        repository,
        record,
        error_code="NODE_FAILURE",
        finished_at=_dt("2026-07-21T03:00:00Z"),
        exit_code=137,
        log_uri=_LAUNDERED_URI,
    )

    assert _durable_master_payload(repository, record)["log_uri"] == _REAL_MASTER_LOG_URI
    assert _direct_row_payload(repository, str(record["job_id"]))["log_uri"] == _REAL_MASTER_LOG_URI


# --- J6 / J7: attribution sticks, observation keeps refreshing -----------


def test_permanently_failed_master_keeps_its_error_code_across_reprojection(tmp_path: Path) -> None:
    """#1589 J6a: the attribution family sticks with the mark.

    Reachability (design "D4 / #1589 可达性更正"): this is NOT reached on just
    any resume/reconcile pass — the resume leg is gated by
    ``settled_cohort_master`` and the reconcile leg by
    ``_job_needs_restart_reconcile``.  The one production geometry that reaches
    the sticky line is "durable projection incomplete + this pass carries
    ``complete=True``", which is what this test constructs.
    """

    repository, record = _terminally_failed_cohort_master(tmp_path)
    marked = _mark_master_permanently_failed(repository, record)

    _reproject_cohort(
        repository,
        record,
        error_code="NODE_FAILURE",
        master_error_message="a later accounting pass derived a different cause",
        finished_at=_dt("2026-07-21T03:00:00Z"),
    )

    durable = _durable_master_payload(repository, record)
    assert durable["status"] == "permanently_failed"
    assert durable["error_code"] == marked["error_code"] == "RETRY_LIMIT_EXHAUSTED"
    assert _direct_row_payload(repository, str(record["job_id"]))["error_code"] == "RETRY_LIMIT_EXHAUSTED"


def test_permanently_failed_master_keeps_its_error_message_across_reprojection(tmp_path: Path) -> None:
    """#1589 J6b: ``error_message`` is the same attribution family as the code.

    Kept in its own test so a fix that sticks only ``error_code`` — the literal
    scope of the issue title — is separately falsifiable.  Same reachability
    limitation as J6a.
    """

    repository, record = _terminally_failed_cohort_master(tmp_path)
    marked = _mark_master_permanently_failed(repository, record)
    assert marked["error_message"] == "automatic retry declined for this cohort master"

    _reproject_cohort(
        repository,
        record,
        error_code="NODE_FAILURE",
        master_error_message="a later accounting pass derived a different cause",
        finished_at=_dt("2026-07-21T03:00:00Z"),
    )

    durable = _durable_master_payload(repository, record)
    assert durable["error_message"] == "automatic retry declined for this cohort master"
    assert (
        _direct_row_payload(repository, str(record["job_id"]))["error_message"]
        == "automatic retry declined for this cohort master"
    )


def test_permanently_failed_master_still_refreshes_observational_evidence(tmp_path: Path) -> None:
    """#1589 J7 (must-preserve 3): the reverse nail on design D4.

    ``finished_at`` / ``exit_code`` / ``log_uri`` are facts about the master
    Slurm job, not claims about why it was condemned.  Refreshing them under a
    sticky status is the projection's stated intent; freezing them would be
    design D4 mis-implemented as the rejected whole-row short circuit.
    """

    repository, record = _terminally_failed_cohort_master(tmp_path)
    marked = _mark_master_permanently_failed(repository, record)
    finished_at = _dt("2026-07-21T03:00:00Z")

    _reproject_cohort(
        repository,
        record,
        error_code="NODE_FAILURE",
        finished_at=finished_at,
        exit_code=137,
        log_uri=_REAL_MASTER_LOG_URI,
    )

    durable = _durable_master_payload(repository, record)
    assert durable["finished_at"] == journal_module._format_utc(finished_at)
    assert durable["finished_at"] != marked["finished_at"]
    assert durable["exit_code"] == 137
    assert durable["log_uri"] == _REAL_MASTER_LOG_URI


# --- J8: stickiness does not spread to the derived terminal statuses -----


@pytest.mark.parametrize("status", ["succeeded", "partially_failed", "failed"])
def test_derived_terminal_masters_still_take_the_reprojected_error_code(
    tmp_path: Path,
    status: str,
) -> None:
    """#1589 J8 (design D5): the reverse nail on the trigger condition.

    ``succeeded`` / ``partially_failed`` / ``failed`` are exactly the values the
    projection derives for itself.  Making them sticky would pin the first
    pass's conclusion forever, i.e. disable the projection rather than protect
    it.  All three arms are parametrized so widening the trigger to
    ``in TERMINAL_PIPELINE_STATUSES`` turns the whole set red.

    This does NOT catch a narrow widening to ``{permanently_failed, cancelled}``
    — there is no ``cancelled`` arm and by design D5 we do not want one
    (``cancelled`` has no status stickiness at all today; that pre-existing gap
    is tracked separately).
    """

    repository, record = _cohort_master_in_status(tmp_path, status)

    _reproject_cohort(repository, record, error_code="NODE_FAILURE")

    durable = _durable_master_payload(repository, record)
    assert durable["error_code"] == "NODE_FAILURE"
    assert _direct_row_payload(repository, str(record["job_id"]))["error_code"] == "NODE_FAILURE"


# --- J9: stickiness must not manufacture an empty write ------------------


def test_stickiness_suppressing_the_only_change_writes_no_record(tmp_path: Path) -> None:
    """#1589 J9 (must-preserve 7): ``cohort_changed`` turns False on its own.

    The geometry is UNIT-CONSTRUCTED and not production reachable: it requires
    ``candidate_projections`` to be unchanged, which means the durable
    projection is already complete — and such a row is never scanned into this
    path in the first place.  It exists to prove the change-detection list was
    left alone rather than special-cased for stickiness.
    """

    repository, record = _terminally_failed_cohort_master(tmp_path)
    _mark_master_permanently_failed(repository, record)
    payloads_before = len(_durable_pipeline_job_payloads(repository.root, str(record["job_id"])))
    events_before = _master_events(repository, record)

    counts = _reproject_cohort(repository, record, error_code="NODE_FAILURE")

    assert counts == {"total": 0, "pipeline_status": 0, "pipeline_event": 0}
    assert len(_durable_pipeline_job_payloads(repository.root, str(record["job_id"]))) == payloads_before
    assert _master_events(repository, record) == events_before


# --- J10: the manual-retry round-trip residue ----------------------------


def test_manual_retry_round_trip_keeps_the_real_log_uri(
    tmp_path: Path,
) -> None:
    """#1592 J10 (design D8, restated round 2): the real URI SURVIVES.

    ``_record_manual_retry_submission_success`` reads a PUBLIC row and writes it
    straight back, so ``log_uri`` arrives as ``[object-uri]``.  D8 originally
    claimed the durable residue as ``None`` -- "the only honest never
    published".  Round 2 supersedes that: a placeholder is a WITHHELD value, and
    the honest resolution of a withheld field is the value the row already
    carries, not an erasure.  ``upsert_pipeline_job`` resolves it against the
    persisted row, so the real ``s3://`` URI stays durable.

    The geometry is UNIT-CONSTRUCTED, not flow reachable: the full
    ``attempt_manual_retry`` flow mints its pending row with an explicit
    ``log_uri=None`` (``_create_pending_manual_retry_job``), so it cannot reach
    this laundering step.  The round-trip helper is therefore called directly,
    and the reachability claim is kept at "unit-constructed" rather than
    promoted to "production reachable".
    """

    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_manual_retry_round_trip")
    repository.reserve_pipeline_job(record)
    repository.bind_pipeline_job_reservation(record["idempotency_key"], slurm_job_id="4242")
    repository.update_pipeline_job_status(
        record["job_id"],
        "failed",
        error_code="NODE_FAILURE",
        error_message="node failed",
        finished_at=cycle_time,
        log_uri=_REAL_MASTER_LOG_URI,
    )
    assert _direct_row_payload(repository, record["job_id"])["log_uri"] == _REAL_MASTER_LOG_URI
    assert repository.get_pipeline_job(record["job_id"])["log_uri"] == _LAUNDERED_URI
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    trusted_snapshot = repository._pipeline_job_for_id_unlocked(record["job_id"])
    assert trusted_snapshot is not None
    service._record_manual_retry_submission_success(
        record["job_id"],
        {"job_id": "7007", "status": "submitted"},
        trusted_snapshot,
    )

    durable = _durable_pipeline_job_payloads(repository.root, record["job_id"])[-1]
    assert durable["status"] == "submitted"
    assert durable["log_uri"] == _REAL_MASTER_LOG_URI
    assert _direct_row_payload(repository, record["job_id"])["log_uri"] == _REAL_MASTER_LOG_URI
    assert _LAUNDERED_URI not in _raw_journal_text(repository)


# --- J13-J20: every other write leg that admits caller evidence ----------
#
# Round-2 fix pass.  Putting the anti-laundering strip at the record
# constructor made durable state stripped while caller input stayed raw, so any
# leg that compares "what the caller asked for" against "what is on the row"
# lost the ability to converge: its overwrite guard and its equality gate both
# see a placeholder that the write boundary then erases.  The projection and
# defer legs were fixed in round 1; these pin the rest of the class.


def _reserved_unbound_master(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    """One versioned master still reserved and unbound (submit-evidence CAS)."""

    repository, record = _reserved_cohort_master(tmp_path, member_count=2)
    assert repository.reserve_pipeline_job(dict(record)) is not None
    return repository, record


def _defer_submit_evidence(repository: Any, record: Mapping[str, Any], *, log_uri: str | None) -> Any:
    """One generic versioned submit-evidence transition that leaves status alone.

    ``status=None`` keeps the row ``reserved`` and unbound, so the identical
    replay still passes the CAS gates and actually reaches the equality gate --
    which is the thing under test.
    """

    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )

    return repository.transition_pipeline_job_submit_evidence(
        str(record["job_id"]),
        AcceptedSubmitTransition.accounting(
            "absence_deferred",
            submit_outcome="submit_result_ambiguous",
        ),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
        log_uri=log_uri,
    )


def _defer_cohort_projection(repository: Any, record: Mapping[str, Any], *, log_uri: str | None) -> Any:
    return repository.defer_forecast_cohort_projection(
        str(record["job_id"]),
        reconciliation_decision="accounting_unavailable",
        reconciliation_reason_class="coverage_incomplete",
        error_code="SLURM_TASK_ACCOUNTING_INCOMPLETE",
        error_message="terminal Slurm array task accounting was incomplete",
        log_uri=log_uri,
    )


def _running_master_with_real_log_uri(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    """One bound master advanced to ``running`` carrying a real durable log URI."""

    repository, record = _bound_cohort_master(tmp_path)
    advanced = repository.transition_pipeline_job_runtime_status(
        str(record["job_id"]),
        "running",
        log_uri=_REAL_MASTER_LOG_URI,
    )
    assert advanced.outcome == "applied"
    assert _durable_master_payload(repository, record)["log_uri"] == _REAL_MASTER_LOG_URI
    return repository, record


def _durable_log_uri(repository: Any, job_id: str) -> Any:
    return _durable_pipeline_job_payloads(repository.root, job_id)[-1]["log_uri"]


def _durable_write_count(repository: Any, job_id: str) -> int:
    return len(_durable_pipeline_job_payloads(repository.root, job_id))


def test_runtime_status_placeholder_replay_stops_appending_records(tmp_path: Path) -> None:
    """#1589 J13: an identical replay carrying a placeholder must settle.

    The guard asks "did the caller supply a value?"; a placeholder answers yes
    and the write boundary then erases it, so every pass sees a row that differs
    from durable state and appends another record.  ``running -> running`` is a
    legal self transition, so nothing else stops the loop: this is unbounded
    append growth walking towards the journal's segment/byte ceilings.
    """

    repository, record = _bound_cohort_master(tmp_path)
    job_id = str(record["job_id"])
    before = _durable_write_count(repository, job_id)

    outcomes = [
        repository.transition_pipeline_job_runtime_status(
            job_id,
            "running",
            log_uri=_LAUNDERED_URI,
        ).outcome
        for _ in range(3)
    ]

    assert outcomes == ["applied", "idempotent", "idempotent"]
    assert _durable_write_count(repository, job_id) == before + 1
    assert _durable_log_uri(repository, job_id) is None


def test_runtime_status_placeholder_does_not_displace_a_real_log_uri(tmp_path: Path) -> None:
    """#1589 J14: withheld means keep, on the runtime-status leg."""

    repository, record = _running_master_with_real_log_uri(tmp_path)
    job_id = str(record["job_id"])
    before = _durable_write_count(repository, job_id)

    replay = repository.transition_pipeline_job_runtime_status(
        job_id,
        "running",
        log_uri=_LAUNDERED_URI,
    )

    assert replay.outcome == "idempotent"
    assert _durable_write_count(repository, job_id) == before
    assert _durable_log_uri(repository, job_id) == _REAL_MASTER_LOG_URI


def test_submit_evidence_placeholder_replay_stops_appending_records(tmp_path: Path) -> None:
    """#1589 J15: the submit-evidence equality gate must be able to converge."""

    repository, record = _reserved_unbound_master(tmp_path)
    job_id = str(record["job_id"])
    before = _durable_write_count(repository, job_id)

    outcomes = [_defer_submit_evidence(repository, record, log_uri=_LAUNDERED_URI).outcome for _ in range(3)]

    assert outcomes == ["applied", "idempotent", "idempotent"]
    assert _durable_write_count(repository, job_id) == before + 1
    assert _durable_log_uri(repository, job_id) is None


def test_submit_evidence_placeholder_does_not_displace_a_real_log_uri(tmp_path: Path) -> None:
    """#1589 J16: withheld means keep, on the submit-evidence leg."""

    repository, record = _reserved_unbound_master(tmp_path)
    job_id = str(record["job_id"])
    assert _defer_submit_evidence(repository, record, log_uri=_REAL_MASTER_LOG_URI).outcome == "applied"
    assert _durable_log_uri(repository, job_id) == _REAL_MASTER_LOG_URI
    before = _durable_write_count(repository, job_id)

    replay = _defer_submit_evidence(repository, record, log_uri=_LAUNDERED_URI)

    assert replay.outcome == "idempotent"
    assert _durable_write_count(repository, job_id) == before
    assert _durable_log_uri(repository, job_id) == _REAL_MASTER_LOG_URI


def _cancellation_pending_master(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    repository, record = _running_master_with_real_log_uri(tmp_path)
    intent = repository.request_pipeline_job_cancellation(
        str(record["job_id"]),
        expected_statuses=("running",),
        reason="operator_requested",
    )
    assert intent.outcome == "applied"
    return repository, record


def _complete_cancellation(repository: Any, record: Mapping[str, Any], *, log_uri: str | None) -> Any:
    return repository.complete_pipeline_job_cancellation(
        str(record["job_id"]),
        finished_at=_dt("2026-07-20T02:00:00Z"),
        exit_code=0,
        error_code=None,
        error_message=None,
        log_uri=log_uri,
    )


def test_cancellation_completion_placeholder_replay_is_idempotent(tmp_path: Path) -> None:
    """#1589 J17: the Gateway cancellation receipt must still land twice-safely.

    This leg writes ``log_uri`` UNCONDITIONALLY, so a stripped placeholder is
    not merely "declined" -- it destroys the value.  The replay comparator at
    the ``reconcile_unverified`` arm then compares the raw placeholder against
    erased durable state and answers ``stale``, which makes
    ``AcceptedSubmitCommitResult.committed`` False and makes
    ``chain_forecast_control.cancel_active_cycle_jobs`` ``continue`` -- dropping
    the cancellation event and the ``cancelled`` entry it owes its caller.
    """

    repository, record = _cancellation_pending_master(tmp_path)
    job_id = str(record["job_id"])

    first = _complete_cancellation(repository, record, log_uri=_LAUNDERED_URI)
    after_first = _durable_write_count(repository, job_id)
    second = _complete_cancellation(repository, record, log_uri=_LAUNDERED_URI)

    assert (first.outcome, second.outcome) == ("applied", "idempotent")
    assert _durable_write_count(repository, job_id) == after_first


def test_cancellation_completion_placeholder_does_not_displace_a_real_log_uri(
    tmp_path: Path,
) -> None:
    """#1589 J18: withheld means keep, on the unconditional cancellation write.

    A strip alone is not enough here: the write has no ``is not None`` guard to
    decline, so the placeholder has to resolve to the durable value instead.
    """

    repository, record = _cancellation_pending_master(tmp_path)
    job_id = str(record["job_id"])

    assert _complete_cancellation(repository, record, log_uri=_LAUNDERED_URI).outcome == "applied"

    assert _durable_log_uri(repository, job_id) == _REAL_MASTER_LOG_URI


def _plain_job_with_real_log_uri(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    """One non-master pipeline job carrying a real durable log URI."""

    cycle_time = _dt("2026-06-28T00:00:00Z")
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(cycle_time, job_id="job_plain_log_uri_probe")
    repository.reserve_pipeline_job(record)
    repository.update_pipeline_job_status(
        str(record["job_id"]),
        "running",
        log_uri=_REAL_MASTER_LOG_URI,
    )
    assert _durable_log_uri(repository, str(record["job_id"])) == _REAL_MASTER_LOG_URI
    return repository, record


def test_update_pipeline_job_status_placeholder_does_not_displace_a_real_log_uri(
    tmp_path: Path,
) -> None:
    """#1589 J19: withheld means keep, on the untyped compatibility leg.

    This leg has no equality gate and appends on every call -- base behaviour
    that is deliberately left alone.  What must hold is that the VALUE
    converges: two placeholder passes leave the real URI durable rather than
    erasing it on the first one.
    """

    repository, record = _plain_job_with_real_log_uri(tmp_path)
    job_id = str(record["job_id"])

    repository.update_pipeline_job_status(job_id, "running", log_uri=_LAUNDERED_URI)
    after_first = _durable_log_uri(repository, job_id)
    repository.update_pipeline_job_status(job_id, "running", log_uri=_LAUNDERED_URI)

    assert after_first == _REAL_MASTER_LOG_URI
    assert _durable_log_uri(repository, job_id) == _REAL_MASTER_LOG_URI


_DEFER_REAL_ERROR_CODE = "SLURM_TASK_ACCOUNTING_INCOMPLETE"
_DEFER_REAL_ERROR_MESSAGE = "terminal Slurm array task accounting was incomplete"
_DEFER_URI_VALUED_ERROR = "s3://nhms/logs/cycle_gfs_2026072000/forecast-master-failure.log"
_DEFER_ERROR_FAMILY = ("error_code", "error_message")


def _defer_cohort_projection_errors(
    repository: Any,
    record: Mapping[str, Any],
    *,
    error_code: str = _DEFER_REAL_ERROR_CODE,
    error_message: str = _DEFER_REAL_ERROR_MESSAGE,
) -> Any:
    """Drive the defer leg with its error family under the caller's control.

    ``_defer_cohort_projection`` hardcodes both error fields because it exists to
    vary ``log_uri``; this one varies the other half of the evidence family.
    """

    return repository.defer_forecast_cohort_projection(
        str(record["job_id"]),
        reconciliation_decision="accounting_unavailable",
        reconciliation_reason_class="coverage_incomplete",
        error_code=error_code,
        error_message=error_message,
    )


@pytest.mark.parametrize("field", _DEFER_ERROR_FAMILY)
def test_defer_projection_placeholder_error_replay_stops_appending_records(
    tmp_path: Path,
    field: str,
) -> None:
    """#1589 J21: the defer leg's error family must converge on a replay too.

    ``log_uri``/``finished_at``/``exit_code`` were resolved on this leg already;
    ``error_code``/``error_message`` were not, and they are written
    UNCONDITIONALLY and then compared by the same ``changed_fields`` gate.  A raw
    placeholder therefore differs from stripped durable state on every pass, so
    every pass answers ``applied`` and appends another record -- unbounded growth
    towards the journal's segment/byte ceilings, on a row parked at
    ``reconcile_unverified``, which is not terminal and so is not stopped by the
    whole-row short circuit above the gate.
    """

    repository, record = _bound_cohort_master(tmp_path)
    job_id = str(record["job_id"])
    before = _durable_write_count(repository, job_id)

    outcomes = [
        _defer_cohort_projection_errors(repository, record, **{field: _LAUNDERED_URI}).outcome
        for _ in range(3)
    ]

    assert outcomes == ["applied", "idempotent", "idempotent"]
    assert _durable_write_count(repository, job_id) == before + 1
    assert _durable_master_payload(repository, record)[field] is None


@pytest.mark.parametrize("field", _DEFER_ERROR_FAMILY)
def test_defer_projection_placeholder_does_not_displace_a_real_error_family(
    tmp_path: Path,
    field: str,
) -> None:
    """#1589 J22: withheld means keep, on the defer leg's unconditional pair.

    Seeded with a URI-valued error field, because that is exactly the shape the
    public projection renders as a bare ``[object-uri]`` -- pinned below by
    reading the field back through ``get_pipeline_job`` rather than asserted in
    a comment.  A caller round-tripping that public row hands the placeholder
    straight back, and this leg has no ``is not None`` guard to decline with, so
    only resolving against the persisted row keeps the value alive.
    """

    repository, record = _bound_cohort_master(tmp_path)
    job_id = str(record["job_id"])
    assert _defer_cohort_projection_errors(repository, record, **{field: _DEFER_URI_VALUED_ERROR}).outcome == (
        "applied"
    )
    assert _durable_master_payload(repository, record)[field] == _DEFER_URI_VALUED_ERROR
    assert repository.get_pipeline_job(job_id)[field] == _LAUNDERED_URI
    before = _durable_write_count(repository, job_id)

    replay = _defer_cohort_projection_errors(repository, record, **{field: _LAUNDERED_URI})

    assert replay.outcome == "idempotent"
    assert _durable_write_count(repository, job_id) == before
    assert _durable_master_payload(repository, record)[field] == _DEFER_URI_VALUED_ERROR


def _project_cohort(repository: Any, record: Mapping[str, Any], *, log_uri: str | None) -> Any:
    return _reproject_cohort(repository, record, log_uri=log_uri)


def _reserved_master_with_real_log_uri(tmp_path: Path) -> tuple[Any, dict[str, Any]]:
    """One still-reserved, still-unbound master already carrying a real log URI.

    ``reserve_pipeline_job`` nulls the whole evidence family, so a *fresh*
    reservation cannot be displaced.  It is not the only producer of a
    ``reserved`` row that ``commit_pipeline_job_submit_attempt`` will bind:
    an unbound submit-evidence transition writes ``log_uri`` onto a row that
    stays ``reserved``, and ``reclaim_pipeline_job_reservation`` nulls
    ``exit_code`` / ``error_code`` / ``error_message`` but NOT ``log_uri``, so a
    reclaimed reservation carries the previous attempt's URI forward.  This
    seeds the first of those two shapes -- the state the commit leg's
    ``_resolved_caller_evidence`` actually has to defend.
    """

    repository, record = _reserved_unbound_master(tmp_path)
    _defer_submit_evidence(repository, record, log_uri=_REAL_MASTER_LOG_URI)
    assert _durable_log_uri(repository, str(record["job_id"])) == _REAL_MASTER_LOG_URI
    return repository, record


def _commit_submit_attempt(repository: Any, record: Mapping[str, Any], *, log_uri: str | None) -> Any:
    """Bind the reserved master, asserting the write leg was actually reached.

    The shared convergence assertions only read durable state, so a call that
    declined at a CAS gate would look identical to a call that wrote and kept
    the real URI.  Pinning the outcome here keeps this arm from passing
    vacuously: the first pass must apply, the replay must settle as idempotent.
    """

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

    result = repository.commit_pipeline_job_submit_attempt(
        str(record["idempotency_key"]),
        pipeline_job_id=str(record["job_id"]),
        expected_submission_attempt=int(record.get("submission_attempt") or 1),
        slurm_job_id=_COMMIT_ATTEMPT_SLURM_JOB_ID,
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
        log_uri=log_uri,
    )
    assert result.outcome in ("applied", "idempotent"), result.outcome
    return result


_COMMIT_ATTEMPT_SLURM_JOB_ID = "17667"


_LOG_URI_WRITE_LEGS: tuple[tuple[str, Any, Any, bool], ...] = (
    (
        "transition_pipeline_job_submit_evidence",
        lambda tmp_path: _seeded(_reserved_unbound_master(tmp_path), _defer_submit_evidence),
        _defer_submit_evidence,
        False,
    ),
    (
        "transition_pipeline_job_runtime_status",
        _running_master_with_real_log_uri,
        lambda repository, record, *, log_uri: repository.transition_pipeline_job_runtime_status(
            str(record["job_id"]), "running", log_uri=log_uri
        ),
        False,
    ),
    (
        "complete_pipeline_job_cancellation",
        _cancellation_pending_master,
        _complete_cancellation,
        False,
    ),
    (
        "update_pipeline_job_status",
        _plain_job_with_real_log_uri,
        lambda repository, record, *, log_uri: repository.update_pipeline_job_status(
            str(record["job_id"]), "running", log_uri=log_uri
        ),
        True,
    ),
    (
        "defer_forecast_cohort_projection",
        lambda tmp_path: _seeded(_bound_cohort_master(tmp_path), _defer_cohort_projection),
        _defer_cohort_projection,
        False,
    ),
    (
        "project_forecast_cohort_tasks",
        lambda tmp_path: _seeded(_bound_cohort_master(tmp_path), _project_cohort),
        _project_cohort,
        False,
    ),
    (
        "commit_pipeline_job_submit_attempt",
        _reserved_master_with_real_log_uri,
        _commit_submit_attempt,
        False,
    ),
)


_LOG_URI_WRITE_LEG_EXCLUSIONS: dict[str, str] = {}
"""Public ``log_uri``-taking methods deliberately outside the replay table.

Empty by intent: every method the scan finds is currently held to the
convergence contract.  A new leg that genuinely cannot be replayed goes here
with its reason as the value, which is a reviewable decision rather than a
silent omission from the table.
"""


def _seeded(
    prepared: tuple[Any, dict[str, Any]],
    seed: Any,
) -> tuple[Any, dict[str, Any]]:
    """Drive one leg once with a REAL log URI so a later replay has prey."""

    repository, record = prepared
    seed(repository, record, log_uri=_REAL_MASTER_LOG_URI)
    assert _durable_log_uri(repository, str(record["job_id"])) == _REAL_MASTER_LOG_URI
    return repository, record


@pytest.mark.parametrize(
    ("leg", "prepare", "write", "appends_on_replay"),
    _LOG_URI_WRITE_LEGS,
    ids=[leg for leg, _prepare, _write, _appends in _LOG_URI_WRITE_LEGS],
)
def test_log_uri_write_legs_converge_on_a_replayed_placeholder(
    tmp_path: Path,
    leg: str,
    prepare: Any,
    write: Any,
    appends_on_replay: bool,
) -> None:
    """#1589 J20: the same contract on every leg in the table.

    Every durable write leg that accepts a caller ``log_uri`` gets the same
    contract: replaying the same display placeholder converges -- the durable
    value stops moving and, where the leg has an equality gate, the replay stops
    appending records.  ``update_pipeline_job_status`` is the one leg with no
    equality gate (it appends on every call by design, unchanged here), so it
    carries ``appends_on_replay`` and is held only to value convergence.

    This test alone proves the contract for the legs listed in
    ``_LOG_URI_WRITE_LEGS`` and nothing more.  What lifts it to a class property
    is ``test_log_uri_write_leg_table_covers_every_log_uri_taking_method``,
    which pins that list against the repository's actual public surface, so a
    leg added later cannot stay silently untested.
    """

    repository, record = prepare(tmp_path)
    job_id = str(record["job_id"])

    write(repository, record, log_uri=_LAUNDERED_URI)
    after_first = _durable_log_uri(repository, job_id)
    count_after_first = _durable_write_count(repository, job_id)
    write(repository, record, log_uri=_LAUNDERED_URI)

    assert after_first == _REAL_MASTER_LOG_URI, leg
    assert _durable_log_uri(repository, job_id) == _REAL_MASTER_LOG_URI, leg
    if appends_on_replay:
        assert _durable_write_count(repository, job_id) == count_after_first + 1, leg
    else:
        assert _durable_write_count(repository, job_id) == count_after_first, leg


def test_log_uri_write_leg_table_covers_every_log_uri_taking_method() -> None:
    """#1589 J20 completeness: bind the leg table to the real public surface.

    ``_LOG_URI_WRITE_LEGS`` is hand written, so on its own it is N instances of
    the convergence contract, not the contract.  This introspects every public
    method of ``FileOrchestrationJournalRepository`` for a ``log_uri``
    parameter and requires that set to equal the table plus the named
    exclusions -- so a new caller-``log_uri`` write leg turns this RED until
    someone either parametrizes it or records why it is excluded.  The two
    directions are asserted separately so the failure says which happened.

    Field boundary, stated rather than implied: this pins the ``log_uri`` family
    ONLY.  The error family (``error_code``/``error_message``) is subject to the
    same D3 contract and is structurally invisible here -- and a name scan could
    not be widened to cover it honestly either, because
    ``project_forecast_cohort_tasks`` spells its parameter
    ``master_error_message``, so a parameter-name scan would silently miss a real
    leg while looking complete.  Error-family coverage is therefore per leg, not
    a class property: see the J13-J20 tests plus
    ``test_defer_projection_placeholder_error_replay_stops_appending_records``
    and
    ``test_defer_projection_placeholder_does_not_displace_a_real_error_family``.
    ``mark_pipeline_job_permanently_failed`` is the known uncovered leg (#1630).

    Mechanism boundary, stated rather than implied: this scans for a ``log_uri``
    PARAMETER.  Row-taking writers such as ``upsert_pipeline_job`` and
    ``reserve_pipeline_job`` carry ``log_uri`` inside a record mapping, are
    invisible to this scan, and are therefore neither covered by it nor
    listable as exclusions -- listing one would itself turn this RED.  Their
    anti-laundering guarantee comes from the single record-construction site
    pinned by ``test_journal_records_have_exactly_one_construction_site``.
    """

    import inspect

    taking_log_uri = set()
    for name in dir(FileOrchestrationJournalRepository):
        if name.startswith("_"):
            continue
        if not callable(inspect.getattr_static(FileOrchestrationJournalRepository, name)):
            continue
        signature = inspect.signature(getattr(FileOrchestrationJournalRepository, name))
        if "log_uri" in signature.parameters:
            taking_log_uri.add(name)

    tabled = [leg for leg, _prepare, _write, _appends in _LOG_URI_WRITE_LEGS]
    assert len(tabled) == len(set(tabled)), tabled
    assert not set(tabled) & set(_LOG_URI_WRITE_LEG_EXCLUSIONS), "a leg cannot be both tabled and excluded"

    assert not taking_log_uri - set(tabled) - set(_LOG_URI_WRITE_LEG_EXCLUSIONS), (
        "new caller-log_uri write leg is neither in _LOG_URI_WRITE_LEGS nor excluded"
    )
    assert not (set(tabled) | set(_LOG_URI_WRITE_LEG_EXCLUSIONS)) - taking_log_uri, (
        "_LOG_URI_WRITE_LEGS / exclusions name something that no longer takes a caller log_uri"
    )


def test_journal_records_have_exactly_one_construction_site() -> None:
    """#1592 structural guard: no second record constructor in this module.

    The whole point of putting the anti-laundering strip in
    ``_journal_record_for_write`` is that a write path added later inherits it.
    That only holds while this stays the ONE place a journal record dict is
    built, so this walks the module's AST for any dict literal carrying the full
    record key set and requires the sole hit to be inside that function.

    Known limits, stated rather than papered over: it sees literal dict
    construction in THIS module only.  A record assembled by ``dict()`` /
    ``update()`` / a comprehension, or one built in another module, would slip
    past.  It is a cheap guard against the most likely regression -- someone
    copying the constructor next to a new write path -- not a proof of the
    architectural constraint.

    It keys on the innermost enclosing function NAME, which on its own a second
    function of the same name would satisfy, so the definition count is asserted
    too: one hit, inside the one definition.
    """

    import ast

    record_keys = {"schema_version", "sequence", "record_type", "source_id", "cycle_time"}
    module_path = Path(journal_module.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))

    sites: list[tuple[str, int]] = []
    enclosing: list[str] = []
    definitions: list[int] = []

    def walk(node: ast.AST) -> None:
        pushed = False
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            enclosing.append(node.name)
            pushed = True
            if node.name == "_journal_record_for_write":
                definitions.append(node.lineno)
        if isinstance(node, ast.Dict):
            keys = {
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
            if record_keys <= keys:
                sites.append((enclosing[-1] if enclosing else "<module>", node.lineno))
        for child in ast.iter_child_nodes(node):
            walk(child)
        if pushed:
            enclosing.pop()

    walk(tree)

    assert [name for name, _line in sites] == ["_journal_record_for_write"], sites
    assert len(definitions) == 1, definitions


# ---------------------------------------------------------------------------
# #1734: cycle-scoped single-row journal lookups
# ---------------------------------------------------------------------------

_NARROWING_CYCLES: tuple[tuple[str, str], ...] = (
    ("gfs", "2026-06-28T00:00:00Z"),
    ("gfs", "2026-06-28T12:00:00Z"),
    ("IFS", "2026-06-28T00:00:00Z"),
    ("IFS", "2026-06-28T12:00:00Z"),
)


def _narrowing_row(
    *,
    source_id: str,
    cycle_time: datetime,
    job_id: str,
    run_id: str,
    stage: str,
    idempotency_key: str,
    model_id: str | None,
    job_type: str,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "run_id": run_id,
        "cycle_id": cycle_id_for(source_id, cycle_time),
        "job_type": job_type,
        "model_id": model_id,
        "status": "reserved",
        "stage": stage,
        "idempotency_key": idempotency_key,
        "candidate_id": "candidate_a",
    }


def _populate_narrowing_journal(tmp_path: Path) -> Any:
    """Populate one journal across several cycles and both sources.

    Every row goes in through the production writers, so the on-disk layout is
    the real one: cohort master rows and non-forecast-stage rows land in the
    flat ``pipeline-jobs/`` file (never the by-cycle partition), and only
    model-scoped rows materialise a ``latest/`` view.
    """

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    for source_id, cycle_text in _NARROWING_CYCLES:
        cycle_time = _dt(cycle_text)
        segment = source_id.lower()
        stamp = format_cycle_time(cycle_time)
        cohort_run_id = f"cycle_{segment}_{stamp}"
        rows = [
            # Cohort master shape: no model_id, so no latest/ view is written
            # and the direct row lands in the flat pipeline-jobs/ file.
            _narrowing_row(
                source_id=source_id,
                cycle_time=cycle_time,
                job_id=f"job_{cohort_run_id}_forecast",
                run_id=cohort_run_id,
                stage="forecast",
                idempotency_key=f"{cohort_run_id}:forecast",
                model_id=None,
                job_type="run_shud_forecast_array",
            ),
            # Non-forecast stage: also outside the by-cycle partition.
            _narrowing_row(
                source_id=source_id,
                cycle_time=cycle_time,
                job_id=f"job_{cohort_run_id}_convert",
                run_id=cohort_run_id,
                stage="convert",
                idempotency_key=f"{cohort_run_id}:convert",
                model_id="model_a",
                job_type="convert_grib",
            ),
            # Per-candidate forecast row: forecast run-id shape, and a
            # per-candidate idempotency key that carries no run id at all.
            _narrowing_row(
                source_id=source_id,
                cycle_time=cycle_time,
                job_id=f"job_fcst_{segment}_{stamp}_model_a",
                run_id=f"fcst_{segment}_{stamp}_model_a",
                stage="forecast",
                idempotency_key=f"{segment}:{cycle_id_for(source_id, cycle_time)}:basin_a:forecast",
                model_id="model_a",
                job_type="run_shud_forecast",
            ),
        ]
        for row in rows:
            assert repository.reserve_pipeline_job(dict(row)) is not None
        # Drive a second write for one row per cycle so the narrowed replay has
        # a real last-write-wins merge to resolve, not a single record.
        assert (
            repository.bind_pipeline_job_reservation(
                f"{cohort_run_id}:convert",
                slurm_job_id=f"7{stamp[-5:]}",
            )
            is not None
        )
        repository.update_pipeline_job_status(f"job_{cohort_run_id}_convert", "running")
    return repository


def _whole_tree_rows(repository: Any) -> list[dict[str, Any]]:
    return [journal_module._public_scheduler_row(job) for job in repository._iter_pipeline_job_records()]


def _whole_tree_filtered(repository: Any, field: str, value: str) -> list[dict[str, Any]]:
    rows = [row for row in _whole_tree_rows(repository) if str(row.get(field) or "") == value]
    rows.sort(key=journal_module._db_compatible_pipeline_job_order_key)
    return rows


def _narrowing_keys(repository: Any) -> tuple[list[str], list[str], list[str]]:
    rows = _whole_tree_rows(repository)
    cycle_ids = sorted({str(row["cycle_id"]) for row in rows})
    run_ids = sorted({str(row["run_id"]) for row in rows})
    keys = sorted({str(row["idempotency_key"]) for row in rows if row.get("idempotency_key")})
    return cycle_ids, run_ids, keys


def test_narrowed_journal_lookups_equal_the_whole_tree_scan_filtered(tmp_path: Path) -> None:
    """#1734 I1: narrowing is a restriction of the INPUT SET, never a new merge.

    For every key reachable by each narrowed entrypoint, the cycle-scoped
    answer must be list-equal to the whole-tree answer filtered by that key —
    same rows, same last-write-wins resolution, same
    ``_db_compatible_pipeline_job_order_key`` ordering.
    """

    repository = _populate_narrowing_journal(tmp_path)
    cycle_ids, run_ids, idempotency_keys = _narrowing_keys(repository)
    assert len(cycle_ids) == len(_NARROWING_CYCLES)
    assert len(run_ids) == 2 * len(_NARROWING_CYCLES)
    assert len(idempotency_keys) == 3 * len(_NARROWING_CYCLES)

    for cycle_id in cycle_ids:
        assert repository.query_pipeline_jobs_by_cycle(cycle_id) == _whole_tree_filtered(
            repository, "cycle_id", cycle_id
        )
    for run_id in run_ids:
        assert repository.query_pipeline_jobs_by_run(run_id) == _whole_tree_filtered(repository, "run_id", run_id)
    for idempotency_key in idempotency_keys:
        expected = _whole_tree_filtered(repository, "idempotency_key", idempotency_key)
        assert len(expected) == 1
        assert repository.query_candidate_state(idempotency_key) == expected[0]
        unlocked = repository._candidate_job_for_idempotency_unlocked(idempotency_key)
        assert unlocked is not None
        assert journal_module._public_scheduler_row(unlocked) == expected[0]

    # D1a: the by-job-id entrypoint is narrowed too. Blind its direct probe so
    # every id is answered by the narrowed replay, which is the path under test.
    job_ids = sorted({str(row["job_id"]) for row in _whole_tree_rows(repository)})
    assert len(job_ids) == 3 * len(_NARROWING_CYCLES)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            FileOrchestrationJournalRepository,
            "_direct_pipeline_job_record",
            lambda self, expected_job_id: None,
        )
        for job_id in job_ids:
            expected = _whole_tree_filtered(repository, "job_id", job_id)
            assert len(expected) == 1
            replayed = repository._pipeline_job_for_id_unlocked(job_id)
            assert replayed is not None, job_id
            assert journal_module._public_scheduler_row(replayed) == expected[0]


def test_narrowed_journal_lookup_touches_no_foreign_cycle_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1734 I2: a narrowed single-key lookup opens no other cycle's files."""

    repository = _populate_narrowing_journal(tmp_path)
    touched: list[Path] = []
    real_read_json = FileOrchestrationJournalRepository._read_optional_json
    real_read_jsonl = FileOrchestrationJournalRepository._read_jsonl

    def record_json(self: Any, path: Path) -> Any:
        touched.append(path)
        return real_read_json(self, path)

    def record_jsonl(self: Any, path: Path, **kwargs: Any) -> Any:
        touched.append(path)
        return real_read_jsonl(self, path, **kwargs)

    monkeypatch.setattr(FileOrchestrationJournalRepository, "_read_optional_json", record_json)
    monkeypatch.setattr(FileOrchestrationJournalRepository, "_read_jsonl", record_jsonl)

    target_source, target_cycle_text = _NARROWING_CYCLES[0]
    target_cycle = _dt(target_cycle_text)
    target_stamp = format_cycle_time(target_cycle)
    foreign_stamps = {
        format_cycle_time(_dt(cycle_text))
        for source_id, cycle_text in _NARROWING_CYCLES
        if (source_id, cycle_text) != (target_source, target_cycle_text)
    } - {target_stamp}
    assert foreign_stamps

    # D2a: a flat direct file whose NAME does not resolve to any (source,
    # cycle) must still be read -- the filename filter falls open.
    unparseable_flat = repository.root / "pipeline-jobs" / "cycle_gfs_2026062800_retry_active.json"
    unparseable_flat.write_text(
        json.dumps(
            {
                "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
                "sequence": 1,
                "record_type": "pipeline_job",
                "source_id": "gfs",
                "cycle_time": target_cycle.isoformat(),
                "payload": {
                    **_active_job(target_cycle),
                    "job_id": "cycle_gfs_2026062800_retry_active",
                    "idempotency_key": "cycle_gfs_2026062800:retry_active",
                },
            },
            default=str,
        ),
        encoding="utf-8",
    )
    assert journal_module._cycle_scope_from_job_id(unparseable_flat.stem) is None

    # A flat file that names a DIFFERENT cycle must never be opened.
    foreign_flat = repository.root / "pipeline-jobs" / "job_cycle_ifs_2026062812_forecast.json"
    assert foreign_flat.is_file()

    convert_job_id = f"job_cycle_{target_source.lower()}_{target_stamp}_convert"

    def by_job_id_replay() -> Any:
        # Blind the exact-path direct probe so the narrowed REPLAY is what runs;
        # that is the path D1a changed. (The probe's own hit path delegates to
        # the pre-existing cached ``_cycle_rows`` reader, which is not part of
        # this change.)
        with pytest.MonkeyPatch.context() as blind:
            blind.setattr(
                FileOrchestrationJournalRepository,
                "_direct_pipeline_job_record",
                lambda self, expected_job_id: None,
            )
            return repository._pipeline_job_for_id_unlocked(convert_job_id)

    for lookup in (
        lambda: repository.query_pipeline_jobs_by_cycle(cycle_id_for(target_source, target_cycle)),
        lambda: repository.query_pipeline_jobs_by_run(f"cycle_{target_source.lower()}_{target_stamp}"),
        lambda: repository.query_candidate_state(f"cycle_{target_source.lower()}_{target_stamp}:forecast"),
        by_job_id_replay,
    ):
        # #1734 D10: containment is a property of the COLD path -- the memo can
        # only shrink the opened set, never widen it -- so each lookup is
        # measured against an empty memo. The memo's own behaviour is pinned by
        # ``test_cycle_scoped_replay_memo_is_not_evicted_by_a_write_to_another_cycle``.
        repository._cycle_job_records_cache.clear()
        touched.clear()
        assert lookup()
        relatives = [path.relative_to(repository.root).as_posix() for path in touched]
        assert relatives, "the lookup must still read something"
        for relative in relatives:
            assert relative.startswith(("latest/", "journal/", "pipeline-jobs/")), relative
            if relative.startswith("pipeline-jobs/"):
                continue
            assert target_stamp in relative, relative
        # The decisive assertion: not one file of another cycle, on any of the
        # three surfaces -- flat direct included, per D2a.
        assert not [
            relative for relative in relatives if any(stamp in relative for stamp in foreign_stamps)
        ], relatives
        assert foreign_flat.relative_to(repository.root).as_posix() not in relatives

    # The unparseable name is read by the replaying lookups (fall open), even
    # though the by-cycle/by-run lookups filter its row out of the answer.
    repository._cycle_job_records_cache.clear()
    touched.clear()
    assert repository.query_pipeline_jobs_by_cycle(cycle_id_for(target_source, target_cycle))
    assert unparseable_flat.relative_to(repository.root).as_posix() in [
        path.relative_to(repository.root).as_posix() for path in touched
    ]


def test_narrowed_journal_lookup_resolves_source_case_mismatch(tmp_path: Path) -> None:
    """#1734 I4: run ids spell ``ifs`` while the directory is ``IFS``.

    A derivation that used the raw run-id segment would look in
    ``journal/ifs/``, find nothing and answer "no rows" — a silent miss on
    every non-GFS source.
    """

    repository = _populate_narrowing_journal(tmp_path)
    cycle_time = _dt("2026-06-28T12:00:00Z")
    stamp = format_cycle_time(cycle_time)
    assert (repository.root / "journal" / "IFS" / f"{stamp}.jsonl").is_file()
    # macOS journals live on a case-insensitive filesystem, so directory
    # absence cannot carry this pin. The derivation itself is what must
    # normalise, and that is filesystem independent.
    run_id = f"cycle_ifs_{stamp}"
    assert run_id.islower()
    assert journal_module._cycle_scope_from_file_run_id(run_id) == ("IFS", cycle_time)
    assert journal_module._cycle_scope_from_idempotency_key(f"{run_id}:forecast") == ("IFS", cycle_time)
    assert "IFS" in journal_module._cycle_read_source_segments(
        source_id="IFS",
        source_segment_override=None,
    )
    rows = repository.query_pipeline_jobs_by_run(run_id)
    assert rows == _whole_tree_filtered(repository, "run_id", run_id)
    assert rows

    state = repository.query_candidate_state(f"{run_id}:forecast")
    assert state is not None
    assert state["job_id"] == f"job_{run_id}_forecast"

    cycle_id = cycle_id_for("IFS", cycle_time)
    assert cycle_id.startswith("ifs_")
    assert repository.query_pipeline_jobs_by_cycle(cycle_id) == _whole_tree_filtered(repository, "cycle_id", cycle_id)

def _filesystem_is_case_sensitive(tmp_path: Path) -> bool:
    probe = tmp_path / "case_probe" / "gfs"
    probe.mkdir(parents=True)
    return not (probe.parent / "GFS").exists()


def test_narrowed_journal_lookup_reads_each_source_directory_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1734: case aliases must not enumerate ONE directory twice.

    ``_cycle_read_source_segments`` offers both spellings of the source so a
    case-sensitive filesystem holding ``journal/IFS`` and ``journal/ifs`` side
    by side is read whole. On a case-insensitive filesystem both spellings name
    the same directory, and reading both would replay every record twice --
    inflating record/file budgets and making local budget assertions pass for
    the wrong reason.
    """

    repository = _populate_narrowing_journal(tmp_path)
    touched: list[Path] = []
    real_read_json = FileOrchestrationJournalRepository._read_optional_json
    real_read_jsonl = FileOrchestrationJournalRepository._read_jsonl

    def record_json(self: Any, path: Path) -> Any:
        touched.append(path)
        return real_read_json(self, path)

    def record_jsonl(self: Any, path: Path, **kwargs: Any) -> Any:
        touched.append(path)
        return real_read_jsonl(self, path, **kwargs)

    monkeypatch.setattr(FileOrchestrationJournalRepository, "_read_optional_json", record_json)
    monkeypatch.setattr(FileOrchestrationJournalRepository, "_read_jsonl", record_jsonl)

    cycle_time = _dt("2026-06-28T00:00:00Z")
    assert repository.query_pipeline_jobs_by_cycle(cycle_id_for("IFS", cycle_time))
    assert touched

    spellings_by_identity: dict[tuple[int, int], set[str]] = {}
    for path in touched:
        path_stat = os.stat(path, follow_symlinks=False)
        spellings_by_identity.setdefault((path_stat.st_dev, path_stat.st_ino), set()).add(str(path))
    duplicated = {
        identity: sorted(spellings)
        for identity, spellings in spellings_by_identity.items()
        if len(spellings) > 1
    }
    assert not duplicated, duplicated


def test_narrowed_journal_lookup_still_reads_distinct_case_source_directories(tmp_path: Path) -> None:
    """#1734: the identity dedup must not collapse two REAL directories.

    Only reachable where the filesystem is case sensitive; that is exactly the
    filesystem the alias list exists for.
    """

    if not _filesystem_is_case_sensitive(tmp_path):
        pytest.skip("case-insensitive filesystem cannot hold both spellings")
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    lower_job = _active_job(cycle_time)
    upper_job = _active_job(cycle_time)
    upper_job.update(
        {
            "job_id": "job_cycle_gfs_2026062800_convert",
            "idempotency_key": "cycle_gfs_2026062800:convert",
            "stage": "convert",
            "job_type": "convert_grib",
        }
    )
    for segment, job in (("gfs", lower_job), ("GFS", upper_job)):
        _write_jsonl(
            journal_root / f"journal/{segment}/2026062800.jsonl",
            [
                _journal_record(
                    record_type="pipeline_job",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    payload=job,
                )
            ],
        )

    assert journal_module._cycle_read_source_segments(
        source_id="gfs",
        source_segment_override=None,
        root=journal_root,
    ) == ("gfs", "GFS")
    rows = FileOrchestrationJournalRepository(journal_root).query_pipeline_jobs_by_cycle(
        cycle_id_for("gfs", cycle_time)
    )

    assert {row["job_id"] for row in rows} == {lower_job["job_id"], upper_job["job_id"]}


def _make_source_segment_surfaces(root: Path, segment: str) -> None:
    for surface in journal_module._SOURCE_SEGMENT_SURFACES:
        root.joinpath(*surface.split("/"), segment).mkdir(parents=True, exist_ok=True)


@pytest.mark.parametrize(
    "stat_error",
    [
        FileNotFoundError(errno.ENOENT, "No such file or directory"),
        PermissionError(errno.EACCES, "Permission denied"),
        OSError(errno.EIO, "Input/output error"),
        ValueError("embedded null byte"),
    ],
    ids=["file_not_found", "permission_denied", "generic_oserror", "value_error"],
)
def test_case_alias_segment_survives_unprovable_identity(
    tmp_path: Path,
    stat_error: Exception,
) -> None:
    """#1734: an unprovable case-alias identity must fail OPEN, never closed.

    ``_source_segment_directory_identities`` swallows every ``stat`` failure
    and simply omits that surface, so ``_names_same_directory`` cannot prove
    the alias names the primary's directory and the alias stays in the read
    list. Keeping it costs at most a second enumeration of an absent or
    unreadable directory; dropping it would silently lose every row that
    lives under the alias spelling -- no error, no log, no exit code, just a
    short query result. This pins the open direction against any refactor
    that "tidies" the swallow into a drop.

    The failure is injected rather than filesystem-derived, so the assertion
    is identical on case-sensitive and case-insensitive filesystems and the
    test never skips.
    """

    root = tmp_path / "journal"
    _make_source_segment_surfaces(root, "gfs")
    real_stat = os.stat
    probed_alias: list[str] = []

    def failing_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        if Path(path).name == "GFS":
            probed_alias.append(str(path))
            raise stat_error
        return real_stat(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(journal_module.os, "stat", failing_stat)
        segments = journal_module._cycle_read_source_segments(
            source_id="gfs",
            source_segment_override=None,
            root=root,
        )

    # Without this the assertion below could pass for the wrong reason: a
    # path-construction change that stopped probing the alias at all.
    assert probed_alias, "alias identity probe never ran"
    assert segments == ("gfs", "GFS")


def test_case_alias_segment_survives_absent_primary_directories(tmp_path: Path) -> None:
    """#1734: an empty identity map means "not proven same", so both stay.

    When no surface directory exists for the primary spelling there is
    nothing for the alias to be identical to. ``_names_same_directory``
    answers ``False`` and both spellings are read -- the same fail-open
    direction as a ``stat`` failure, and equally unasserted until now.
    """

    root = tmp_path / "journal"
    root.mkdir()

    assert journal_module._source_segment_directory_identities(root, "gfs") == {}
    assert journal_module._cycle_read_source_segments(
        source_id="gfs",
        source_segment_override=None,
        root=root,
    ) == ("gfs", "GFS")



@pytest.mark.parametrize(
    "underivable_key",
    [
        "gfs:gfs_2026062800:basin_a:forecast",
        "cycle_gfs_20260628zz:forecast",
        "cycle_mars_2026062800:forecast",
        "not-a-run-id",
    ],
)
def test_underivable_idempotency_key_falls_open_to_the_whole_tree_scan(
    tmp_path: Path,
    underivable_key: str,
) -> None:
    """#1734 I3: derivation failure costs the old full scan, never a false miss.

    The per-candidate key shape carries no run id, so it is the production case
    that must keep working through the fallback. A narrowed lookup that
    answered ``None`` here would double-submit a cohort.
    """

    repository = _populate_narrowing_journal(tmp_path)
    assert journal_module._cycle_scope_from_idempotency_key(underivable_key) is None

    expected = _whole_tree_filtered(repository, "idempotency_key", underivable_key)
    observed = repository.query_candidate_state(underivable_key)
    if expected:
        assert observed == expected[0]
        assert observed is not None
    else:
        assert observed is None

    # The production per-candidate key is one of the rows this journal holds,
    # so at least that parametrisation proves the fallback returns a real row.
    if underivable_key.startswith("gfs:"):
        assert observed is not None
        assert observed["job_id"] == "job_fcst_gfs_2026062800_model_a"


@pytest.mark.parametrize(
    "underivable_cycle_id",
    ["mars_2026062800", "2026062800", "GFS_2026062800", "gfs_20260628zz"],
)
def test_underivable_cycle_id_falls_open_instead_of_reporting_a_blocked_read(
    tmp_path: Path,
    underivable_cycle_id: str,
) -> None:
    """#1734 I3: derivation runs OUTSIDE the blocked-row handler.

    ``_source_cycle_from_cycle_id`` raises ``FileOrchestrationJournalError`` on
    an unknown source or a failed round trip. If the derivation ran inside
    ``query_pipeline_jobs_by_cycle``'s ``except`` clause, these keys would come
    back as a synthetic ``file_journal_read_blocked`` row instead of the
    whole-tree answer.
    """

    repository = _populate_narrowing_journal(tmp_path)
    assert journal_module._cycle_scope_from_cycle_id(underivable_cycle_id) is None

    rows = repository.query_pipeline_jobs_by_cycle(underivable_cycle_id)
    assert rows == _whole_tree_filtered(repository, "cycle_id", underivable_cycle_id)
    assert rows == []
    assert [row.get("job_id") for row in rows] != ["file_journal_read_blocked"]


def test_narrowed_lookup_returns_cohort_master_and_non_forecast_rows(tmp_path: Path) -> None:
    """#1734 I5 / D2: the by-cycle direct partition is a STRICT SUBSET.

    ``_write_pipeline_job_direct_unlocked`` only routes current accepted-submit
    *candidate* rows into ``pipeline-jobs/by-cycle/``; cohort master rows and
    every non-forecast-stage row go to the flat file. A narrowing implemented
    as "read the by-cycle partition" would drop exactly the rows asserted here.
    """

    repository = _populate_narrowing_journal(tmp_path)
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    cohort_run_id = f"cycle_gfs_{stamp}"

    partition = repository.root / "pipeline-jobs" / "by-cycle" / "gfs" / stamp
    partition_job_ids = {path.stem for path in partition.glob("*.json")} if partition.is_dir() else set()
    master_job_id = f"job_{cohort_run_id}_forecast"
    convert_job_id = f"job_{cohort_run_id}_convert"
    assert master_job_id not in partition_job_ids
    assert convert_job_id not in partition_job_ids

    rows = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
    assert master_job_id in {row["job_id"] for row in rows}
    assert convert_job_id in {row["job_id"] for row in rows}

    by_run = repository.query_pipeline_jobs_by_run(cohort_run_id)
    assert {row["job_id"] for row in by_run} == {master_job_id, convert_job_id}

    master_state = repository.query_candidate_state(f"{cohort_run_id}:forecast")
    assert master_state is not None
    assert master_state["job_id"] == master_job_id
    convert_state = repository.query_candidate_state(f"{cohort_run_id}:convert")
    assert convert_state is not None
    # The merged last write, not the reservation record.
    assert convert_state["status"] == "running"
    assert convert_state["slurm_job_id"] == f"7{stamp[-5:]}"


def test_cycle_scoped_replay_include_direct_false_still_excludes_direct_records(
    tmp_path: Path,
) -> None:
    """#1734 I6 / D6: the flag keeps its exact meaning in the shared merge path.

    ``_pipeline_job_for_id_unlocked`` relies on ``include_direct=False`` so its
    fallback never re-counts the direct record it already probed. The direct
    surface is pinned to a job id that no view or journal segment of this cycle
    carries, so dropping the flag would surface it here.
    """

    repository = _populate_narrowing_journal(tmp_path)
    cycle_time = _dt("2026-06-28T00:00:00Z")
    orphan_job_id = "job_cycle_gfs_2026062800_direct_only"
    orphan_record = {
        "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
        "sequence": 1,
        "record_type": "pipeline_job",
        "source_id": "gfs",
        "cycle_time": cycle_time.isoformat(),
        "payload": {
            **_active_job(cycle_time),
            "job_id": orphan_job_id,
            "idempotency_key": "cycle_gfs_2026062800:direct_only",
        },
    }
    _write_json(repository.root / "pipeline-jobs" / f"{orphan_job_id}.json", orphan_record)

    def replayed_job_ids(*, include_direct: bool) -> list[str]:
        return [
            str(job["job_id"])
            for job in repository._iter_pipeline_job_records_for_cycle(
                source_id="gfs",
                cycle_time=cycle_time,
                include_direct=include_direct,
            )
        ]

    with_direct = replayed_job_ids(include_direct=True)
    without_direct = replayed_job_ids(include_direct=False)
    assert orphan_job_id in with_direct
    assert orphan_job_id not in without_direct
    # D2a filters the flat surface to this cycle, so the flag is now the ONLY
    # difference between the two replays.
    assert [job_id for job_id in with_direct if job_id != orphan_job_id] == without_direct

    # Same meaning as the whole-tree scan it shares the merge path with.
    whole_tree = [str(job["job_id"]) for job in repository._iter_pipeline_job_records(include_direct=False)]
    assert orphan_job_id not in whole_tree
    # And no duplication: a job id appears at most once either way.
    assert len(set(with_direct)) == len(with_direct)

    # D1a/D6: the parity must hold for _pipeline_job_for_id_unlocked's OWN
    # narrowed fallback, not only for the shared iterator. The orphan row lives
    # solely on the direct surface, so the fallback replay (include_direct=
    # False) must not find it there -- only the direct probe ahead of the
    # replay may return it, exactly once and unduplicated.
    resolved = repository._pipeline_job_for_id_unlocked(orphan_job_id)
    assert resolved is not None
    assert resolved["job_id"] == orphan_job_id
    assert journal_module._cycle_scope_from_job_id(orphan_job_id) == ("gfs", cycle_time)

    replay_only: list[str] = []
    real_direct = FileOrchestrationJournalRepository._direct_pipeline_job_record

    def blind_direct(self: Any, expected_job_id: str) -> Any:
        replay_only.append(expected_job_id)
        return None

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FileOrchestrationJournalRepository, "_direct_pipeline_job_record", blind_direct)
        # With the direct probe blinded the entrypoint has only its narrowed
        # replay left, and include_direct=False keeps the direct surface out of
        # it -- so a direct-only row is genuinely unreachable.
        assert repository._pipeline_job_for_id_unlocked(orphan_job_id) is None
        # A row the cycle's journal carries is still found by that same replay.
        assert repository._pipeline_job_for_id_unlocked("job_cycle_gfs_2026062800_convert") is not None
    assert replay_only
    assert real_direct is FileOrchestrationJournalRepository._direct_pipeline_job_record


def test_cycle_scoped_replay_keeps_the_blocked_row_error_shape(tmp_path: Path) -> None:
    """#1734 I1: a blocked row inside the owning cycle raises identically."""

    repository = _populate_narrowing_journal(tmp_path)
    cycle_time = _dt("2026-06-28T00:00:00Z")
    stamp = format_cycle_time(cycle_time)
    corrupt = repository.root / "latest" / "gfs" / stamp / "model_a.json"
    assert corrupt.is_file()
    corrupt.write_text("{ not json", encoding="utf-8")

    cycle_id = cycle_id_for("gfs", cycle_time)
    rows = repository.query_pipeline_jobs_by_cycle(cycle_id)
    assert len(rows) == 1
    assert rows[0]["job_id"] == "file_journal_read_blocked"
    assert rows[0]["cycle_id"] == cycle_id
    assert rows[0]["error_code"] == "file_journal_malformed_json"

    state = repository.query_candidate_state(f"cycle_gfs_{stamp}:forecast")
    assert state is not None
    assert state["job_id"] == "file_journal_read_blocked"
    assert state["error_code"] == "file_journal_malformed_json"

    with pytest.raises(journal_module.FileOrchestrationJournalError) as caught:
        repository._candidate_job_for_idempotency_unlocked(f"cycle_gfs_{stamp}:forecast")
    assert caught.value.reason == "file_journal_malformed_json"


def test_untouched_journal_entrypoints_still_replay_the_whole_tree(tmp_path: Path) -> None:
    """#1734 D1a / I8: only ``query_pipeline_job_by_slurm_id`` still full-scans.

    Its argument carries no derivable cycle and it has zero production callers
    (Task 1(b) measured 0), so it stays on the whole-tree replay and remains
    everyone's fall-open path.
    """

    repository = _populate_narrowing_journal(tmp_path)
    calls: list[str] = []
    real_full = FileOrchestrationJournalRepository._iter_pipeline_job_records
    real_scoped = FileOrchestrationJournalRepository._iter_pipeline_job_records_for_cycle

    def full(self: Any, **kwargs: Any) -> Any:
        calls.append("full")
        return real_full(self, **kwargs)

    def scoped(self: Any, **kwargs: Any) -> Any:
        calls.append("scoped")
        return real_scoped(self, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FileOrchestrationJournalRepository, "_iter_pipeline_job_records", full)
        patch.setattr(FileOrchestrationJournalRepository, "_iter_pipeline_job_records_for_cycle", scoped)

        assert repository.query_pipeline_job_by_slurm_id("no-such-slurm-id") is None
        assert calls == ["full"]

        calls.clear()
        assert repository.query_pipeline_jobs_by_cycle("gfs_2026062800")
        assert calls == ["scoped"]

        # D1a: a parseable job id narrows its direct-miss fallback too.
        calls.clear()
        with pytest.MonkeyPatch.context() as blind:
            blind.setattr(
                FileOrchestrationJournalRepository,
                "_direct_pipeline_job_record",
                lambda self, expected_job_id: None,
            )
            assert repository._pipeline_job_for_id_unlocked("job_cycle_gfs_2026062800_convert") is not None
            assert calls == ["scoped"]

            # An unparseable job id falls open to the whole-tree replay (D4).
            calls.clear()
            assert repository._pipeline_job_for_id_unlocked("cycle_gfs_2026062800_retry_active") is None
            assert calls == ["full"]


@pytest.mark.parametrize(
    ("job_id", "expected_scope"),
    [
        ("job_fcst_gfs_2026062800_model_a", ("gfs", "2026-06-28T00:00:00Z")),
        ("job_fcst_ifs_2026062812_model_a", ("IFS", "2026-06-28T12:00:00Z")),
        ("job_cycle_gfs_2026062800_forecast", ("gfs", "2026-06-28T00:00:00Z")),
        ("job_cycle_ifs_2026062800_convert_retry_2", ("IFS", "2026-06-28T00:00:00Z")),
        # Fall open (D4): no ``job_`` prefix, unknown source, bad cycle token,
        # and a bare shape with nothing after the cycle.
        ("cycle_gfs_2026062800_retry_active", None),
        ("job_cycle_mars_2026062800_forecast", None),
        ("job_fcst_gfs_20260628zz_model_a", None),
        ("job_fcst_gfs_2026062800", None),
    ],
)
def test_job_id_cycle_scope_derivation_covers_both_shapes_and_falls_open(
    job_id: str,
    expected_scope: tuple[str, str] | None,
) -> None:
    """#1734 D1a: both live job id shapes carry ``(source, cycle)``.

    The reversed ruling rests on this. ``job_fcst_`` is already used by
    ``_direct_pipeline_job_record`` to route a by-cycle partition read, and
    ``job_cycle_`` is the cohort master shape. Anything else falls open.
    """

    observed = journal_module._cycle_scope_from_job_id(job_id)
    if expected_scope is None:
        assert observed is None
    else:
        assert observed == (expected_scope[0], _dt(expected_scope[1]))


def test_flat_direct_surface_is_filtered_by_filename_with_fall_open(tmp_path: Path) -> None:
    """#1734 D2a: skip only names resolving to a DIFFERENT (source, cycle).

    The flat directory holds one file per cohort master and per non-forecast
    stage row for all retained history (4,303 files / 12.29 MiB on node-22), so
    reading it whole would dominate the cycle slice and leave the growth law
    intact. The filter fails toward reading too much: an unresolvable name is
    read, never skipped.
    """

    repository = _populate_narrowing_journal(tmp_path)
    cycle_time = _dt("2026-06-28T00:00:00Z")
    flat = repository.root / "pipeline-jobs"

    unparseable = flat / "cycle_gfs_2026062800_retry_active.json"
    unparseable.write_text(
        json.dumps(
            {
                "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
                "sequence": 1,
                "record_type": "pipeline_job",
                "source_id": "gfs",
                "cycle_time": cycle_time.isoformat(),
                "payload": {
                    **_active_job(cycle_time),
                    "job_id": "cycle_gfs_2026062800_retry_active",
                    "idempotency_key": "cycle_gfs_2026062800:retry_active",
                },
            },
            default=str,
        ),
        encoding="utf-8",
    )

    all_flat = sorted(path.name for path in flat.glob("*.json"))
    assert len(all_flat) > 4, all_flat
    assert any(name.startswith("job_cycle_ifs_") for name in all_flat)

    scoped = list(
        repository._iter_flat_direct_pipeline_job_records_for_cycle(
            source_id="gfs",
            cycle_time=cycle_time,
        )
    )
    scoped_ids = {str(job["job_id"]) for job in scoped}

    # Read: this cycle's rows, plus the unresolvable name under fall-open.
    assert "job_cycle_gfs_2026062800_forecast" in scoped_ids
    assert "cycle_gfs_2026062800_retry_active" in scoped_ids
    # Skipped: every name that resolves to another (source, cycle).
    assert not [job_id for job_id in scoped_ids if "2026062812" in job_id]
    assert not [job_id for job_id in scoped_ids if job_id.startswith("job_cycle_ifs_")]

    # A malformed foreign-cycle file is skipped by name and never decoded, so
    # it cannot block this cycle's lookup.
    (flat / "job_cycle_ifs_2026062812_forecast.json").write_text("{not-json", encoding="utf-8")
    assert repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
    # ... while the same malformed content inside THIS cycle still fails closed.
    (flat / "job_cycle_gfs_2026062800_forecast.json").write_text("{not-json", encoding="utf-8")
    blocked = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
    assert blocked[0]["error_code"] == "file_journal_malformed_json"


@pytest.mark.parametrize(
    ("stem", "in_scope"),
    [
        ("2026062800", True),
        ("2026062800.1", True),
        ("2026062800.x", True),
        # ``parse_cycle_time`` reads this as 2026-06-28T01:00 -- the whole-tree
        # scan attributes it to that other cycle, so skipping it is equivalence,
        # not a silent drop.
        ("20260628001", False),
        ("not-a-cycle", True),
        ("2026062812", False),
        ("2026062812.2", False),
    ],
)
def test_journal_segment_name_filter_falls_open_on_unresolvable_stems(stem: str, in_scope: bool) -> None:
    """#1734: the D2a fall-open shape applied to the cycle event-log surface.

    A stem that resolves to another cycle is skipped; one that resolves to
    nothing is handed to ``_journal_identity_from_path`` so it fails closed with
    the same reason the whole-tree scan raises.
    """

    assert journal_module._journal_segment_stem_in_cycle_scope(stem, "2026062800") is in_scope


# ---------------------------------------------------------------------------
# #1734 round 2: D9 parity, D10 memo, D11 traced read attribution
# ---------------------------------------------------------------------------


def _record_read_paths(repository: Any, patch: Any) -> list[str]:
    """Record every path whose BYTES are actually read from the filesystem.

    Hooking ``_read_bytes_limited_cached`` rather than ``_read_optional_json``
    keeps the probe on the one choke point both readers share, so a memo hit
    is distinguishable from a byte-cache hit: a memo hit performs no call at
    all, while a byte-cache hit still calls through here.
    """

    paths: list[str] = []
    real = FileOrchestrationJournalRepository._read_bytes_limited_cached

    def counting(self: Any, path: Path) -> Any:
        paths.append(str(path))
        return real(self, path)

    patch.setattr(FileOrchestrationJournalRepository, "_read_bytes_limited_cached", counting)
    return paths


def test_cycle_scoped_replay_memo_is_not_evicted_by_a_write_to_another_cycle(
    tmp_path: Path,
) -> None:
    """#1734 D10: the memo's invalidation signature is scoped to ONE cycle.

    ``_direct_jobs_cycle_cache`` keys its signature on
    ``_stat_signature(root / "pipeline-jobs")`` — a shared, unpartitioned
    directory — so any write to any cycle invalidates every entry. It is
    correct and it thrashes. This test is what separates the new memo from
    that pattern: a write to a DIFFERENT cycle must leave this cycle's entry
    intact, while a write to THIS cycle must still invalidate it.
    """

    repository = _populate_narrowing_journal(tmp_path)
    target = _dt("2026-06-28T00:00:00Z")
    other = _dt("2026-06-28T12:00:00Z")

    def replay() -> list[dict[str, Any]]:
        return [
            dict(job)
            for job in repository._iter_pipeline_job_records_for_cycle(
                source_id="gfs",
                cycle_time=target,
            )
        ]

    with pytest.MonkeyPatch.context() as patch:
        reads = _record_read_paths(repository, patch)

        first = replay()
        assert first
        assert reads, "a cold memo must read this cycle's files"

        reads.clear()
        assert replay() == first
        assert reads == [], "a warm memo must not re-read any file"

        # A write that belongs to ANOTHER cycle: same shared flat
        # ``pipeline-jobs/`` directory, same shared ``journal/gfs/`` directory,
        # different cycle. The memo entry for `target` must survive it.
        repository.update_pipeline_job_status("job_cycle_gfs_2026062812_convert", "succeeded")
        assert repository.get_pipeline_job("job_cycle_gfs_2026062812_convert")["status"] == "succeeded"
        reads.clear()
        assert replay() == first
        assert reads == [], "a write to another cycle must not evict this cycle's memo entry"

        # A write that belongs to THIS cycle must invalidate it, and the memo
        # must then serve the new row rather than the stale one.
        repository.update_pipeline_job_status("job_cycle_gfs_2026062800_convert", "succeeded")
        reads.clear()
        refreshed = replay()
        assert reads, "a write to this cycle must invalidate the memo entry"
        statuses = {str(job["job_id"]): str(job["status"]) for job in refreshed}
        assert statuses["job_cycle_gfs_2026062800_convert"] == "succeeded"
        assert {str(job["job_id"]) for job in refreshed} == {str(job["job_id"]) for job in first}

    # Untouched cycles still answer correctly through the same memo.
    assert repository.query_pipeline_jobs_by_cycle(cycle_id_for("IFS", other))


def test_cycle_scoped_replay_memo_separates_include_direct_variants(tmp_path: Path) -> None:
    """#1734 D10: ``include_direct`` is part of the memo key, not a collision.

    ``_pipeline_job_for_id_unlocked`` replays with ``include_direct=False``;
    every other narrowed entrypoint replays with ``True``. A key that ignored
    the flag would serve one variant's rows to the other.
    """

    repository = _populate_narrowing_journal(tmp_path)
    target = _dt("2026-06-28T00:00:00Z")
    orphan_job_id = "job_cycle_gfs_2026062800_direct_only"
    _write_json(
        repository.root / "pipeline-jobs" / f"{orphan_job_id}.json",
        {
            "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
            "sequence": 1,
            "record_type": "pipeline_job",
            "source_id": "gfs",
            "cycle_time": target.isoformat(),
            "payload": {
                **_active_job(target),
                "job_id": orphan_job_id,
                "idempotency_key": "cycle_gfs_2026062800:direct_only",
            },
        },
    )

    def replay(*, include_direct: bool) -> set[str]:
        return {
            str(job["job_id"])
            for job in repository._iter_pipeline_job_records_for_cycle(
                source_id="gfs",
                cycle_time=target,
                include_direct=include_direct,
            )
        }

    with_direct = replay(include_direct=True)
    without_direct = replay(include_direct=False)
    assert orphan_job_id in with_direct
    assert orphan_job_id not in without_direct
    # Re-read in the opposite order: a shared key would now serve the wrong set.
    assert replay(include_direct=False) == without_direct
    assert replay(include_direct=True) == with_direct


def test_direct_cycle_records_skip_flat_files_of_another_cycle(tmp_path: Path) -> None:
    """#1734 D9: the second flat reader gets the D2a prefilter by delegation.

    ``_iter_direct_pipeline_job_records_for_cycle`` used to ``_read_optional_
    json`` every file in the flat ``pipeline-jobs/`` directory before any
    content check — 13.18 MB across 4,375 files on node-22 per cache miss.
    Its flat leg now shares ONE filter definition with
    ``_iter_flat_direct_pipeline_job_records_for_cycle``.
    """

    repository = _populate_narrowing_journal(tmp_path)
    target = _dt("2026-06-28T00:00:00Z")
    flat = repository.root / "pipeline-jobs"
    assert any(path.name.startswith("job_cycle_ifs_") for path in flat.glob("*.json"))

    with pytest.MonkeyPatch.context() as patch:
        reads = _record_read_paths(repository, patch)
        rows = list(
            repository._iter_direct_pipeline_job_records_for_cycle(
                source_id="gfs",
                cycle_time=target,
                model_id=None,
            )
        )

    opened = {Path(path).name for path in reads if Path(path).parent == flat}
    assert "job_cycle_gfs_2026062800_forecast.json" in opened
    assert "job_cycle_gfs_2026062800_convert.json" in opened
    assert "job_cycle_gfs_2026062812_convert.json" not in opened
    assert not [name for name in opened if name.startswith("job_cycle_ifs_")]

    job_ids = {str(job["job_id"]) for job in rows}
    assert job_ids == {
        "job_cycle_gfs_2026062800_forecast",
        "job_cycle_gfs_2026062800_convert",
        "job_fcst_gfs_2026062800_model_a",
    }

    # A malformed file of ANOTHER cycle is skipped by name and never decoded,
    # so it cannot block this cycle's direct scan.
    (flat / "job_cycle_ifs_2026062812_forecast.json").write_text("{not-json", encoding="utf-8")
    assert {
        str(job["job_id"])
        for job in repository._iter_direct_pipeline_job_records_for_cycle(
            source_id="gfs",
            cycle_time=target,
            model_id=None,
        )
    } == job_ids


def test_direct_cycle_records_prefilter_normalises_the_source_case(tmp_path: Path) -> None:
    """#1734 I4 on the delegating path: ``ifs`` and ``IFS`` are one source.

    ``_cycle_scope_from_job_id`` yields the canonical spelling (``IFS``) while
    callers may hand in the lower-case run-id spelling, and
    ``_job_matches_source_cycle`` normalises before comparing. A filename
    prefilter that compared raw strings would skip every file of a source
    passed in the other case — a silent empty result, not a slow one.
    """

    repository = _populate_narrowing_journal(tmp_path)
    target = _dt("2026-06-28T00:00:00Z")
    lower = {
        str(job["job_id"])
        for job in repository._iter_direct_pipeline_job_records_for_cycle(
            source_id="ifs",
            cycle_time=target,
            model_id=None,
        )
    }
    upper = {
        str(job["job_id"])
        for job in repository._iter_direct_pipeline_job_records_for_cycle(
            source_id="IFS",
            cycle_time=target,
            model_id=None,
        )
    }
    assert lower == upper
    assert "job_cycle_ifs_2026062800_forecast" in lower


def test_direct_cycle_records_still_read_unparseable_flat_names(tmp_path: Path) -> None:
    """#1734 D2a fall-open, pinned on the delegating path (D9).

    The prefilter fails toward reading too much: a name that does not resolve
    at all is read, never skipped. Delegation must preserve that by
    construction, not by re-argument.
    """

    repository = _populate_narrowing_journal(tmp_path)
    target = _dt("2026-06-28T00:00:00Z")
    unparseable_job_id = "cycle_gfs_2026062800_retry_active"
    _write_json(
        repository.root / "pipeline-jobs" / f"{unparseable_job_id}.json",
        {
            "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
            "sequence": 1,
            "record_type": "pipeline_job",
            "source_id": "gfs",
            "cycle_time": target.isoformat(),
            "payload": {
                **_active_job(target),
                "job_id": unparseable_job_id,
                "run_id": "cycle_gfs_2026062800",
                "idempotency_key": "cycle_gfs_2026062800:retry_active",
            },
        },
    )
    assert journal_module._cycle_scope_from_job_id(unparseable_job_id) is None

    with pytest.MonkeyPatch.context() as patch:
        reads = _record_read_paths(repository, patch)
        rows = list(
            repository._iter_direct_pipeline_job_records_for_cycle(
                source_id="gfs",
                cycle_time=target,
                model_id=None,
            )
        )
    assert f"{unparseable_job_id}.json" in {Path(path).name for path in reads}
    assert unparseable_job_id in {str(job["job_id"]) for job in rows}

    # ... and on the other reader, which already carried the property.
    assert unparseable_job_id in {
        str(job["job_id"])
        for job in repository._iter_flat_direct_pipeline_job_records_for_cycle(
            source_id="gfs",
            cycle_time=target,
        )
    }


def _seed_unparseable_flat_row(repository: Any, cycle_time: datetime, *, sequence: int = 1) -> str:
    """Write the real legacy flat shape: no ``job_`` prefix, so unparseable."""

    job_id = "cycle_gfs_2026062800_retry_active"
    _write_json(
        repository.root / "pipeline-jobs" / f"{job_id}.json",
        {
            "schema_version": FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
            "sequence": sequence,
            "record_type": "pipeline_job",
            "source_id": "gfs",
            "cycle_time": cycle_time.isoformat(),
            "payload": {
                **_active_job(cycle_time),
                "job_id": job_id,
                "run_id": "cycle_gfs_2026062800",
                "idempotency_key": "cycle_gfs_2026062800:retry_active",
            },
        },
    )
    assert journal_module._cycle_scope_from_job_id(job_id) is None
    return job_id


def test_cycle_scoped_replay_memo_fall_open_residue_is_pinned(tmp_path: Path) -> None:
    """#1734 D13 corrected: the flat leg has two UNSCOPABLE fall-open arms.

    The round-1 discriminating test used only parseable job ids, so it could
    see neither arm. Both are pinned here as they actually behave, because
    narrowing the signature to make them look scoped would serve stale rows:
    these files genuinely are read by every cycle, so invalidating every cycle
    when they change is semantically required, not a defect. What was a defect
    was leaving the residue undeclared.
    """

    repository = _populate_narrowing_journal(tmp_path)
    target = _dt("2026-06-28T00:00:00Z")
    other = _dt("2026-06-28T12:00:00Z")
    unparseable_job_id = _seed_unparseable_flat_row(repository, target)

    def replay(cycle_time: datetime) -> list[dict[str, Any]]:
        return [
            dict(job)
            for job in repository._iter_pipeline_job_records_for_cycle(
                source_id="gfs",
                cycle_time=cycle_time,
            )
        ]

    # ARM 1 — an unparseable file name is selected for EVERY cycle, so a write
    # to it evicts every cycle's entry. Measured in round-1 verification:
    # warming two cycles and touching only this one file evicted both.
    with pytest.MonkeyPatch.context() as patch:
        reads = _record_read_paths(repository, patch)
        warm_target = replay(target)
        warm_other = replay(other)
        assert unparseable_job_id in {str(job["job_id"]) for job in warm_target}
        assert unparseable_job_id in {str(job["job_id"]) for job in warm_other}, (
            "the fall-open arm reaches the OTHER cycle's replay too, which is why "
            "its invalidation cannot be cycle-scoped"
        )

        reads.clear()
        assert replay(target) == warm_target
        assert replay(other) == warm_other
        assert reads == [], "both memo entries must be warm"

        _seed_unparseable_flat_row(repository, target, sequence=2)
        reads.clear()
        assert replay(target)
        target_reads = list(reads)
        reads.clear()
        assert replay(other)
        other_reads = list(reads)

    assert target_reads, "the touched fall-open file must invalidate this cycle"
    assert other_reads, (
        "and the OTHER cycle too — the residue D13 declares, pinned rather than "
        "argued away"
    )

    # ARM 2 — a source_id this instance cannot normalise filters nothing, so
    # the whole flat directory is selected and the entry is invalidated by a
    # write anywhere in it.
    with pytest.raises(FileOrchestrationJournalError):
        journal_module._normalize_file_source_id("unknown_source", field="source_id")
    unnormalisable = [
        dict(job)
        for job in repository._iter_pipeline_job_records_for_cycle(
            source_id="unknown_source",
            cycle_time=target,
        )
    ]
    assert {str(job["cycle_id"]) for job in unnormalisable} > {cycle_id_for("gfs", target)}, (
        "an unusable scope selects the whole flat directory, across every cycle"
    )

    with pytest.MonkeyPatch.context() as patch:
        reads = _record_read_paths(repository, patch)
        assert [
            dict(job)
            for job in repository._iter_pipeline_job_records_for_cycle(
                source_id="unknown_source",
                cycle_time=target,
            )
        ] == unnormalisable
        assert reads == [], "the unnormalisable-source entry must memoize like any other"

        repository.update_pipeline_job_status("job_cycle_ifs_2026062812_convert", "succeeded")
        reads.clear()
        assert repository._iter_pipeline_job_records_for_cycle(
            source_id="unknown_source",
            cycle_time=target,
        )
        assert reads, (
            "a write to ANY flat row evicts it, because its signature covers the "
            "whole flat directory — declared, not fixed"
        )


def test_cycle_scoped_replay_memo_survives_concurrent_readers_and_a_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1734 I7 round-2 vehicle: the NEW memo under concurrency, with eviction.

    The named I7 vehicle
    (``test_file_journal_read_caches_survive_concurrent_readers_and_a_writer``)
    drives only ``_cycle_rows`` and ``_read_bytes_limited_cached``: it never
    reaches ``_cycle_job_records_cache`` at all. The 8-thread counter test does
    populate it incidentally, but at the default capacity of 512 its eviction
    branch never executes and it runs no writer.

    Here the cap is squeezed to 2 while more than 2 distinct memo keys are
    driven — both ``include_direct`` variants across several cycles — so the
    eviction branch runs on every miss, with a writer invalidating one of the
    cycles underneath. The three caches sharing this constant do NOT share a
    budget: they are separate dicts, each bounded by it independently.
    """

    monkeypatch.setattr(journal_module, "MAX_FILE_JOURNAL_CYCLE_ROWS_CACHE_ENTRIES", 2)
    repository = _populate_narrowing_journal(tmp_path)
    cycles = [(source_id, _dt(text)) for source_id, text in _NARROWING_CYCLES]
    writer_source, writer_cycle = cycles[0]
    keys_seen: set[tuple[str, str, bool]] = set()
    real_memo = FileOrchestrationJournalRepository._cycle_job_records_memoized

    def recording(self: Any, *, source_id: str, cycle_time: datetime, include_direct: bool) -> Any:
        keys_seen.add((source_id, format_cycle_time(cycle_time), include_direct))
        return real_memo(self, source_id=source_id, cycle_time=cycle_time, include_direct=include_direct)

    monkeypatch.setattr(FileOrchestrationJournalRepository, "_cycle_job_records_memoized", recording)

    failures: list[BaseException] = []
    stop = threading.Event()
    deadline = time.monotonic() + 0.6

    def read_cycle_jobs() -> None:
        for source_id, cycle_time in cycles:
            assert repository.query_pipeline_jobs_by_cycle(cycle_id_for(source_id, cycle_time))

    def read_missing_job() -> None:
        # No direct row exists for this id, so the lookup falls through to the
        # narrowed replay with ``include_direct=False`` — the other half of the
        # memo key space, and the variant that must never collide with True.
        for source_id, cycle_time in cycles:
            segment = source_id.lower()
            stamp = format_cycle_time(cycle_time)
            assert repository.get_pipeline_job(f"job_cycle_{segment}_{stamp}_absent") is None

    def append_records() -> None:
        repository.update_forecast_cycle_status(
            source_id=writer_source, cycle_time=writer_cycle, status="raw_complete"
        )

    _join_all(
        [
            threading.Thread(
                target=_hammer_until(read_cycle_jobs, stop=stop, deadline=deadline, failures=failures),
                name="cycle-jobs-a",
            ),
            threading.Thread(
                target=_hammer_until(read_cycle_jobs, stop=stop, deadline=deadline, failures=failures),
                name="cycle-jobs-b",
            ),
            threading.Thread(
                target=_hammer_until(read_missing_job, stop=stop, deadline=deadline, failures=failures),
                name="missing-job",
            ),
            threading.Thread(
                target=_hammer_until(append_records, stop=stop, deadline=deadline, failures=failures),
                name="writer",
            ),
        ],
        stop=stop,
    )

    assert not failures, f"{type(failures[0]).__name__}: {failures[0]}"
    assert len(keys_seen) > 2, sorted(keys_seen)
    assert {include_direct for _, _, include_direct in keys_seen} == {True, False}
    # Pigeonhole: more distinct keys were driven than the cap allows, and the
    # cap held — the eviction branch executed.
    assert len(repository._cycle_job_records_cache) <= 2, list(repository._cycle_job_records_cache)


def test_journal_read_counter_attributes_bytes_to_the_driving_entrypoint(
    tmp_path: Path,
) -> None:
    """#1734 D11: the read counter ships in the repo and is on by default.

    node-22 pulls from GitHub, so a monkeypatched probe is not an option. The
    counter must name the entrypoint AND the lane (A full-tree replay / B flat
    direct scan / C cycle-scoped replay) so the design's A/B/C split is
    directly readable off a pass artifact.
    """

    populated = _populate_narrowing_journal(tmp_path)
    cycle_time = _dt("2026-06-28T00:00:00Z")
    # A FRESH instance over the same root: the writer instance left every file
    # in its in-process byte cache, and a byte-cache hit is deliberately not
    # counted as `bytes` because it costs no `rchar`.
    repository = FileOrchestrationJournalRepository(populated.root)

    journal_module.reset_journal_read_counters()
    assert journal_module.journal_read_attribution()["totals"]["calls"] == 0

    assert repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
    scoped = journal_module.journal_read_attribution()
    scoped_tags = {row["tag"]: row for row in scoped["tags"]}
    assert (
        "query_pipeline_jobs_by_cycle|cycle_replay" in scoped_tags
    ), sorted(scoped_tags)
    entry = scoped_tags["query_pipeline_jobs_by_cycle|cycle_replay"]
    assert entry["calls"] > 0
    assert entry["bytes"] > 0
    assert scoped["totals"]["bytes"] >= entry["bytes"]
    assert not [tag for tag in scoped_tags if tag.endswith("|full_tree_replay")]

    journal_module.reset_journal_read_counters()
    assert repository.query_pipeline_job_by_slurm_id("no-such-slurm-id") is None
    full = {row["tag"]: row for row in journal_module.journal_read_attribution()["tags"]}
    assert "query_pipeline_job_by_slurm_id|full_tree_replay" in full, sorted(full)
    assert full["query_pipeline_job_by_slurm_id|full_tree_replay"]["calls"] > 0

    journal_module.reset_journal_read_counters()
    repository._direct_pipeline_job_records_for_cycle_cached(source_id="gfs", cycle_time=cycle_time)
    direct = {row["tag"]: row for row in journal_module.journal_read_attribution()["tags"]}
    assert [tag for tag in direct if tag.endswith("|direct_flat_scan")], sorted(direct)


def _attribution_shares(payload: dict[str, Any]) -> tuple[int, int, int]:
    """(total bytes, bytes with no entrypoint, bytes with no lane)."""

    total = 0
    without_entrypoint = 0
    without_lane = 0
    for row in payload["tags"]:
        entrypoint, _, lane = str(row["tag"]).partition("|")
        total += int(row["bytes"])
        if entrypoint == journal_module._JOURNAL_READ_UNATTRIBUTED:
            without_entrypoint += int(row["bytes"])
        if lane == journal_module._JOURNAL_READ_UNATTRIBUTED:
            without_lane += int(row["bytes"])
    return total, without_entrypoint, without_lane


def test_journal_reads_carry_an_entrypoint_across_the_whole_public_surface(
    tmp_path: Path,
) -> None:
    """#1734 D11 corrected: attribution is an obligation on the read surface.

    Round 1 measured the enumerated-list version of this: six methods carried
    a hand-written tag, the design document claimed eight, and on a real
    308-test fixture **80.6% of counted bytes carried no entrypoint at all** —
    ``query_inflight_jobs``, the cycle-status predicates and every write path
    that reads before it writes were untagged. The residual reconciled
    perfectly while telling nobody anything.

    The spec requires this to be pinned by an assertion rather than by prose,
    so the mix below deliberately includes a cycle predicate, a whole-tree
    query and a write path that reads before it writes — and, because
    ``_install_public_read_attribution`` decorates BOTH
    ``FileOrchestrationJournalRepository`` and ``FileJournalRetryService``,
    a retry-service read as well: "the whole public surface" is two classes.
    """

    _populate_narrowing_journal(tmp_path)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    target = _dt("2026-06-28T00:00:00Z")
    journal_module.reset_journal_read_counters()

    assert repository.has_active_orchestration(source_id="gfs", cycle_time=target) in (True, False)
    assert repository.has_active_pipeline(source_id="gfs", cycle_time=target, model_id="model_a") in (True, False)
    assert repository.has_completed_pipeline(source_id="gfs", cycle_time=target, model_id="model_a") in (True, False)
    assert repository.get_pipeline_job("job_cycle_gfs_2026062800_convert") is not None
    assert repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", target))
    repository.query_inflight_jobs()
    repository.query_reserved_unbound_jobs()
    repository.update_pipeline_job_status("job_cycle_gfs_2026062800_convert", "succeeded")
    # The decorated surface is TWO classes, so driving only the repository
    # cannot see a decorator dropped from the retry service: measured, stripping
    # `_install_public_read_attribution` from `FileJournalRetryService` leaves
    # `without_entrypoint == 0` because the reads fall through to the still
    # wrapped `repository.get_pipeline_job` and are silently misattributed to
    # the inner entrypoint. Round-3 targeting is read off entrypoint identity
    # (D11b), so that granularity is load-bearing and must be pinned here.
    repository.update_pipeline_job_status(
        "job_cycle_gfs_2026062800_convert",
        "failed",
        error_code="SLURM_TIMEOUT",
    )
    retry_service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    assert retry_service.handle_failed_job(repository.get_pipeline_job("job_cycle_gfs_2026062800_convert")) is not None

    attribution = journal_module.journal_read_attribution()
    total, without_entrypoint, without_lane = _attribution_shares(attribution)
    entrypoints = {str(row["tag"]).partition("|")[0] for row in attribution["tags"]}
    assert total > 0
    assert without_entrypoint == 0, [row["tag"] for row in attribution["tags"]]
    assert {
        "has_active_orchestration",
        "get_pipeline_job",
        "query_pipeline_jobs_by_cycle",
        "query_inflight_jobs",
        "update_pipeline_job_status",
        # `FileJournalRetryService` — the second decorated class.
        "handle_failed_job",
    } <= entrypoints, sorted(entrypoints)
    # The lane residual is not a resting place, and it is not a varying one
    # either: measured over 5 consecutive runs against this fixture it is
    # byte-identical (total=100997 no_lane=0 tags=21 tags_dropped=0), a pure
    # function of the fixture bytes and the call list above. So it is pinned
    # exactly, like ``without_entrypoint`` — a 5% tolerance would only hide a
    # newly-laneless reader.
    assert without_lane == 0, [row["tag"] for row in attribution["tags"]]


def test_direct_cycle_read_lanes_separate_flat_from_by_cycle(tmp_path: Path) -> None:
    """#1734 round-1 finding 3: ``direct_flat_scan`` conflated two legs.

    ``_iter_direct_pipeline_job_records_for_cycle`` merges the unpartitioned
    flat directory with the already-partitioned
    ``pipeline-jobs/by-cycle/<source>/<cycle>/`` tree. One tag over the merged
    list graded partitioned bytes against the flat directory's size: measured
    at 33.5% of the lane in this fixture, and roughly two thirds at node-22's
    tree sizes (26 MB by-cycle / 13 MB flat).
    """

    populated = _populate_narrowing_journal(tmp_path)
    target = _dt("2026-06-28T00:00:00Z")
    source_segment = journal_module._normalize_file_source_id("gfs", field="source_id")
    relocated = populated.root / "pipeline-jobs" / "job_fcst_gfs_2026062800_model_a.json"
    assert relocated.exists()
    destination = (
        populated.root
        / "pipeline-jobs"
        / "by-cycle"
        / source_segment
        / format_cycle_time(target)
        / relocated.name
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    relocated.rename(destination)

    repository = FileOrchestrationJournalRepository(populated.root)
    journal_module.reset_journal_read_counters()
    jobs = repository._direct_pipeline_job_records_for_cycle_cached(source_id="gfs", cycle_time=target)
    assert "job_fcst_gfs_2026062800_model_a" in {str(job["job_id"]) for job in jobs}

    lanes = {
        str(row["tag"]).partition("|")[2]: int(row["bytes"])
        for row in journal_module.journal_read_attribution()["tags"]
    }
    assert lanes.get("direct_flat_scan", 0) > 0, lanes
    assert lanes.get("direct_by_cycle_scan", 0) > 0, lanes


def test_journal_read_counter_is_thread_safe_under_concurrent_readers(tmp_path: Path) -> None:
    """#1734 D11 + spec pipeline-job-persistence:550 — accurate, not self-consistent.

    Round 1 measured why the oracle had to change: the previous assertion was
    ``totals == sum(tags)``, and ``journal_read_attribution`` builds ``totals``
    by summing the very rows it returns as ``tags``, so the equality holds for
    ANY counter content. With ``_record_journal_read`` replaced by a non-atomic
    read-modify-write it measured ``TRUE_CALLS=40 COUNTED=37 LOST=3`` — 7.5% of
    reads genuinely lost — and passed anyway.

    The oracle here is independent of the counter: every thread owns its own
    integer, incremented at ``_read_bytes_limited_cached`` (which performs
    exactly one accounting call per successful return, cached or not) and read
    only after every thread has joined. Nothing is shared between the two
    tallies, so a lost update on one side cannot be mirrored on the other.
    """

    _populate_narrowing_journal(tmp_path)
    # A FRESH instance over the populated root: cold caches, so the workers do
    # real reads rather than serving everything from the writer's memo.
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    cycle_ids = [cycle_id_for(source_id, _dt(text)) for source_id, text in _NARROWING_CYCLES]
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)
    # Key per thread ident: each thread writes only its own key, so this tally
    # cannot itself lose an update the way an unlocked shared counter would.
    observed: dict[int, int] = {}
    real_read = FileOrchestrationJournalRepository._read_bytes_limited_cached

    def counting(self: Any, path: Path) -> Any:
        result = real_read(self, path)
        ident = threading.get_ident()
        observed[ident] = observed.get(ident, 0) + 1
        return result

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=30)
            for _ in range(10):
                repository.query_pipeline_jobs_by_cycle(cycle_ids[index % len(cycle_ids)])
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FileOrchestrationJournalRepository, "_read_bytes_limited_cached", counting)
        journal_module.reset_journal_read_counters()
        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        attribution = journal_module.journal_read_attribution()

    assert not errors, errors
    assert not [thread for thread in threads if thread.is_alive()]

    expected_calls = sum(observed.values())
    assert expected_calls > 0, "the workers must actually reach the read primitive"
    assert len(observed) > 1, "the reads must be spread across threads for this to prove anything"
    counted = attribution["totals"]["calls"] + attribution["totals"]["cache_hit_calls"]
    assert counted == expected_calls, (
        f"counter recorded {counted} reads, threads performed {expected_calls}: "
        f"per-thread {sorted(observed.values())}"
    )


def test_scheduler_pass_evidence_carries_the_journal_read_attribution() -> None:
    """#1734 D11: the counter's totals reach the pass artifact.

    ``_finalize_timing_into_evidence`` is documented as running at every
    ``SchedulerPassResult`` return site BEFORE the evidence is written, so it
    is the one hook that makes the attribution always-on.
    """

    from services.orchestrator import scheduler_runtime
    from services.orchestrator.scheduler_timing import SchedulerPassTiming

    journal_module.reset_journal_read_counters()
    journal_module._record_journal_read(byte_count=1234, cached=False)

    evidence: dict[str, Any] = {}
    collector = SchedulerPassTiming(pass_id="scheduler_2026062800_deadbeefcafe", level="stage")
    with collector.pass_span():
        scheduler_runtime._finalize_timing_into_evidence(evidence, collector, "completed")

    assert "timing" in evidence
    attribution = evidence["journal_read_attribution"]
    assert attribution["schema_version"] == journal_module.JOURNAL_READ_ATTRIBUTION_SCHEMA_VERSION
    assert attribution["totals"]["bytes"] == 1234
    assert [row["tag"] for row in attribution["tags"]] == ["unattributed|unattributed"]
    journal_module.reset_journal_read_counters()


def _released_identity_blocked_master(
    tmp_path: Path, *, member_count: int = 2
) -> tuple[Any, dict[str, Any]]:
    """The production wedge: reserved -> identity-blocked release (#1748)."""

    repository, record = _reserved_cohort_master(tmp_path, member_count=member_count)
    assert repository.reserve_pipeline_job(dict(record)) is not None
    reserved = repository.get_pipeline_job(str(record["job_id"]))
    assert (
        repository.release_identity_blocked_reservation(
            str(record["job_id"]),
            accepted_submit_contract_version=reserved["accepted_submit_contract_version"],
            expected_submission_attempt=int(reserved["submission_attempt"]),
            expected_submission_attempt_started_at=reserved["submission_attempt_started_at"],
            identity_blocked_streak=3,
        )
        == 1
    )
    return repository, record


def _attested_released_master(
    tmp_path: Path, *, member_count: int = 2
) -> tuple[Any, dict[str, Any]]:
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    repository, record = _released_identity_blocked_master(tmp_path, member_count=member_count)
    released = repository.get_pipeline_job(str(record["job_id"]))
    attested = repository.recover_released_identity_blocked_reservation(
        str(record["job_id"]),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=int(released["submission_attempt"]),
        expected_submission_attempt_started_at=released["submission_attempt_started_at"],
    )
    assert attested is not None
    return repository, record


def _forecast_cycle_context(orchestrator: Any, cycle_time: datetime, member_count: int) -> Any:
    from services.orchestrator.chain import CycleOrchestrationContext

    # Deliberately NOT ``tests.test_orchestration_chain._basins``: that fixture
    # pins ``run_id="run_<n>"``, which only suits ``FakeCycleRepository``. The
    # file journal derives the canonical ``fcst_<source>_<cycle>_<model>`` run id
    # and rejects a divergent one with ``file_journal_run_mismatch``.
    raw_basins = [
        {
            "model_id": f"model_{index}",
            "basin_id": f"basin_{index}",
            "basin_version_id": f"basin_v{index}",
            "river_network_version_id": f"river_v{index}",
        }
        for index in range(member_count)
    ]
    basins = orchestrator._normalize_cycle_basins(raw_basins, "gfs", cycle_time)
    return CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=cycle_time,
        cycle_id="gfs_2026072000",
        run_id="cycle_gfs_2026072000",
        all_basins=basins,
        active_basins=list(basins),
    )


def test_operator_attested_release_lets_an_ordinary_pass_submit(tmp_path: Path) -> None:
    """#1748 tasks 5.1 -- THE decisive oracle: liveness, end to end.

    The first implementation of this change pre-materialized the successor row
    and passed 2377 tests while being permanently INERT, because no test drove an
    ordinary pass to an actual submission after recovery.  This is that test.

    It walks the real stage-loop branch at
    ``chain_forecast_execution._run_cycle_chain`` (`:172-206`): find the stage's
    existing job, ask whether it needs submission, ask
    ``_terminal_stage_needs_manual_retry``, then mint the retry identity and call
    the real ``_submit_and_wait_cycle_stage``.  The reservation for the minted
    identity must be NEWLY CREATED -- if it were refused as already in flight the
    pass would take ``_skip_duplicate_submission`` and never reach sbatch.
    """

    from services.orchestrator.chain import M3_STAGES
    from tests.test_orchestration_chain import FakeCycleSlurmClient, _orchestrator

    repository, record = _attested_released_master(tmp_path, member_count=3)
    job_id = str(record["job_id"])
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    cycle_time = _dt("2026-07-20T00:00:00Z")
    # Deliberately a DIFFERENT cohort than the released row's: the production
    # motivation is a manifest that went from 17 basins to 24 while the wedged
    # row froze at the old set.  Driving the pass with 2 basins against a
    # 3-member released row is what makes the "no member-set carry-forward"
    # assertion below discriminating -- with a carry-forward it would be 3.
    context = _forecast_cycle_context(orchestrator, cycle_time, member_count=2)
    stage = M3_STAGES[2]
    assert stage.stage == "forecast"

    existing_jobs = orchestrator._query_pipeline_jobs_for_cycle_context(context)
    existing_job = orchestrator._find_existing_stage_job(existing_jobs, stage, context=context)
    assert existing_job is not None
    assert existing_job["job_id"] == job_id
    assert existing_job["status"] == "reservation_lost"
    assert orchestrator._job_needs_submission(existing_job) is False

    # The additive disjunct: without it this is False and the stage resumes the
    # dead row forever.
    assert orchestrator._terminal_stage_needs_manual_retry(context, existing_job) is True

    pipeline_job_id = orchestrator._retry_cycle_stage_job_id(context, stage, existing_job)
    assert pipeline_job_id == f"{job_id}_retry_1"

    orchestrator._submit_and_wait_cycle_stage(stage, context, pipeline_job_id=pipeline_job_id)

    # Reached the stage's submission call -- not skipped as a duplicate.
    assert len(client.submissions) == 1
    successor = repository.get_pipeline_job(pipeline_job_id)
    assert successor is not None
    assert successor["submit_outcome"] == "accepted"
    assert successor["slurm_job_id"] not in (None, "")
    # The recovered attempt is built from the THEN-CURRENT cohort by the ordinary
    # reservation path (``_reserve_cycle_stage``), not carried over from the
    # released row.  The member set is the discriminating evidence: the digest
    # alone would differ anyway, because it hashes the retry-suffixed
    # job_id/key/comment.
    released_row = repository.get_pipeline_job(job_id)
    assert [member["model_id"] for member in released_row["cohort_members"]] == [
        "model_0",
        "model_1",
        "model_2",
    ]
    assert [member["model_id"] for member in successor["cohort_members"]] == ["model_0", "model_1"]
    assert successor["cohort_digest"] != released_row["cohort_digest"]
    assert successor.get("operator_recovery_attested_at") in (None, "")


def test_released_row_without_the_attestation_still_blocks_the_stage(tmp_path: Path) -> None:
    """#1748 tasks 5.3 -- the disjunct is additive: no attestation, no change.

    Same walk as the decisive test, on a released row that was never attested.
    ``_terminal_stage_needs_manual_retry`` must stay false (the reconcile door
    predicate is unchanged and still demands ``absence_retry_permitted``), so the
    stage resumes the dead row and nothing is submitted.
    """

    from services.orchestrator.chain import M3_STAGES
    from tests.test_orchestration_chain import FakeCycleSlurmClient, _orchestrator

    repository, record = _released_identity_blocked_master(tmp_path)
    job_id = str(record["job_id"])
    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    context = _forecast_cycle_context(orchestrator, _dt("2026-07-20T00:00:00Z"), member_count=3)
    stage = M3_STAGES[2]

    existing_jobs = orchestrator._query_pipeline_jobs_for_cycle_context(context)
    existing_job = orchestrator._find_existing_stage_job(existing_jobs, stage, context=context)
    assert existing_job is not None and existing_job["job_id"] == job_id

    assert orchestrator._terminal_stage_needs_manual_retry(context, existing_job) is False
    assert client.submissions == []
    assert repository.get_pipeline_job(f"{job_id}_retry_1") is None


def test_operator_recovery_attestation_predicate_refuses_non_released_shapes(tmp_path: Path) -> None:
    """#1748 -- the predicate must implement its own narrowing, not lean on the caller.

    The sole call site already gates on ``status``, so none of these shapes is
    reachable today. The point is that the predicate has to be safe to read --
    and to reuse -- in isolation: a ``pending`` successor that somehow inherited
    both the attestation and the released decision is exactly the self-attesting
    shape #1804 describes on the write side, and this closes it at the consumer
    regardless of how the write side evolves.
    """

    from services.orchestrator.chain_forecast_orchestrator_cycle import _operator_recovery_attested

    repository, record = _attested_released_master(tmp_path)
    attested = repository.get_pipeline_job(str(record["job_id"]))
    assert _operator_recovery_attested(attested) is True

    # The self-attesting successor: attestation + released decision, but the row
    # is an ordinary pending one.
    assert _operator_recovery_attested({**attested, "status": "pending"}) is False
    assert _operator_recovery_attested({**attested, "status": None}) is False
    assert _operator_recovery_attested({**attested, "slurm_job_id": "12345"}) is False


def test_operator_recovery_attestation_predicate_refuses_an_empty_string_binding(
    tmp_path: Path,
) -> None:
    """#1748 -- both bindings mean UNBOUND the same (permissive) way.

    The recovery API's own admission guard
    (``recover_released_identity_blocked_reservation``) treats
    ``slurm_job_id`` and ``matched_slurm_job_id`` identically, admitting
    ``(None, "")`` for both. The consumer used a strict ``is None`` on the
    second one, so a row the API would happily attest could then be refused at
    the door -- an attested-but-inert shape. No writer produces ``""`` today;
    the two sides must simply not be able to drift apart.
    """

    from services.orchestrator.chain_forecast_orchestrator_cycle import _operator_recovery_attested

    repository, record = _attested_released_master(tmp_path)
    attested = repository.get_pipeline_job(str(record["job_id"]))

    assert _operator_recovery_attested({**attested, "slurm_job_id": ""}) is True
    assert _operator_recovery_attested({**attested, "matched_slurm_job_id": ""}) is True
    # A real binding is still refused on both fields.
    assert _operator_recovery_attested({**attested, "matched_slurm_job_id": "12345"}) is False
    assert _operator_recovery_attested({**attested, "slurm_job_id": "12345"}) is False


def test_recover_released_identity_blocked_reservation_writes_no_row(tmp_path: Path) -> None:
    """#1748 tasks 5.2 -- the attestation is a marker, never a successor row.

    Pre-materializing the successor occupies the exact ``job_id`` and
    idempotency key the ordinary retry path would mint, and the ordinary path
    refuses to submit a row it did not itself reserve -- so the eager write
    consumes the only submittable identity.  The `_retry_<n>` slot must stay free.
    """

    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    repository, record = _released_identity_blocked_master(tmp_path)
    job_id = str(record["job_id"])
    released = repository.get_pipeline_job(job_id)
    jobs_before = {str(job["job_id"]) for job in repository.query_pipeline_jobs_by_cycle(str(record["cycle_id"]))}

    attested = repository.recover_released_identity_blocked_reservation(
        job_id,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=int(released["submission_attempt"]),
        expected_submission_attempt_started_at=released["submission_attempt_started_at"],
    )

    assert attested is not None
    assert attested["job_id"] == job_id
    assert attested["operator_recovery_attested_at"] not in (None, "")

    persisted = repository.get_pipeline_job(job_id)
    assert persisted["operator_recovery_attested_at"] == attested["operator_recovery_attested_at"]
    # Nothing about the released terminal itself moved.
    assert persisted["status"] == "reservation_lost"
    assert persisted["reconciliation_decision"] == "identity_mismatch_released"
    assert persisted.get("error_code") in (None, "")
    assert persisted["submission_attempt"] == released["submission_attempt"]

    # No successor row, and the identity the ordinary path will mint is free.
    jobs_after = {str(job["job_id"]) for job in repository.query_pipeline_jobs_by_cycle(str(record["cycle_id"]))}
    assert jobs_after == jobs_before
    assert repository.get_pipeline_job(f"{job_id}_retry_1") is None
    assert repository.query_candidate_state(f"{record['idempotency_key']}:retry_1") is None


def test_recover_released_identity_blocked_reservation_repeat_is_idempotent(tmp_path: Path) -> None:
    """#1748 tasks 2.4/5.4 -- a second attestation leaves the row exactly as it was."""

    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    repository, record = _released_identity_blocked_master(tmp_path)
    job_id = str(record["job_id"])
    released = repository.get_pipeline_job(job_id)
    kwargs = {
        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
        "expected_submission_attempt": int(released["submission_attempt"]),
        "expected_submission_attempt_started_at": released["submission_attempt_started_at"],
    }

    first = repository.recover_released_identity_blocked_reservation(job_id, **kwargs)
    assert first is not None
    after_first = repository.get_pipeline_job(job_id)
    events_after_first = _master_events(repository, record)

    second = repository.recover_released_identity_blocked_reservation(job_id, **kwargs)

    assert second is not None
    assert second["operator_recovery_attested_at"] == first["operator_recovery_attested_at"]
    assert repository.get_pipeline_job(job_id) == after_first
    assert _master_events(repository, record) == events_after_first
    assert repository.get_pipeline_job(f"{job_id}_retry_1") is None


def test_recover_released_identity_blocked_reservation_refuses_cas_mismatch(tmp_path: Path) -> None:
    """#1748 -- a concurrently advanced attempt loses the race, write-free."""

    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    repository, record = _released_identity_blocked_master(tmp_path)
    job_id = str(record["job_id"])
    released = repository.get_pipeline_job(job_id)
    before = dict(released)

    assert (
        repository.recover_released_identity_blocked_reservation(
            job_id,
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=int(released["submission_attempt"]) + 1,
            expected_submission_attempt_started_at=released["submission_attempt_started_at"],
        )
        is None
    )
    assert (
        repository.recover_released_identity_blocked_reservation(
            job_id,
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=int(released["submission_attempt"]),
            expected_submission_attempt_started_at=_dt("2001-01-01T00:00:00Z"),
        )
        is None
    )
    assert (
        repository.recover_released_identity_blocked_reservation(
            job_id,
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=int(released["submission_attempt"]),
            expected_submission_attempt_started_at=None,
        )
        is None
    )

    assert repository.get_pipeline_job(job_id) == before
    assert before.get("operator_recovery_attested_at") in (None, "")


def test_recover_released_identity_blocked_reservation_refuses_unowned_shapes(tmp_path: Path) -> None:
    """#1748 tasks 2.3/5.4 -- every shape outside the released terminal is refused."""

    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    # (a) a plain reserved row -- never released.
    repository, record = _reserved_cohort_master(tmp_path / "reserved", member_count=2)
    assert repository.reserve_pipeline_job(dict(record)) is not None
    reserved = repository.get_pipeline_job(str(record["job_id"]))
    assert (
        repository.recover_released_identity_blocked_reservation(
            str(record["job_id"]),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=int(reserved["submission_attempt"]),
            expected_submission_attempt_started_at=reserved["submission_attempt_started_at"],
        )
        is None
    )
    assert repository.get_pipeline_job(str(record["job_id"])) == reserved

    # (b) the OTHER released decision -- ``absence_retry_permitted`` owns the
    # reclaim door and must not be attested through this one.
    permitted_repo, permitted_record = _reserved_cohort_master(tmp_path / "permitted", member_count=2)
    assert permitted_repo.reserve_pipeline_job(dict(permitted_record)) is not None
    permitted_reserved = permitted_repo.get_pipeline_job(str(permitted_record["job_id"]))
    assert (
        permitted_repo.permit_pipeline_job_retry(
            str(permitted_record["job_id"]),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=int(permitted_reserved["submission_attempt"]),
            expected_submission_attempt_started_at=permitted_reserved["submission_attempt_started_at"],
        )
        == 1
    )
    permitted = permitted_repo.get_pipeline_job(str(permitted_record["job_id"]))
    assert permitted["reconciliation_decision"] == "absence_retry_permitted"
    assert (
        permitted_repo.recover_released_identity_blocked_reservation(
            str(permitted_record["job_id"]),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=int(permitted["submission_attempt"]),
            expected_submission_attempt_started_at=permitted["submission_attempt_started_at"],
        )
        is None
    )
    assert permitted_repo.get_pipeline_job(str(permitted_record["job_id"])) == permitted

    # (c) a bound row -- a live Slurm identity is never recoverable this way.
    bound_repo, bound_record = _reserved_cohort_master(tmp_path / "bound", member_count=2)
    assert bound_repo.reserve_pipeline_job(dict(bound_record)) is not None
    bound_reserved = bound_repo.get_pipeline_job(str(bound_record["job_id"]))
    _bind_and_project_cohort(bound_repo, bound_record, member_count=2)
    bound = bound_repo.get_pipeline_job(str(bound_record["job_id"]))
    assert bound["slurm_job_id"] == "17667"
    assert (
        bound_repo.recover_released_identity_blocked_reservation(
            str(bound_record["job_id"]),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=int(bound_reserved["submission_attempt"]),
            expected_submission_attempt_started_at=bound_reserved["submission_attempt_started_at"],
        )
        is None
    )
    assert bound_repo.get_pipeline_job(str(bound_record["job_id"])) == bound

    # (d) an unknown job id.
    assert (
        repository.recover_released_identity_blocked_reservation(
            "job_cycle_gfs_2026072000_forecast_missing",
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_submission_attempt_started_at=reserved["submission_attempt_started_at"],
        )
        is None
    )

    # (e) a stale accepted-submit contract version raises rather than attests.
    released_repo, released_record = _released_identity_blocked_master(tmp_path / "released")
    released_row = released_repo.get_pipeline_job(str(released_record["job_id"]))
    with pytest.raises(journal_module.FileOrchestrationJournalError):
        released_repo.recover_released_identity_blocked_reservation(
            str(released_record["job_id"]),
            accepted_submit_contract_version="v0-not-current",
            expected_submission_attempt=int(released_row["submission_attempt"]),
            expected_submission_attempt_started_at=released_row["submission_attempt_started_at"],
        )
    assert released_repo.get_pipeline_job(str(released_record["job_id"])) == released_row


def test_operator_recovery_attestation_cannot_be_set_by_an_ordinary_upsert(tmp_path: Path) -> None:
    """#1748 tasks 2.5 -- no automatic path can set or clear the attestation.

    ``upsert_pipeline_job`` merges only ``_PIPELINE_JOB_UPSERT_MUTABLE_FIELDS``
    (`file_orchestration_journal.py:373-420`), and the attestation is
    deliberately NOT in that whitelist, so a caller can neither forge it on a
    candidate row nor strip it from an attested master.
    """

    repository, record = _attested_released_master(tmp_path)
    job_id = str(record["job_id"])
    attested_at = repository.get_pipeline_job(job_id)["operator_recovery_attested_at"]
    assert attested_at not in (None, "")

    # Forging it through the generic upsert is refused outright.
    with pytest.raises(journal_module.FileOrchestrationJournalError):
        repository.upsert_pipeline_job(
            {
                "job_id": "job_fcst_gfs_2026072000_model_0_forecast",
                "run_id": "fcst_gfs_2026072000_model_0",
                "cycle_id": "gfs_2026072000",
                "job_type": "run_shud_forecast",
                "stage": "forecast",
                "model_id": "model_0",
                "status": "failed",
                "operator_recovery_attested_at": "2026-07-20T05:00:00Z",
            }
        )
    assert repository.get_pipeline_job("job_fcst_gfs_2026072000_model_0_forecast") is None

    # And an ordinary upsert on the attested master cannot STRIP it, because the
    # field is outside ``_PIPELINE_JOB_UPSERT_MUTABLE_FIELDS``.
    repository.upsert_pipeline_job(
        {
            "job_id": job_id,
            "run_id": str(record["run_id"]),
            "cycle_id": str(record["cycle_id"]),
            "job_type": str(record["job_type"]),
            "stage": "forecast",
            "status": "reservation_lost",
            "operator_recovery_attested_at": None,
        }
    )
    assert repository.get_pipeline_job(job_id)["operator_recovery_attested_at"] == attested_at

    # And a clean reservation may not carry one in.
    fresh_repo, fresh_record = _reserved_cohort_master(tmp_path / "fresh", member_count=2)
    with pytest.raises(journal_module.FileOrchestrationJournalError):
        fresh_repo.reserve_pipeline_job(
            {**fresh_record, "operator_recovery_attested_at": "2026-07-20T05:00:00Z"}
        )
    assert fresh_repo.get_pipeline_job(str(fresh_record["job_id"])) is None


_RECOVER_COMMAND = "recover-released-identity-blocked-reservation"


def _run_recovery_cli(entrypoint: str, argv: list[str], capsys: Any) -> tuple[int, Any]:
    """Drive the operator subcommand through BOTH shipped entrypoints.

    ``_click_main`` runs with ``standalone_mode=False`` when argv is supplied, so
    a command that exits non-zero raises ``SystemExit`` out of it instead of
    returning -- the same normalization the file-journal rollback CLI tests use.
    """

    from services.orchestrator import cli as cli_module

    full = [_RECOVER_COMMAND, *argv]
    if entrypoint == "click":
        try:
            code = cli_module._click_main(full)
        except SystemExit as error:
            code = int(error.code or 0)
    else:
        code = cli_module._argparse_main(full)
    captured = capsys.readouterr().out
    return code, (json.loads(captured) if captured.strip() else None)


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_recovery_cli_attests_a_wedged_row_and_is_idempotent(
    tmp_path: Path, capsys: Any, entrypoint: str
) -> None:
    """#1748 -- the operator-facing channel: attest, then attest again.

    A typed method with no invocation surface is not a recovery channel; an
    operator on node-22 has a shell, not a Python REPL.  Second invocation must
    report ``already_attested`` and leave the row byte-identical.
    """

    base = tmp_path / entrypoint
    repository, record = _released_identity_blocked_master(base)
    job_id = str(record["job_id"])
    released = repository.get_pipeline_job(job_id)
    assert released.get("operator_recovery_attested_at") in (None, "")

    # Half 1 -- FIND. The signal names an API whose CAS arguments a human has
    # no other supported way to read; without this the operator is stuck one
    # step before the recovery.
    list_code, listing = _run_recovery_cli(entrypoint, ["--journal-root", str(repository.root)], capsys)
    assert list_code == 0
    assert listing["decision"] == "listed"
    assert listing["wedged_count"] == 1
    wedged = listing["wedged"][0]
    assert wedged["job_id"] == job_id
    assert wedged["cohort_digest"] == released["cohort_digest"]
    assert wedged["identity_blocked_streak"] == 3
    assert wedged["expected_submission_attempt"] == released["submission_attempt"]
    assert wedged["expected_submission_attempt_started_at"]
    assert wedged["operator_recovery_attested_at"] is None

    # Dry run by default: inspect, write nothing.
    dry_code, dry = _run_recovery_cli(
        entrypoint, ["--journal-root", str(repository.root), "--job-id", job_id], capsys
    )
    assert dry_code == 0
    assert dry["decision"] == "eligible"
    assert repository.get_pipeline_job(job_id).get("operator_recovery_attested_at") in (None, "")

    # Half 2 -- ACT.
    code, payload = _run_recovery_cli(
        entrypoint,
        ["--journal-root", str(repository.root), "--job-id", job_id, "--attest"],
        capsys,
    )

    assert code == 0
    assert payload["decision"] == "attested"
    assert payload["slurm_liveness_checked"] is False
    assert payload["job_id"] == job_id
    # The CAS values are read off the row and echoed for operator confirmation.
    assert payload["expected_submission_attempt"] == released["submission_attempt"]
    assert payload["expected_submission_attempt_started_at"]
    assert payload["operator_recovery_attested_at"]
    attested = repository.get_pipeline_job(job_id)
    assert attested["operator_recovery_attested_at"] == payload["operator_recovery_attested_at"]

    repeat_code, repeat_payload = _run_recovery_cli(
        entrypoint,
        ["--journal-root", str(repository.root), "--job-id", job_id, "--attest"],
        capsys,
    )

    assert repeat_code == 0
    assert repeat_payload["decision"] == "already_attested"
    assert repository.get_pipeline_job(job_id) == attested

    # An attested row leaves the wedge listing: it is no longer awaiting an
    # operator.
    _, after = _run_recovery_cli(entrypoint, ["--journal-root", str(repository.root)], capsys)
    assert after["wedged"][0]["operator_recovery_attested_at"] == payload["operator_recovery_attested_at"]

def _pad_journal_cycles_with_unrelated_records(
    journal_root: Path, *, cycles: tuple[datetime, ...], records_per_cycle: int
) -> int:
    """Grow retained history in OTHER cycles, the way production actually grew.

    ``journal/`` is append-only, so the whole-tree replay's aggregate budget is
    a function of history, not of the cycle under inspection (#1810).  Padding
    with unrelated cycles reproduces that growth law in a fixture.
    """

    for cycle_time in cycles:
        _write_jsonl(
            journal_root / f"journal/gfs/{format_cycle_time(cycle_time)}.jsonl",
            [
                _journal_record(
                    record_type="pipeline_job",
                    source_id="gfs",
                    cycle_time=cycle_time,
                    payload=_active_job(cycle_time),
                    sequence=sequence,
                )
                for sequence in range(1, records_per_cycle + 1)
            ],
        )
    return len(cycles) * records_per_cycle


def test_released_identity_blocked_listing_survives_a_whole_tree_budget(tmp_path: Path) -> None:
    """#1810 task 1.1: discovery is bounded by ONE cycle, not by history.

    The shipped query replayed the whole tree, so its aggregate
    ``_RecordBudget`` counted every retained ``journal/`` line in every cycle.
    On node-22 that is 71,213 records against a 100,000 cap that history had
    already crossed, and the operator-facing FIND half returned
    ``file_journal_record_limit_exceeded`` instead of the wedge.

    The oracle shape is deliberate (design D7).  ``max_records=1`` would redden
    the FIXED code too -- the cycle-scoped replay consumes the same budget per
    call -- and could only be "fixed" by exempting the scoped path, which is an
    oracle weakening.  The discriminating shape is N cycles x M records with
    M < max_records < N*M: three cycles of four records each under a budget of
    six.  The two ``query_pipeline_jobs_by_cycle`` probes below assert that
    shape in-test, so the pin cannot silently degrade into a tautology if the
    fixtures grow a record.
    """

    repository, record = _released_identity_blocked_master(tmp_path)
    job_id = str(record["job_id"])
    released_cycle_time = _dt("2026-07-20T00:00:00Z")
    _pad_journal_cycles_with_unrelated_records(
        repository.root,
        cycles=(_dt("2026-07-20T12:00:00Z"), _dt("2026-07-21T00:00:00Z")),
        records_per_cycle=4,
    )

    # The released cycle costs 3 journal lines + 1 flat direct record = 4.
    budgeted = FileOrchestrationJournalRepository(repository.root, max_records=6)

    # Oracle self-check, half 1: the whole tree is OVER the budget. An
    # underivable cycle id falls open to the full scan (#1734 D4), which is the
    # public way to reach it.
    assert journal_module._cycle_scope_from_cycle_id("unknown-source_2026072000") is None
    blocked = budgeted.query_pipeline_jobs_by_cycle("unknown-source_2026072000")
    assert blocked[0]["error_code"] == "file_journal_record_limit_exceeded"
    assert blocked[0]["file_journal"]["field"] == "pipeline_job_records"

    # Oracle self-check, half 2: one cycle is UNDER it.
    narrowed = budgeted.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", released_cycle_time))
    assert [job["job_id"] for job in narrowed] == [job_id]
    # A released row carries its own ``error_code`` (SLURM_RESERVATION_LOST), so
    # the blocked-read marker is ``file_journal``, not the presence of a code.
    assert "file_journal" not in narrowed[0]

    # The pin. Not "no exception escaped" -- a silently empty listing would
    # satisfy that and would be the fail-open this change exists to avoid.
    listed = budgeted.query_released_identity_blocked_jobs()

    assert [job["job_id"] for job in listed] == [job_id]
    assert listed[0]["identity_blocked_streak"] == 3
    assert listed[0]["reconciliation_decision"] == "identity_mismatch_released"


def test_released_identity_blocked_scope_is_derived_from_row_content(tmp_path: Path) -> None:
    """#1810 task 1.3: the scope is the WRITER's, read back off row content.

    The fixture asked for an end-to-end pin: a flat ``pipeline-jobs/`` row whose
    job_id does not match ``_ACCEPTED_SUBMIT_MASTER_JOB_ID_RE`` but whose content
    is a current-contract master.  **That row cannot exist**, and this test pins
    why rather than asserting the fixture's premise.  A current-contract master
    must satisfy ``forecast_cohort_identity_is_valid``, which forces
    ``run_id == cycle_{source}_{cycle}[_cohort]`` and
    ``job_id == job_{run_id}_forecast[_suffix]``; no storage source id contains
    ``_``; so every such job_id necessarily matches that regex.  The flat reader
    validates every file it yields, so a synthetic counter-example is REJECTED by
    ``_validated_direct_pipeline_job_record`` instead of being discovered.

    What is therefore pinned is the property the fixture wanted: discovery never
    consults the job_id string at all.  ``_released_candidate_cycle_scope`` reads
    the same two content fields ``_write_pipeline_job_direct_unlocked`` and
    ``_write_pipeline_job_unlocked`` used to place the row, so reader and writer
    agree by construction and an unparsable name is a non-event.
    """

    from services.orchestrator.accepted_submit_identity import forecast_cohort_digest

    repository, record = _released_identity_blocked_master(tmp_path)
    parsable_job_id = str(record["job_id"])
    released_record = json.loads(
        (repository.root / "pipeline-jobs" / f"{parsable_job_id}.json").read_text(encoding="utf-8")
    )
    released_row = released_record["payload"]
    assert journal_module._released_identity_blocked_row(released_row)

    residue_job_id = "legacy-master.cycle_gfs_2026072000_forecast"
    assert journal_module._ACCEPTED_SUBMIT_MASTER_JOB_ID_RE.fullmatch(residue_job_id) is None
    assert journal_module._cycle_scope_from_job_id(residue_job_id) is None
    residue_row = {**released_row, "job_id": residue_job_id}
    residue_row["cohort_digest"] = forecast_cohort_digest(residue_row)

    # The property: an unparsable name still yields the writer's exact scope.
    scope = journal_module._released_candidate_cycle_scope(residue_row)
    assert scope == (
        journal_module._source_id_from_job(released_row),
        journal_module._cycle_time_from_job(released_row),
    )
    assert scope == ("gfs", _dt("2026-07-20T00:00:00Z"))

    # And the reason the end-to-end shape is unconstructible, asserted rather
    # than asserted-about: the flat reader refuses the row.
    with pytest.raises(FileOrchestrationJournalError) as rejected:
        repository._validated_direct_pipeline_job_record(
            {**released_record, "job_id": residue_job_id, "payload": residue_row},
            expected_job_id=residue_job_id,
        )
    assert rejected.value.reason == "file_journal_evidence_invariant_invalid"
    assert rejected.value.field == "cohort_digest"


def test_released_identity_blocked_listing_falls_open_when_scope_is_underivable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1810 task 1.4: an underivable scope widens the read, never shortens it.

    #1734's D4 contract is that an underivable key costs the OLD expensive read
    and never a false "not found".  Like task 1.3's shape, this precondition is
    unreachable from data -- every row the flat reader yields carries a
    round-tripping ``cycle_id``, so ``_released_candidate_cycle_scope`` cannot
    return ``None`` -- so the precondition is injected at that one seam.  The
    branch would otherwise be unpinned and freely regressable, and deleting it
    would be a silent deviation from the spec requirement.

    Read attribution is the oracle, not just the returned row: a fall-open that
    quietly dropped the candidate would also return nothing, and a fall-open
    that never happened would tag no ``full_tree_replay``.
    """

    repository, record = _released_identity_blocked_master(tmp_path)
    job_id = str(record["job_id"])
    monkeypatch.setattr(journal_module, "_released_candidate_cycle_scope", lambda job: None)
    journal_module.reset_journal_read_counters()

    listed = repository.query_released_identity_blocked_jobs()

    assert [job["job_id"] for job in listed] == [job_id]
    tags = {str(row["tag"]) for row in journal_module.journal_read_attribution()["tags"]}
    assert "query_released_identity_blocked_jobs|full_tree_replay" in tags, sorted(tags)


def test_released_identity_blocked_listing_never_replays_the_whole_tree(tmp_path: Path) -> None:
    """#1810 tasks 2.1/2.2: flat scan as candidate filter, cycle-scoped confirm.

    The budget pin above proves the read fits; this proves it is the SHAPE the
    design chose and not an accident of fixture size.  D9 recorded that the
    obvious alternative -- one cycle-scoped replay per distinct cycle -- did not
    finish in 40 minutes against the real node-22 journal, so the lane split is
    load-bearing: enumerate the flat directory ONCE, confirm per candidate.
    """

    repository, record = _released_identity_blocked_master(tmp_path)
    _pad_journal_cycles_with_unrelated_records(
        repository.root,
        cycles=(_dt("2026-07-20T12:00:00Z"), _dt("2026-07-21T00:00:00Z")),
        records_per_cycle=4,
    )
    # A FRESH instance: the writer left every file in its byte cache.
    reader = FileOrchestrationJournalRepository(repository.root)
    journal_module.reset_journal_read_counters()

    assert [job["job_id"] for job in reader.query_released_identity_blocked_jobs()] == [
        str(record["job_id"])
    ]

    tags = {str(row["tag"]) for row in journal_module.journal_read_attribution()["tags"]}
    assert "query_released_identity_blocked_jobs|direct_flat_scan" in tags, sorted(tags)
    assert "query_released_identity_blocked_jobs|cycle_replay" in tags, sorted(tags)
    assert not [tag for tag in tags if tag.endswith("|full_tree_replay")], sorted(tags)

def _released_identity_blocked_wedge(
    tmp_path: Path, *, cycle_times: tuple[datetime, ...], per_cycle: int
) -> tuple[Any, list[str]]:
    """A mass-release wedge: ``per_cycle`` released masters in each cycle.

    Built through the production transitions (reserve -> identity-blocked
    release) in ONE repository, so every row is a real member of the shape
    ``_released_identity_blocked_row`` admits.
    """

    from packages.common.source_identity import normalize_source_id
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        forecast_cohort_digest,
    )
    from services.orchestrator.chain_config import scenario_for_source

    canonical_source_id = normalize_source_id("gfs")
    scenario_id = scenario_for_source(canonical_source_id)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    job_ids: list[str] = []
    for cycle_time in cycle_times:
        stamp = format_cycle_time(cycle_time)
        cycle_iso = cycle_time.astimezone(UTC).isoformat().replace("+00:00", "Z")
        for cohort in range(per_cycle):
            run_id = f"cycle_{canonical_source_id.lower()}_{stamp}_c{cohort}"
            job_id = f"job_{run_id}_forecast"
            record: dict[str, Any] = {
                "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
                "job_id": job_id,
                "run_id": run_id,
                "source_id": canonical_source_id,
                "cycle_id": cycle_id_for(canonical_source_id, cycle_time),
                "job_type": "run_shud_forecast_array",
                "model_id": None,
                "stage": "forecast",
                "idempotency_key": f"{run_id}:forecast",
                "slurm_comment": f"nhms_idem:{run_id}:forecast",
                "submit_outcome": None,
                "restart_stage": "forecast",
                "cohort_members": [
                    {
                        "array_task_id": index,
                        "candidate_id": f"{canonical_source_id}:{cycle_iso}:model_c{cohort}_{index}:{scenario_id}",
                        "run_id": f"fcst_{canonical_source_id.lower()}_{stamp}_model_c{cohort}_{index}",
                        "model_id": f"model_c{cohort}_{index}",
                        "basin_id": f"basin_c{cohort}_{index}",
                        "scenario_id": scenario_id,
                        "restart_stage": "forecast",
                    }
                    for index in range(2)
                ],
                "submission_attempt": 1,
                "submission_attempt_started_at": cycle_time,
                "expected_slurm_user": None,
                "expected_slurm_account": None,
                "slurm_ownership_required": False,
                "created_at": cycle_time,
                "updated_at": cycle_time,
            }
            record["cohort_digest"] = forecast_cohort_digest(record)
            assert repository.reserve_pipeline_job(dict(record)) is not None
            reserved = repository.get_pipeline_job(job_id)
            assert (
                repository.release_identity_blocked_reservation(
                    job_id,
                    accepted_submit_contract_version=reserved["accepted_submit_contract_version"],
                    expected_submission_attempt=int(reserved["submission_attempt"]),
                    expected_submission_attempt_started_at=reserved["submission_attempt_started_at"],
                    identity_blocked_streak=3,
                )
                == 1
            )
            job_ids.append(job_id)
    return repository, job_ids


def test_released_identity_blocked_confirm_does_not_relist_the_flat_directory_per_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1810 task 3.1: the confirm half must not be O(candidates x flat dir).

    The scoped confirm reaches ``_flat_direct_pipeline_job_paths_for_cycle``,
    which lists the ENTIRE unpartitioned ``pipeline-jobs/`` directory (4,557
    files on node-22) and only then filters by file name.  Called once per
    candidate, that is the same O(N x flat-directory) growth law design D9
    measured at ">40 minutes for 230 cycles" and rejected.  N is not bounded by
    anything: the admitted shape (reservation_lost + identity_mismatch_released
    + no slurm id) is exactly what a mass release or a SLURM outage produces in
    bulk, which is the incident this operator command exists to serve.

    The oracle is a direct COUNT of flat-directory listings, not wall time, so
    it is independent of fixture size: after the fix the directory is listed
    exactly twice per query -- once by the candidate scan
    (``_iter_direct_pipeline_job_records``, which does not route through the
    per-cycle helper) and once when the per-query memo fills.  A constant, not
    ``<= M`` or ``<= K``, is what proves the growth law is gone.
    """

    cycle_times = (_dt("2026-07-20T00:00:00Z"), _dt("2026-07-20T12:00:00Z"))
    per_cycle = 3
    repository, job_ids = _released_identity_blocked_wedge(
        tmp_path, cycle_times=cycle_times, per_cycle=per_cycle
    )
    flat_directory = repository.root / "pipeline-jobs"

    # A FRESH instance: the writer left every file in its byte cache.
    reader = FileOrchestrationJournalRepository(repository.root)
    original = journal_module._iter_regular_json_files
    listings: list[Path] = []

    def counting_iter_regular_json_files(directory: Path, **kwargs: Any) -> Any:
        if directory == flat_directory:
            listings.append(directory)
        return original(directory, **kwargs)

    monkeypatch.setattr(journal_module, "_iter_regular_json_files", counting_iter_regular_json_files)

    listed = reader.query_released_identity_blocked_jobs()

    # Half 1: every candidate in every scope is still returned. A memo keyed by
    # the wrong thing would shrink this to one cycle's rows.
    assert [job["job_id"] for job in listed] == sorted(job_ids)
    assert len(job_ids) == len(cycle_times) * per_cycle
    # Half 2: the growth law. Two listings, independent of M and of K.
    assert len(listings) == 2, len(listings)


def test_released_identity_blocked_confirm_replays_once_per_cycle_not_per_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1810 design D14: the confirm half is grouped BY CYCLE, not per candidate.

    The pin above counts flat-directory LISTINGS, and the per-query memo alone
    holds that count at two even if the grouping is reverted to a per-candidate
    loop -- so it does not discriminate the two halves of the fix.  This one
    counts the scoped REPLAYS themselves, which the memo does not deduplicate:
    a per-candidate loop costs one cycle replay per candidate, and a mass
    release puts an unbounded number of rows into a handful of cycles.

    The oracle is a constant times K, with the fixture built so C > K in-test
    (12 candidates over 3 cycles), so the bound cannot silently degrade into a
    tautology.  The recorded scopes are compared as a SET too: a count alone
    would also be satisfied by three replays of the wrong cycle, and a
    ``None`` scope would mean the fall-open whole-tree replay, not grouping.
    """

    cycle_times = (
        _dt("2026-07-20T00:00:00Z"),
        _dt("2026-07-20T12:00:00Z"),
        _dt("2026-07-21T00:00:00Z"),
    )
    per_cycle = 4
    repository, job_ids = _released_identity_blocked_wedge(
        tmp_path, cycle_times=cycle_times, per_cycle=per_cycle
    )

    # A FRESH instance: the writer left every file in its byte cache.
    reader = FileOrchestrationJournalRepository(repository.root)
    original = reader._iter_pipeline_job_records_scoped
    replayed: list[tuple[str, datetime] | None] = []

    def counting_iter_pipeline_job_records_scoped(
        cycle_scope: tuple[str, datetime] | None, **kwargs: Any
    ) -> Any:
        replayed.append(cycle_scope)
        return original(cycle_scope, **kwargs)

    monkeypatch.setattr(
        reader, "_iter_pipeline_job_records_scoped", counting_iter_pipeline_job_records_scoped
    )

    listed = reader.query_released_identity_blocked_jobs()

    # Half 1: every candidate in every scope is still returned, and the fixture
    # really is C > K -- 12 candidates spread over 3 cycles.
    assert [job["job_id"] for job in listed] == sorted(job_ids)
    assert len(job_ids) == len(cycle_times) * per_cycle == 12
    # Half 2: the growth law. One replay per DISTINCT cycle, exactly, and each
    # one for a cycle a candidate actually lives in.
    assert len(replayed) == len(cycle_times), replayed
    assert set(replayed) == {("gfs", cycle_time) for cycle_time in cycle_times}


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_recovery_cli_refusals_name_the_failing_precondition(
    tmp_path: Path, capsys: Any, entrypoint: str
) -> None:
    """#1748 -- a refusal must be diagnosable, not just non-zero.

    This command exists because the wedge itself was undiagnosable; ``None``
    from the typed API tells an operator nothing about WHICH precondition it
    tripped.  The CLI mirrors the API's precondition order and names the one
    that failed.
    """

    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    base = tmp_path / entrypoint
    repository, record = _reserved_cohort_master(base, member_count=2)
    assert repository.reserve_pipeline_job(dict(record)) is not None
    job_id = str(record["job_id"])
    root = str(repository.root)

    # --attest without a target: it acts on exactly one row, never a sweep.
    code, payload = _run_recovery_cli(entrypoint, ["--journal-root", root, "--attest"], capsys)
    assert code == 2
    assert payload["reason"] == "job_id_required_to_attest"

    # Nothing wedged yet -- the listing is empty rather than an error.
    code, payload = _run_recovery_cli(entrypoint, ["--journal-root", root], capsys)
    assert code == 0
    assert payload["wedged"] == []

    # Unparseable job_id: refused before any journal read.
    code, payload = _run_recovery_cli(
        entrypoint, ["--journal-root", root, "--job-id", "nope", "--attest"], capsys
    )
    assert code == 2
    assert payload["decision"] == "refused"
    assert payload["reason"] == "job_id_unparseable"

    # Well-formed but absent.
    code, payload = _run_recovery_cli(
        entrypoint,
        ["--journal-root", root, "--job-id", "job_cycle_gfs_2026072000_publish", "--attest"],
        capsys,
    )
    assert code == 2
    assert payload["reason"] == "job_not_found"

    # Present, but still an ordinary live reservation.
    code, payload = _run_recovery_cli(
        entrypoint, ["--journal-root", root, "--job-id", job_id, "--attest"], capsys
    )
    assert code == 2
    assert payload["reason"] == "status_not_reservation_lost"
    assert payload["observed"]["status"] == "reserved"

    # Released through the OTHER door: reclaim's absence proof, not the identity
    # wedge.  That row has its own automatic path and must not be attested here.
    reserved = repository.get_pipeline_job(job_id)
    assert (
        repository.permit_pipeline_job_retry(
            job_id,
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=int(reserved["submission_attempt"]),
            expected_submission_attempt_started_at=reserved["submission_attempt_started_at"],
        )
        == 1
    )
    code, payload = _run_recovery_cli(
        entrypoint, ["--journal-root", root, "--job-id", job_id, "--attest"], capsys
    )
    assert code == 2
    assert payload["reason"] == "decision_not_identity_mismatch_released"
    # And it is absent from the wedge listing -- a different door owns it.
    _, listing = _run_recovery_cli(entrypoint, ["--journal-root", root], capsys)
    assert listing["wedged"] == []
    assert payload["observed"]["reconciliation_decision"] == "absence_retry_permitted"

    # Every refusal above is write-free.
    assert repository.get_pipeline_job(job_id).get("operator_recovery_attested_at") in (None, "")


@pytest.mark.parametrize("entrypoint", ["click", "argparse"])
def test_recovery_cli_help_says_it_is_an_attestation_not_a_proof(
    tmp_path: Path, capsys: Any, entrypoint: str
) -> None:
    """#1748 -- the non-goal has to be readable at the point of use.

    Both entrypoints ship depending on whether click is importable, so both must
    carry the warning.
    """

    del tmp_path
    from services.orchestrator import cli as cli_module

    argv = [_RECOVER_COMMAND, "--help"]
    if entrypoint == "click":
        try:
            cli_module._click_main(argv)
        except SystemExit:
            pass
    else:
        with pytest.raises(SystemExit):
            cli_module._argparse_main(argv)
    text = capsys.readouterr().out
    assert "attestation" in text.lower()
    assert "liveness" in text.lower() or "does not check" in text.lower()
    assert "slurm" in text.lower()


def test_operator_cli_recovery_lets_an_ordinary_pass_submit(tmp_path: Path, capsys: Any) -> None:
    """#1748 -- the decisive oracle, driven through the OPERATOR-FACING path.

    ``test_operator_attested_release_lets_an_ordinary_pass_submit`` pins the
    typed API.  This sibling pins the channel a human actually has: the CLI
    subcommand must reach a real submission through an ordinary pass.
    """

    from services.orchestrator.chain import M3_STAGES
    from tests.test_orchestration_chain import FakeCycleSlurmClient, _orchestrator

    repository, record = _released_identity_blocked_master(tmp_path, member_count=3)
    job_id = str(record["job_id"])

    code, payload = _run_recovery_cli(
        "argparse",
        ["--journal-root", str(repository.root), "--job-id", job_id, "--attest"],
        capsys,
    )
    assert code == 0 and payload["decision"] == "attested"

    client = FakeCycleSlurmClient()
    orchestrator = _orchestrator(tmp_path, repository, client)
    context = _forecast_cycle_context(orchestrator, _dt("2026-07-20T00:00:00Z"), member_count=2)
    stage = M3_STAGES[2]

    existing_jobs = orchestrator._query_pipeline_jobs_for_cycle_context(context)
    existing_job = orchestrator._find_existing_stage_job(existing_jobs, stage, context=context)
    assert existing_job is not None and existing_job["job_id"] == job_id
    assert orchestrator._job_needs_submission(existing_job) is False
    assert orchestrator._terminal_stage_needs_manual_retry(context, existing_job) is True

    pipeline_job_id = orchestrator._retry_cycle_stage_job_id(context, stage, existing_job)
    assert pipeline_job_id == f"{job_id}_retry_1"

    orchestrator._submit_and_wait_cycle_stage(stage, context, pipeline_job_id=pipeline_job_id)

    # Reached the stage's submission call -- not skipped as a duplicate.
    assert len(client.submissions) == 1
    successor = repository.get_pipeline_job(pipeline_job_id)
    assert successor is not None
    assert successor["submit_outcome"] == "accepted"
    assert successor["slurm_job_id"] not in (None, "")


def test_signalled_operator_command_is_actually_runnable(tmp_path: Path, capsys: Any) -> None:
    """#1748 -- copy the command out of the journal record and run it verbatim.

    The strongest available guard against drift: if the subcommand were renamed,
    or the flags changed, or the acting flag dropped, this fails. A signal that
    names a command no longer accepted by the shell is worse than one that names
    a method.
    """

    from services.orchestrator import cli as cli_module
    from tests.test_production_scheduler import _identity_released_operator_signals

    repository, record = _released_identity_blocked_master(tmp_path)
    job_id = str(record["job_id"])
    signals = _identity_released_operator_signals(repository, job_id)
    assert len(signals) == 1

    tokens = str(signals[0]["details"]["operator_command"]).split()
    assert tokens[0] == "nhms-pipeline"
    # Substitute only the placeholder the record cannot know; everything else is
    # replayed exactly as the operator would read it.
    argv = list(tokens[1:])
    argv[argv.index("--journal-root") + 1] = str(repository.root)

    assert cli_module._argparse_main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "attested"
    assert payload["job_id"] == job_id
    assert repository.get_pipeline_job(job_id)["operator_recovery_attested_at"]


# ---------------------------------------------------------------------------
# #1604/#1605/#1606: retry-attempt lineage preservation and identity.
# ---------------------------------------------------------------------------

_LINEAGE_CYCLE_TIME = _dt("2026-07-20T00:00:00Z")
_LINEAGE_PLACEHOLDERS = ("[object-uri]", "[local-path]", "[redacted]")


def _project_cohort_failure_with_identities(
    repository: FileOrchestrationJournalRepository,
    *,
    member_count: int = 2,
) -> None:
    """A projected cohort whose per-model terminal rows carry real lineage.

    ``project_forecast_cohort_tasks`` copies each task's entry verbatim out of
    the master's reservation-time map, so this is the production-shaped way to
    manufacture a FAILED contract-current candidate row with real durable
    ``init_state_identities`` (rather than hand-forging direct files).
    """

    from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition

    repository_, record = _reserved_cohort_master(
        repository.root.parent,
        member_count=member_count,
        init_state_identities=[_cohort_init_state_identity(index) for index in range(member_count)],
    )
    assert repository_.root == repository.root
    assert repository.reserve_pipeline_job(record) is not None
    commit = repository.commit_pipeline_job_submit_attempt(
        str(record["idempotency_key"]),
        pipeline_job_id=str(record["job_id"]),
        expected_submission_attempt=1,
        slurm_job_id=_PERMANENT_FAILURE_MASTER_SLURM_JOB_ID,
        transition=AcceptedSubmitTransition.accepted(status="submitted"),
    )
    assert commit.committed
    failed = repository.project_forecast_cohort_tasks(
        str(record["job_id"]),
        master_slurm_job_id=_PERMANENT_FAILURE_MASTER_SLURM_JOB_ID,
        projections=_cohort_task_projections(record, error_code="OUT_OF_MEMORY"),
        complete=True,
        master_status="failed",
        master_error_code="OUT_OF_MEMORY",
        reconciliation_decision="matched_bound",
    )
    assert failed["total"] >= 1


def _terminally_failed_cohort_master_with_identities(
    tmp_path: Path,
    *,
    member_count: int = 2,
) -> tuple[FileOrchestrationJournalRepository, dict[str, Any]]:
    """A terminally failed contract-current cohort MASTER carrying real lineage."""

    repository, record = _reserved_cohort_master(
        tmp_path,
        member_count=member_count,
        init_state_identities=[_cohort_init_state_identity(index) for index in range(member_count)],
    )
    _bind_cohort_master(repository, record)
    repository.project_forecast_cohort_tasks(
        str(record["job_id"]),
        master_slurm_job_id=_PERMANENT_FAILURE_MASTER_SLURM_JOB_ID,
        projections=_cohort_task_projections(record, error_code="OUT_OF_MEMORY"),
        complete=True,
        master_status="failed",
        master_error_code="OUT_OF_MEMORY",
        reconciliation_decision="matched_bound",
    )
    persisted = repository.get_pipeline_job(str(record["job_id"]))
    assert persisted is not None
    assert persisted["status"] == "failed"
    return repository, record


def _lineage_manual_retry_gateway() -> Any:
    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.requests.append(request)
            return {"job_id": "7160", "status": "submitted"}

    return Gateway()


def _lineage_marker_free_failed_row(
    tmp_path: Path,
    *,
    job_id: str,
    init_state_identities: list[dict[str, Any]] | None,
) -> FileOrchestrationJournalRepository:
    """One marker-free failed forecast row carrying real durable lineage."""

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    record = _pipeline_reservation_record(_LINEAGE_CYCLE_TIME, job_id=job_id)
    if init_state_identities is not None:
        record["init_state_identities"] = init_state_identities
    repository.reserve_pipeline_job(record)
    repository.update_pipeline_job_status(
        job_id,
        "failed",
        error_code="SLURM_TIMEOUT",
        error_message="timed out",
        finished_at=_LINEAGE_CYCLE_TIME,
    )
    return repository


def _lineage_jsonl_text(repository: FileOrchestrationJournalRepository) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((repository.root / "journal").rglob("*.jsonl"))
    )


def _lineage_assert_no_placeholders(rendered: str) -> None:
    for placeholder in _LINEAGE_PLACEHOLDERS:
        assert placeholder not in rendered


def _lineage_pending_manual_retry_row(tmp_path: Path) -> tuple[FileOrchestrationJournalRepository, str]:
    """Marker-free failed row plus its pending manual retry row with real lineage."""

    identities = [_cohort_init_state_identity(0)]
    identities[0] = {**identities[0], "model_id": "model_a"}
    repository = _lineage_marker_free_failed_row(
        tmp_path,
        job_id="job_lineage_pending",
        init_state_identities=identities,
    )
    pending = repository._pipeline_job_for_id_unlocked("job_lineage_pending")
    assert pending is not None
    retry_job_id = "fcst_gfs_2026072000_model_a_retry_active"
    pending_retry = {
        **pending,
        "job_id": retry_job_id,
        "status": "pending",
        "slurm_job_id": None,
        "submitted_at": None,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "retry_count": 1,
        "manual_retry_marker": True,
        "previous_job_id": "job_lineage_pending",
        "idempotency_key": "manual_retry:fcst_gfs_2026072000_model_a:1",
        "candidate_id": None,
        "error_code": None,
        "error_message": None,
        "log_uri": None,
        "updated_at": "2026-07-20T00:10:00Z",
    }
    pending_retry.pop("accepted_submit_contract_version", None)
    written = repository.upsert_pipeline_job(pending_retry)
    assert written is not None
    return repository, retry_job_id


def test_file_manual_retry_candidate_source_preserves_durable_lineage_without_authority_marker(
    tmp_path: Path,
) -> None:
    """#1604/#1605: contract-current CANDIDATE manual retry end to end.

    The predecessor is a projected per-model candidate row whose public
    projection carries the display placeholder ``[object-uri]`` in its lineage
    map.  Before this change the retry constructor copied the marker onto a row
    with cleared discriminators (rejected by accepted-submit validation) and
    built durable bytes from the public projection (lineage laundered to the
    placeholder).  The retry row must instead succeed, drop the marker, keep
    ``previous_job_id``, and inherit the exact real mapping in BOTH durable
    surfaces.
    """

    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    _project_cohort_failure_with_identities(repository)
    failed_job = repository._pipeline_job_for_id_unlocked(_TERMINAL_TASK_JOB_ID)
    assert failed_job is not None and failed_job["status"] == "failed"
    assert failed_job["accepted_submit_contract_version"] == ACCEPTED_SUBMIT_CONTRACT_VERSION
    public = repository.get_pipeline_job(_TERMINAL_TASK_JOB_ID)
    assert public["init_state_identities"] == [
        {**_cohort_init_state_identity(0), "init_state_uri": "[object-uri]"}
    ]

    gateway = _lineage_manual_retry_gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    retried = service.attempt_manual_retry("fcst_gfs_2026072000_model_0", gateway, trusted_internal=True)

    assert retried.status == "submitted"
    assert retried.previous_job_id == _TERMINAL_TASK_JOB_ID
    direct = _direct_row_payload(repository, str(retried.job_id))
    assert "accepted_submit_contract_version" not in direct
    assert direct["previous_job_id"] == _TERMINAL_TASK_JOB_ID
    assert direct["init_state_identities"] == [_cohort_init_state_identity(0)]
    jsonl_retry_payload = _durable_pipeline_job_payloads(repository.root, str(retried.job_id))[-1]
    assert jsonl_retry_payload["init_state_identities"] == [_cohort_init_state_identity(0)]
    assert "accepted_submit_contract_version" not in jsonl_retry_payload
    rendered = _lineage_jsonl_text(repository) + json.dumps(direct, sort_keys=True)
    _lineage_assert_no_placeholders(rendered)


def test_file_manual_retry_master_source_preserves_durable_lineage_without_authority_marker(
    tmp_path: Path,
) -> None:
    """#1604/#1605: contract-current MASTER manual retry end to end."""

    repository, record = _terminally_failed_cohort_master_with_identities(tmp_path)
    gateway = _lineage_manual_retry_gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    retried = service.attempt_manual_retry("cycle_gfs_2026072000", gateway, trusted_internal=True)

    assert retried.status == "submitted"
    assert retried.previous_job_id == str(record["job_id"])
    direct = _direct_row_payload(repository, str(retried.job_id))
    assert "accepted_submit_contract_version" not in direct
    assert direct["previous_job_id"] == str(record["job_id"])
    expected_master_lineage = [_cohort_init_state_identity(index) for index in range(2)]
    assert direct["init_state_identities"] == expected_master_lineage
    jsonl_retry_payload = _durable_pipeline_job_payloads(repository.root, str(retried.job_id))[-1]
    assert jsonl_retry_payload["init_state_identities"] == expected_master_lineage
    assert "accepted_submit_contract_version" not in jsonl_retry_payload
    rendered = _lineage_jsonl_text(repository) + json.dumps(direct, sort_keys=True)
    _lineage_assert_no_placeholders(rendered)


def test_file_manual_retry_submission_success_update_cannot_launder_durable_lineage(
    tmp_path: Path,
) -> None:
    """#1604 D2 second write phase: the submission-success update is private.

    The pending retry row carries real durable lineage; the update path used to
    read the PUBLIC projection (placeholder lineage) and write it back.  Even
    after deliberately re-corrupting the direct file's lineage with the
    placeholder and running the success update, the durable lineage must stay
    the real mapping -- ``upsert_pipeline_job`` treats a placeholder as a
    withheld value against the durable row.
    """

    repository, retry_job_id = _lineage_pending_manual_retry_row(tmp_path)
    real_lineage = [_cohort_init_state_identity(0)]
    real_lineage[0] = {**real_lineage[0], "model_id": "model_a"}
    durable = _durable_pipeline_job_payloads(repository.root, retry_job_id)[-1]
    assert durable["init_state_identities"] == real_lineage
    direct_path = repository.root / "pipeline-jobs" / f"{retry_job_id}.json"
    direct_record = json.loads(direct_path.read_text(encoding="utf-8"))
    direct_record["payload"]["init_state_identities"] = [
        {**real_lineage[0], "init_state_uri": "[object-uri]"}
    ]
    direct_path.write_text(json.dumps(direct_record, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    trusted_snapshot = repository._pipeline_job_for_id_unlocked(retry_job_id)
    assert trusted_snapshot is not None
    assert trusted_snapshot["init_state_identities"] == real_lineage
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    updated = service._record_manual_retry_submission_success(
        retry_job_id,
        {"job_id": "7161", "status": "submitted"},
        trusted_snapshot,
    )

    assert updated["status"] == "submitted"
    after = _durable_pipeline_job_payloads(repository.root, retry_job_id)[-1]
    assert after["status"] == "submitted"
    assert after["init_state_identities"] == real_lineage
    assert _direct_row_payload(repository, retry_job_id)["init_state_identities"] == real_lineage
    rendered = _lineage_jsonl_text(repository) + json.dumps(
        _direct_row_payload(repository, retry_job_id), sort_keys=True
    )
    _lineage_assert_no_placeholders(rendered)


def test_file_manual_retry_marker_free_and_non_forecast_sources_preserve_exact_lineage(
    tmp_path: Path,
) -> None:
    """Legacy compatibility: marker-free forecast and non-forecast sources.

    Both fixtures carry real durable lineage; their existing eligibility,
    identity and status behavior is unchanged, and the retry row inherits the
    exact mapping with no placeholders in either durable surface.
    """

    marker_free_lineage = [{**_cohort_init_state_identity(0), "model_id": "model_a"}]
    repository = _lineage_marker_free_failed_row(
        tmp_path,
        job_id="job_lineage_marker_free",
        init_state_identities=marker_free_lineage,
    )
    gateway = _lineage_manual_retry_gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    marker_free_retry = service.attempt_manual_retry(
        "fcst_gfs_2026072000_model_a", gateway, trusted_internal=True
    )

    assert marker_free_retry.status == "submitted"
    assert marker_free_retry.previous_job_id == "job_lineage_marker_free"
    assert marker_free_retry.retry_count == 1
    marker_free_direct = _direct_row_payload(repository, str(marker_free_retry.job_id))
    assert marker_free_direct["init_state_identities"] == marker_free_lineage
    assert marker_free_direct["previous_job_id"] == "job_lineage_marker_free"
    marker_free_jsonl = _durable_pipeline_job_payloads(
        repository.root, str(marker_free_retry.job_id)
    )[-1]
    assert marker_free_jsonl["init_state_identities"] == marker_free_lineage

    non_forecast_lineage = [{**_cohort_init_state_identity(1), "model_id": "model_a"}]
    non_forecast_repository = FileOrchestrationJournalRepository(tmp_path / "journal-non-forecast")
    non_forecast = _source_job(
        _LINEAGE_CYCLE_TIME,
        source_id="gfs",
        job_id="job_lineage_non_forecast",
        stage="state_save_qc",
    )
    non_forecast["init_state_identities"] = non_forecast_lineage
    non_forecast["status"] = "failed"
    non_forecast["error_code"] = "STORAGE_WRITE_FAILED"
    non_forecast["finished_at"] = "2026-07-20T00:05:00Z"
    non_forecast_repository.upsert_pipeline_job(non_forecast)
    non_forecast_gateway = _lineage_manual_retry_gateway()
    non_forecast_service = FileJournalRetryService(
        non_forecast_repository, RetryConfig(max_retries=3, backoff_schedule=[0])
    )
    non_forecast_retry = non_forecast_service.attempt_manual_retry(
        "fcst_gfs_2026072000_model_a", non_forecast_gateway, trusted_internal=True
    )

    assert non_forecast_retry.status == "submitted"
    assert non_forecast_retry.previous_job_id == "job_lineage_non_forecast"
    non_forecast_direct = _direct_row_payload(non_forecast_repository, str(non_forecast_retry.job_id))
    assert non_forecast_direct["init_state_identities"] == non_forecast_lineage
    non_forecast_jsonl = _durable_pipeline_job_payloads(
        non_forecast_repository.root, str(non_forecast_retry.job_id)
    )[-1]
    assert non_forecast_jsonl["init_state_identities"] == non_forecast_lineage
    for rendered in (
        _lineage_jsonl_text(repository) + json.dumps(marker_free_direct, sort_keys=True),
        _lineage_jsonl_text(non_forecast_repository)
        + json.dumps(non_forecast_direct, sort_keys=True),
    ):
        _lineage_assert_no_placeholders(rendered)


def test_file_manual_retry_concurrent_same_run_has_single_durable_winner(tmp_path: Path) -> None:
    """Concurrency: two synchronized manual retries, one payload and one event."""

    start_barrier = threading.Barrier(2)

    class Gateway:
        def __init__(self) -> None:
            self.requests: list[Any] = []
            self.lock = threading.Lock()

        def submit_job(self, request: Any) -> dict[str, Any]:
            with self.lock:
                self.requests.append(request)
            return {"job_id": "7162", "status": "submitted"}

    repository = _lineage_marker_free_failed_row(
        tmp_path,
        job_id="job_lineage_concurrent",
        init_state_identities=[],
    )
    gateway = Gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    results: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}

    def call(name: str) -> None:
        try:
            start_barrier.wait(5)
            results[name] = service.attempt_manual_retry(
                "fcst_gfs_2026072000_model_a", gateway, trusted_internal=True
            )
        except BaseException as error:  # noqa: BLE001 - collected for the assertions below.
            errors[name] = error

    threads = [threading.Thread(target=call, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert set(results) == {"a"} or set(results) == {"b"}
    winner_name = next(iter(results))
    assert results[winner_name].status == "submitted"
    conflict_name = next(iter(set(errors)))
    assert isinstance(errors[conflict_name], journal_module.RetryConflictError)
    retry_job_id = str(results[winner_name].job_id)
    retry_payloads = _durable_pipeline_job_payloads(repository.root, retry_job_id)
    assert len(retry_payloads) >= 1
    retry_events = [
        json.loads(line)
        for line in _lineage_jsonl_text(repository).splitlines()
        if json.loads(line).get("record_type") == "pipeline_event"
        and json.loads(line).get("payload", {}).get("event_type") == "retry"
        and json.loads(line).get("payload", {}).get("entity_id") == retry_job_id
    ]
    assert len(retry_events) == 1
    all_retry_job_ids = [
        payload["job_id"]
        for payload in (
            json.loads(line)["payload"]
            for line in _lineage_jsonl_text(repository).splitlines()
            if json.loads(line).get("record_type") == "pipeline_job"
        )
        if payload.get("manual_retry_marker") is True
    ]
    assert set(all_retry_job_ids) == {retry_job_id}
    assert len(gateway.requests) == 1


def _lineage_cross_cycle_failed_row(tmp_path: Path) -> tuple[FileOrchestrationJournalRepository, Path]:
    """A July failed row whose job id NAMES June: public scan sees it, probe cannot.

    The job id is the private probe's routing key (``_cycle_scope_from_job_id``
    and the by-cycle partition both follow the ID's own cycle), while the
    by-run scan follows the RUN's cycle through the journal jsonl and latest
    view.  A row whose id spells another cycle is therefore selectable in
    public while being invisible--or independently corruptible--through the
    private accessor.  This is the honest filesystem construction of the
    race window the rebind guard exists for; deleting or corrupting the flat
    direct file decides which failure shape the retry sees.
    """

    cross_cycle_job_id = "job_fcst_gfs_2026060100_model_a_forecast"
    repository = _lineage_marker_free_failed_row(
        tmp_path,
        job_id=cross_cycle_job_id,
        init_state_identities=[{**_cohort_init_state_identity(0), "model_id": "model_a"}],
    )
    direct_path = repository.root / "pipeline-jobs" / f"{cross_cycle_job_id}.json"
    assert direct_path.exists()
    return repository, direct_path


def test_file_manual_retry_public_selection_private_row_disappearing_writes_nothing(
    tmp_path: Path,
) -> None:
    """Selected public source exists, private probe finds none: RetryNotFoundError."""

    repository, direct_path = _lineage_cross_cycle_failed_row(tmp_path)
    direct_path.unlink()
    safe_jobs = repository.query_pipeline_jobs_by_run("fcst_gfs_2026072000_model_a")
    assert {job["job_id"] for job in safe_jobs} == {"job_fcst_gfs_2026060100_model_a_forecast"}
    assert (
        repository._pipeline_job_for_id_unlocked("job_fcst_gfs_2026060100_model_a_forecast") is None
    )

    gateway = _lineage_manual_retry_gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    with pytest.raises(RetryNotFoundError):
        service.attempt_manual_retry("fcst_gfs_2026072000_model_a", gateway, trusted_internal=True)

    assert gateway.requests == []
    for payload in (
        json.loads(line)["payload"]
        for line in _lineage_jsonl_text(repository).splitlines()
        if json.loads(line).get("record_type") == "pipeline_job"
    ):
        assert payload.get("manual_retry_marker") is not True
    retry_events = [
        json.loads(line)
        for line in _lineage_jsonl_text(repository).splitlines()
        if json.loads(line).get("record_type") == "pipeline_event"
        and json.loads(line).get("payload", {}).get("event_type") == "retry"
    ]
    assert retry_events == []


def test_file_manual_retry_invalid_private_evidence_raises_stable_retry_evidence_invalid(
    tmp_path: Path,
) -> None:
    """#1606: invalid durable evidence -> typed 409 error before any mutation.

    The public by-run scan selects the CLEAN row through the journal jsonl;
    the private rebind then probes ``pipeline-jobs/<job_id>.json`` directly
    and its record validation fails on the non-scalar ``error_code``.  The
    service must raise the stable ``RetryEvidenceInvalidError`` (no bare
    ``FileOrchestrationJournalError``) with safe run/reason/field details,
    and write nothing.
    """

    repository, direct_path = _lineage_cross_cycle_failed_row(tmp_path)
    record = json.loads(direct_path.read_text(encoding="utf-8"))
    record["payload"]["error_code"] = {"nested": "not-a-scalar"}
    direct_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    before_text = _lineage_jsonl_text(repository)

    gateway = _lineage_manual_retry_gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    with pytest.raises(RetryError) as caught:
        service.attempt_manual_retry("fcst_gfs_2026072000_model_a", gateway, trusted_internal=True)

    error = caught.value
    assert isinstance(error, journal_module.RetryEvidenceInvalidError)
    assert error.code == "RETRY_EVIDENCE_INVALID"
    assert error.status_code == 409
    assert error.details["run_id"] == "fcst_gfs_2026072000_model_a"
    assert error.details["journal_reason"] == "file_journal_invalid_field"
    assert error.details["journal_field"] == "error_code"
    assert gateway.requests == []
    assert _lineage_jsonl_text(repository) == before_text


def _lineage_auto_retry_identities() -> list[dict[str, Any]]:
    return [{**_cohort_init_state_identity(0), "model_id": "model_a"}]


def test_file_auto_retry_durable_read_fault_fails_closed_before_any_write(
    tmp_path: Path,
) -> None:
    """A private durable read fault must fail closed, never fall back public.

    The production caller hands in a narrow snapshot with no lineage field.
    If the corrupted durable row were skipped in favour of the public
    projection (or the snapshot), the retry row would persist a false empty
    lineage.  The service must raise a typed RetryError before any retry
    payload, jsonl record or event exists.
    """

    from services.orchestrator.persistence import PipelineJob

    repository = _lineage_marker_free_failed_row(
        tmp_path,
        job_id="job_lineage_auto_corrupt",
        init_state_identities=_lineage_auto_retry_identities(),
    )
    before_text = _lineage_jsonl_text(repository)
    direct_path = repository.root / "pipeline-jobs" / "job_lineage_auto_corrupt.json"
    record = json.loads(direct_path.read_text(encoding="utf-8"))
    record["payload"]["error_code"] = {"nested": "not-a-scalar"}
    direct_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    narrow_snapshot = PipelineJob(
        job_id="job_lineage_auto_corrupt",
        run_id="fcst_gfs_2026072000_model_a",
        cycle_id="gfs_2026072000",
        job_type="run_shud_forecast_array",
        slurm_job_id="3001",
        model_id="model_a",
        status="failed",
        stage="forecast",
    )
    narrow_snapshot.error_code = "SLURM_TIMEOUT"
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    with pytest.raises(RetryError) as caught:
        service.schedule_auto_retry(narrow_snapshot)

    error = caught.value
    assert error.code == "AUTO_RETRY_EVIDENCE_UNAVAILABLE"
    assert error.details["job_id"] == "job_lineage_auto_corrupt"
    assert error.details["journal_reason"] == "file_journal_invalid_field"
    assert error.details["journal_field"] == "error_code"
    assert _lineage_jsonl_text(repository) == before_text
    for payload in (
        json.loads(line)["payload"]
        for line in _lineage_jsonl_text(repository).splitlines()
        if json.loads(line).get("record_type") == "pipeline_job"
    ):
        assert "_retry_" not in str(payload.get("job_id"))
    retry_events = [
        json.loads(line)
        for line in _lineage_jsonl_text(repository).splitlines()
        if json.loads(line).get("record_type") == "pipeline_event"
        and json.loads(line).get("payload", {}).get("event_type") == "retry"
    ]
    assert retry_events == []


def test_file_auto_retry_missing_predecessor_fails_closed_before_any_write(
    tmp_path: Path,
) -> None:
    """No durable predecessor row: fail closed, never clone the snapshot.

    A narrow production snapshot whose job id has no durable row must not
    produce a retry with a fabricated empty lineage; the typed error fires
    before any retry payload, jsonl record or event.
    """

    from services.orchestrator.persistence import PipelineJob

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    narrow_snapshot = PipelineJob(
        job_id="job_lineage_auto_absent",
        run_id="fcst_gfs_2026072000_model_a",
        cycle_id="gfs_2026072000",
        job_type="run_shud_forecast_array",
        slurm_job_id="3001",
        model_id="model_a",
        status="failed",
        stage="forecast",
    )
    narrow_snapshot.error_code = "SLURM_TIMEOUT"
    assert repository._pipeline_job_for_id_unlocked("job_lineage_auto_absent") is None
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    with pytest.raises(RetryError) as caught:
        service.schedule_auto_retry(narrow_snapshot)

    error = caught.value
    assert error.code == "AUTO_RETRY_EVIDENCE_UNAVAILABLE"
    assert error.details["job_id"] == "job_lineage_auto_absent"
    assert error.details["journal_reason"] == "file_journal_predecessor_missing"
    assert error.details["journal_field"] == "job_id"
    jsonl_paths = (
        list((repository.root / "journal").rglob("*.jsonl"))
        if (repository.root / "journal").exists()
        else []
    )
    assert all(path.read_text(encoding="utf-8").strip() == "" for path in jsonl_paths)
    direct_paths = (
        list((repository.root / "pipeline-jobs").rglob("*.json"))
        if (repository.root / "pipeline-jobs").exists()
        else []
    )
    assert direct_paths == []


def test_file_auto_retry_master_virtual_row_renders_public_lineage(tmp_path: Path) -> None:
    """The master's virtual next-attempt return must stay publicly rendered.

    The private durable row is used for identity/routing only; the returned
    namespace must not carry unredacted lineage URIs to its caller, and the
    virtual row is never persisted.
    """

    repository, record = _terminally_failed_cohort_master_with_identities(tmp_path)
    before_text = _lineage_jsonl_text(repository)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    handled = service.schedule_auto_retry(
        SimpleNamespace(
            job_id=str(record["job_id"]),
            status="failed",
            error_code="OUT_OF_MEMORY",
            retry_count=0,
        )
    )

    assert handled.status == "pending"
    expected_public_lineage = [
        {**_cohort_init_state_identity(index), "init_state_uri": "[object-uri]"} for index in range(2)
    ]
    assert handled.init_state_identities == expected_public_lineage
    assert _lineage_jsonl_text(repository) == before_text
    retry_events = [
        json.loads(line)
        for line in _lineage_jsonl_text(repository).splitlines()
        if json.loads(line).get("record_type") == "pipeline_event"
        and json.loads(line).get("payload", {}).get("event_type") == "retry"
    ]
    assert retry_events == []


def _corrupt_direct_lineage(repository: FileOrchestrationJournalRepository, job_id: str, lineage: Any) -> None:
    """Corrupt the row's durable lineage on the direct surface only.

    The jsonl segments are blanked and the materialized latest views removed
    so neither replay surface can shadow the corrupted direct record with
    the last clean copy.
    """

    direct_path = repository.root / "pipeline-jobs" / f"{job_id}.json"
    record = json.loads(direct_path.read_text(encoding="utf-8"))
    record["payload"]["init_state_identities"] = lineage
    direct_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    for jsonl_path in (repository.root / "journal").rglob("*.jsonl"):
        jsonl_path.write_text("", encoding="utf-8")
    for latest_path in (repository.root / "latest").rglob("*.json"):
        latest_path.unlink()


def _jsonl_payloads(repository: FileOrchestrationJournalRepository, record_type: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)["payload"]
        for line in _lineage_jsonl_text(repository).splitlines()
        if json.loads(line).get("record_type") == record_type
    ]


def _assert_no_retry_mutation(repository: FileOrchestrationJournalRepository) -> None:
    assert [
        payload for payload in _jsonl_payloads(repository, "pipeline_job") if "_retry_" in str(payload.get("job_id"))
    ] == []
    assert [
        payload
        for payload in _jsonl_payloads(repository, "pipeline_event")
        if payload.get("event_type") == "retry"
    ] == []
    direct_paths = (
        list((repository.root / "pipeline-jobs").rglob("*retry*"))
        if (repository.root / "pipeline-jobs").exists()
        else []
    )
    assert direct_paths == []


def test_file_manual_retry_malformed_marker_free_lineage_fails_closed(
    tmp_path: Path,
) -> None:
    """Manual marker-free predecessor with a malformed string lineage map."""

    repository = _lineage_marker_free_failed_row(
        tmp_path,
        job_id="job_manual_bad_lineage",
        init_state_identities=[],
    )
    _corrupt_direct_lineage(repository, "job_manual_bad_lineage", "not-a-list")
    repository = FileOrchestrationJournalRepository(repository.root)
    gateway = _lineage_manual_retry_gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    with pytest.raises(RetryError) as caught:
        service.attempt_manual_retry("fcst_gfs_2026072000_model_a", gateway, trusted_internal=True)

    error = caught.value
    assert isinstance(error, journal_module.RetryEvidenceInvalidError)
    assert error.code == "RETRY_EVIDENCE_INVALID"
    assert error.status_code == 409
    assert error.details["journal_reason"] == "file_journal_evidence_type_invalid"
    assert error.details["journal_field"] == "init_state_identities"
    assert gateway.requests == []
    _assert_no_retry_mutation(repository)


@pytest.mark.parametrize(
    "bad_lineage",
    [
        pytest.param(["not-a-mapping"], id="non_mapping_entry"),
        pytest.param(
            [{"array_task_id": 0, "model_id": "model_a", "init_state_id": "state", "bogus": "x"}],
            id="unknown_entry_field",
        ),
        pytest.param(
            [
                {
                    "array_task_id": 0,
                    "model_id": "model_a",
                    "init_state_id": "state",
                    "init_state_uri": "[object-uri]",
                }
            ],
            id="object_uri_placeholder",
        ),
        pytest.param(
            [
                {
                    "array_task_id": 0,
                    "model_id": "model_a",
                    "init_state_id": "state",
                    "init_state_uri": "[uri]",
                }
            ],
            id="uri_placeholder",
        ),
        pytest.param(
            [
                {
                    "array_task_id": 0,
                    "model_id": "model_a",
                    "init_state_id": "state",
                    "init_state_uri": "[local-path]",
                }
            ],
            id="local_path_placeholder",
        ),
        pytest.param(
            [
                {
                    "array_task_id": 0,
                    "model_id": "model_a",
                    "init_state_id": "state",
                    "init_state_checksum": "[redacted]",
                }
            ],
            id="redacted_placeholder",
        ),
        pytest.param(
            [
                {
                    "array_task_id": 0,
                    "model_id": "model_a",
                    "init_state_id": "state",
                    "init_state_checksum": "sha256:[redacted]",
                }
            ],
            id="sha256_redacted_placeholder",
        ),
    ],
)
def test_file_manual_retry_non_forecast_invalid_lineage_fails_closed(
    tmp_path: Path,
    bad_lineage: Any,
) -> None:
    """Manual non-forecast predecessor: non-mapping entry or placeholder."""

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    non_forecast = _source_job(
        _LINEAGE_CYCLE_TIME,
        source_id="gfs",
        job_id="job_non_forecast_bad_lineage",
        stage="state_save_qc",
    )
    non_forecast["init_state_identities"] = bad_lineage
    non_forecast["status"] = "failed"
    non_forecast["error_code"] = "STORAGE_WRITE_FAILED"
    non_forecast["finished_at"] = "2026-07-20T00:05:00Z"
    repository.upsert_pipeline_job(non_forecast)
    _corrupt_direct_lineage(repository, "job_non_forecast_bad_lineage", bad_lineage)
    repository = FileOrchestrationJournalRepository(repository.root)
    gateway = _lineage_manual_retry_gateway()
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    with pytest.raises(journal_module.RetryEvidenceInvalidError) as caught:
        service.attempt_manual_retry("fcst_gfs_2026072000_model_a", gateway, trusted_internal=True)

    error = caught.value
    assert error.code == "RETRY_EVIDENCE_INVALID"
    assert error.status_code == 409
    reason = error.details["journal_reason"]
    field = error.details["journal_field"]
    if reason == "file_journal_evidence_placeholder_invalid":
        assert field == "init_state_identities"
    else:
        assert reason in {
            "file_journal_evidence_type_invalid",
            "file_journal_evidence_field_not_allowed",
        }
        assert field.startswith("init_state_identities")
    assert gateway.requests == []
    _assert_no_retry_mutation(repository)


def test_file_auto_retry_narrow_snapshot_malformed_lineage_fails_closed(
    tmp_path: Path,
) -> None:
    """Auto narrow production snapshot + malformed durable lineage."""

    from services.orchestrator.persistence import PipelineJob

    repository = _lineage_marker_free_failed_row(
        tmp_path,
        job_id="job_auto_bad_lineage",
        init_state_identities=[],
    )
    _corrupt_direct_lineage(repository, "job_auto_bad_lineage", "not-a-list")
    repository = FileOrchestrationJournalRepository(repository.root)
    narrow_snapshot = PipelineJob(
        job_id="job_auto_bad_lineage",
        run_id="fcst_gfs_2026072000_model_a",
        cycle_id="gfs_2026072000",
        job_type="run_shud_forecast_array",
        slurm_job_id="3001",
        model_id="model_a",
        status="failed",
        stage="forecast",
    )
    narrow_snapshot.error_code = "SLURM_TIMEOUT"
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    with pytest.raises(RetryError) as caught:
        service.schedule_auto_retry(narrow_snapshot)

    error = caught.value
    assert error.code == "AUTO_RETRY_EVIDENCE_UNAVAILABLE"
    assert error.details["journal_reason"] == "file_journal_evidence_type_invalid"
    assert error.details["journal_field"] == "init_state_identities"
    _assert_no_retry_mutation(repository)


def test_file_handle_failed_job_production_path_malformed_lineage_fails_closed(
    tmp_path: Path,
) -> None:
    """handle_failed_job (production entry) + malformed durable lineage."""

    repository = _lineage_marker_free_failed_row(
        tmp_path,
        job_id="job_handle_bad_lineage",
        init_state_identities=[],
    )
    _corrupt_direct_lineage(repository, "job_handle_bad_lineage", [{"no_task_id": True}])
    repository = FileOrchestrationJournalRepository(repository.root)
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    with pytest.raises(RetryError) as caught:
        service.handle_failed_job(repository.get_pipeline_job("job_handle_bad_lineage"))

    error = caught.value
    assert error.code == "AUTO_RETRY_EVIDENCE_UNAVAILABLE"
    _assert_no_retry_mutation(repository)


@pytest.mark.parametrize("empty_lineage", [None, []])
def test_file_retry_genuine_empty_lineage_stays_empty(
    tmp_path: Path,
    empty_lineage: Any,
) -> None:
    """Genuine absent/None/[] lineage keeps the existing empty semantics."""

    repository = _lineage_marker_free_failed_row(
        tmp_path,
        job_id="job_genuine_empty",
        init_state_identities=empty_lineage,
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    retried = service.schedule_auto_retry(
        repository._pipeline_job_for_id_unlocked("job_genuine_empty")
    )
    direct = _direct_row_payload(repository, str(retried.job_id))
    assert direct["init_state_identities"] == []
    jsonl = _durable_pipeline_job_payloads(repository.root, str(retried.job_id))[-1]
    assert jsonl["init_state_identities"] == []


def test_file_auto_retry_first_hop_and_retry_of_retry_bind_durable_predecessor(
    tmp_path: Path,
) -> None:
    """previous_job_id comes from the durable row, hop by hop.

    First hop (full-row and narrow-snapshot callers) and the retry-of-retry
    second hop must each bind their IMMEDIATE durable predecessor in both the
    direct payload and the jsonl replay row; the second retry points to the
    first retry, not the original, and no authority marker is retained.
    """

    from services.orchestrator.persistence import PipelineJob

    identities = _lineage_auto_retry_identities()
    repository = _lineage_marker_free_failed_row(
        tmp_path,
        job_id="job_auto_chain",
        init_state_identities=identities,
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    full_row = repository._pipeline_job_for_id_unlocked("job_auto_chain")
    assert full_row is not None
    first_full = service.schedule_auto_retry(full_row)
    first_full_direct = _direct_row_payload(repository, str(first_full.job_id))
    first_full_jsonl = _durable_pipeline_job_payloads(repository.root, str(first_full.job_id))[-1]
    for payload in (first_full_direct, first_full_jsonl):
        assert payload["previous_job_id"] == "job_auto_chain"
        assert payload["init_state_identities"] == identities
        assert "accepted_submit_contract_version" not in payload

    repository.update_pipeline_job_status(
        str(first_full.job_id), "failed", error_code="SLURM_TIMEOUT", finished_at=_LINEAGE_CYCLE_TIME
    )
    narrow_snapshot = PipelineJob(
        job_id=str(first_full.job_id),
        run_id="fcst_gfs_2026072000_model_a",
        cycle_id="gfs_2026072000",
        job_type="run_shud_forecast_array",
        slurm_job_id=None,
        model_id="model_a",
        status="failed",
        stage="forecast",
    )
    narrow_snapshot.error_code = "SLURM_TIMEOUT"
    narrow_snapshot.retry_count = 1
    second_narrow = service.schedule_auto_retry(narrow_snapshot)
    second_direct = _direct_row_payload(repository, str(second_narrow.job_id))
    second_jsonl = _durable_pipeline_job_payloads(repository.root, str(second_narrow.job_id))[-1]
    for payload in (second_direct, second_jsonl):
        assert payload["previous_job_id"] == str(first_full.job_id)
        assert payload["init_state_identities"] == identities
        assert "accepted_submit_contract_version" not in payload
    first_event = next(
        payload
        for payload in _jsonl_payloads(repository, "pipeline_event")
        if payload.get("entity_id") == str(first_full.job_id)
    )
    second_event = next(
        payload
        for payload in _jsonl_payloads(repository, "pipeline_event")
        if payload.get("entity_id") == str(second_narrow.job_id)
    )
    assert first_event["details"]["previous_job_id"] == "job_auto_chain"
    assert second_event["details"]["previous_job_id"] == str(first_full.job_id)


def test_file_manual_retry_producer_result_carries_trusted_private_snapshot(
    tmp_path: Path,
) -> None:
    """The producer result carries the exact private row; no reread needed.

    ``_create_pending_manual_retry_job`` returns a frozen result with BOTH the
    caller-facing public retry namespace (existing semantics) and a copy of the
    strict-validated private ``retry_row`` actually written under the cycle
    lock.  The private snapshot's lineage is exact (no placeholder), the
    accepted-submit marker is absent, and ``previous_job_id`` is already bound
    -- so ``attempt_manual_retry`` has everything it needs without a new
    lock-outside durable lookup.
    """

    original_lineage = _lineage_auto_retry_identities()
    repository = _lineage_marker_free_failed_row(
        tmp_path,
        job_id="job_producer_snapshot",
        init_state_identities=original_lineage,
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    result = service._create_pending_manual_retry_job("fcst_gfs_2026072000_model_a")

    assert result.public_job.status == "pending"
    assert result.public_job.manual_retry_marker is True
    snapshot = result.private_snapshot
    assert snapshot["init_state_identities"] == original_lineage
    assert "accepted_submit_contract_version" not in snapshot
    assert snapshot["previous_job_id"] == "job_producer_snapshot"
    assert snapshot["status"] == "pending"
    # The strict boundary already accepted the snapshot's lineage; a malformed
    # durable predecessor would have failed the producer before this result.
    strict = journal_module._strict_retry_init_state_identities(snapshot.get("init_state_identities"))
    assert strict == original_lineage


def test_file_manual_retry_submission_success_survives_gateway_time_corruption(
    tmp_path: Path,
) -> None:
    """Submission-success keeps the PRE-SUBMIT snapshot lineage.

    The trusted snapshot is captured before the gateway call; the callback
    then corrupts the pending row's durable lineage (direct + jsonl + latest)
    and swaps in a fresh repository so the success updater's current read
    observes the malformed row.  The submission must still record the binding
    with the exact pre-submit lineage, no placeholder, no false ``[]``,
    marker absent, ``previous_job_id`` unchanged, exactly one gateway call.
    """

    original_lineage = _lineage_auto_retry_identities()
    repository = _lineage_marker_free_failed_row(
        tmp_path,
        job_id="job_gateway_corrupt",
        init_state_identities=original_lineage,
    )
    retry_job_id = "fcst_gfs_2026072000_model_a_retry_active"

    class CorruptingGateway:
        def __init__(self) -> None:
            self.calls: list[Any] = []

        def submit_job(self, request: Any) -> dict[str, Any]:
            self.calls.append(request)
            # Corrupt the winning pending row's durable lineage and remove the
            # replay surfaces so the success updater's private current read
            # MUST observe the malformed direct record.  Swap in a fresh
            # repository so no in-memory cache of the writing instance can
            # serve the clean pre-corruption row.
            direct_path = repository.root / "pipeline-jobs" / f"{retry_job_id}.json"
            record = json.loads(direct_path.read_text(encoding="utf-8"))
            record["payload"]["init_state_identities"] = "not-a-list"
            direct_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            for jsonl_path in (repository.root / "journal").rglob("*.jsonl"):
                jsonl_path.write_text("", encoding="utf-8")
            for latest_path in (repository.root / "latest").rglob("*.json"):
                latest_path.unlink()
            service.repository = FileOrchestrationJournalRepository(repository.root)
            return {"job_id": "7180", "status": "submitted"}

    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))
    gateway = CorruptingGateway()
    retried = service.attempt_manual_retry(
        "fcst_gfs_2026072000_model_a", gateway, trusted_internal=True
    )

    assert retried.status == "submitted"
    assert len(gateway.calls) == 1
    assert retried.job_id == retry_job_id
    assert retried.slurm_job_id == "7180"
    assert retried.previous_job_id == "job_gateway_corrupt"
    assert "accepted_submit_contract_version" not in vars(retried)
    direct = _direct_row_payload(repository, retry_job_id)
    assert direct["init_state_identities"] == original_lineage
    assert direct["previous_job_id"] == "job_gateway_corrupt"
    assert "accepted_submit_contract_version" not in direct
    jsonl = _durable_pipeline_job_payloads(repository.root, retry_job_id)[-1]
    assert jsonl["init_state_identities"] == original_lineage
    assert jsonl["previous_job_id"] == "job_gateway_corrupt"
    assert "accepted_submit_contract_version" not in jsonl
    rendered = _lineage_jsonl_text(repository) + json.dumps(direct, sort_keys=True)
    _lineage_assert_no_placeholders(rendered)
    submission_events = [
        payload
        for payload in _jsonl_payloads(repository, "pipeline_event")
        if payload.get("entity_id") == retry_job_id and payload.get("event_type") == "submission"
    ]
    assert len(submission_events) == 1
    assert submission_events[0]["status_to"] == "submitted"
    assert submission_events[0]["details"]["slurm_job_id"] == "7180"


def test_file_auto_retry_full_row_and_narrow_snapshot_inherit_durable_lineage(
    tmp_path: Path,
) -> None:
    """#1605: auto retry inherits lineage from the durable row by job id.

    Full durable-row caller and narrow production-snapshot caller (a
    ``PipelineJob``-shaped namespace with no lineage field at all) must both
    inherit the durable predecessor mapping exactly in the direct payload and
    the jsonl replay record.  A marker-free source is the shape the production
    auto-retry path actually reaches.
    """

    identities = _lineage_auto_retry_identities()
    repository = _lineage_marker_free_failed_row(
        tmp_path,
        job_id="job_lineage_auto",
        init_state_identities=identities,
    )
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    full_row = repository._pipeline_job_for_id_unlocked("job_lineage_auto")
    assert full_row is not None
    retried_full = service.schedule_auto_retry(full_row)
    direct_full = _direct_row_payload(repository, str(retried_full.job_id))
    assert direct_full["init_state_identities"] == identities
    jsonl_full = _durable_pipeline_job_payloads(repository.root, str(retried_full.job_id))[-1]
    assert jsonl_full["init_state_identities"] == identities

    from services.orchestrator.persistence import PipelineJob

    narrow_snapshot = PipelineJob(
        job_id="job_lineage_auto",
        run_id="fcst_gfs_2026072000_model_a",
        cycle_id="gfs_2026072000",
        job_type="run_shud_forecast_array",
        slurm_job_id="3001",
        model_id="model_a",
        status="failed",
        stage="forecast",
    )
    narrow_snapshot.error_code = "SLURM_TIMEOUT"
    retried_narrow = service.schedule_auto_retry(narrow_snapshot)
    direct_narrow = _direct_row_payload(repository, str(retried_narrow.job_id))
    assert direct_narrow["init_state_identities"] == identities
    jsonl_narrow = _durable_pipeline_job_payloads(repository.root, str(retried_narrow.job_id))[-1]
    assert jsonl_narrow["init_state_identities"] == identities
    rendered = _lineage_jsonl_text(repository) + json.dumps(direct_narrow, sort_keys=True)
    _lineage_assert_no_placeholders(rendered)


def test_file_auto_retry_contract_candidate_and_empty_lineage_and_permanent_failure(
    tmp_path: Path,
) -> None:
    """Auto-retry shape matrix: candidate source, empty lineage, permanent mark.

    * contract-current candidate source: the auto retry row must shed the
      accepted-submit marker (attempt rows are not authority rows) while
      inheriting the candidate's exact single-entry lineage;
    * a genuinely empty durable lineage stays ``[]``;
    * ``mark_permanently_failed`` is untouched and retains the mapping.
    """

    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    _project_cohort_failure_with_identities(repository)
    failed_candidate = repository._pipeline_job_for_id_unlocked(_TERMINAL_TASK_JOB_ID)
    assert failed_candidate is not None
    service = FileJournalRetryService(repository, RetryConfig(max_retries=3, backoff_schedule=[0]))

    retried_candidate = service.schedule_auto_retry(failed_candidate)
    direct_candidate = _direct_row_payload(repository, str(retried_candidate.job_id))
    assert "accepted_submit_contract_version" not in direct_candidate
    assert direct_candidate["init_state_identities"] == [_cohort_init_state_identity(0)]
    jsonl_candidate = _durable_pipeline_job_payloads(repository.root, str(retried_candidate.job_id))[-1]
    assert jsonl_candidate["init_state_identities"] == [_cohort_init_state_identity(0)]
    assert "accepted_submit_contract_version" not in jsonl_candidate

    empty_repository = _lineage_marker_free_failed_row(
        tmp_path / "journal-empty",
        job_id="job_lineage_auto_empty",
        init_state_identities=[],
    )
    empty_service = FileJournalRetryService(
        empty_repository, RetryConfig(max_retries=3, backoff_schedule=[0])
    )
    retried_empty = empty_service.schedule_auto_retry(
        empty_repository._pipeline_job_for_id_unlocked("job_lineage_auto_empty")
    )
    direct_empty = _direct_row_payload(empty_repository, str(retried_empty.job_id))
    assert direct_empty["init_state_identities"] == []

    lineage_repository = _lineage_marker_free_failed_row(
        tmp_path / "journal-permanent",
        job_id="job_lineage_auto_permanent",
        init_state_identities=_lineage_auto_retry_identities(),
    )
    permanent_service = FileJournalRetryService(
        lineage_repository, RetryConfig(max_retries=3, backoff_schedule=[0])
    )
    permanent_service.mark_permanently_failed(
        lineage_repository._pipeline_job_for_id_unlocked("job_lineage_auto_permanent")
    )
    permanent_direct = _direct_row_payload(lineage_repository, "job_lineage_auto_permanent")
    assert permanent_direct["init_state_identities"] == _lineage_auto_retry_identities()
    permanent_jsonl = _durable_pipeline_job_payloads(
        lineage_repository.root, "job_lineage_auto_permanent"
    )[-1]
    assert permanent_jsonl["init_state_identities"] == _lineage_auto_retry_identities()
    assert permanent_jsonl["status"] == "permanently_failed"
