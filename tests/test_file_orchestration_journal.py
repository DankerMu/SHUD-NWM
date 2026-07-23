from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from datetime import UTC, datetime
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Mapping

import pytest

from packages.common import safe_fs
from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.chain import SlurmClientError
from services.orchestrator.chain_types import OrchestratorError
from services.orchestrator.file_orchestration_journal import (
    FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
    FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
    FileJournalRetryService,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.retry import RetryConfig, RetryError, RetryNotFoundError
from services.orchestrator.scheduler import ProductionScheduler, ProductionSchedulerConfig
from tests.test_production_scheduler import (
    FakeAdapter,
    _dt,
    _model,
    _set_db_free_scheduler_env,
    _write_db_free_file_provider_fixtures,
    _write_db_free_raw_manifest_fixture,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time


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
    job_limit: int = 100,
    event_limit: int = 100,
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


def test_file_orchestration_journal_reconcile_scan_skips_bad_journal_path(
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

    inflight = repository.query_inflight_jobs()

    assert {job.job_id for job in inflight} == {"job_reconcile_pending_after_bad_path"}


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


def test_file_orchestration_journal_migration_skips_bad_entry_and_keeps_old_active(
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

    reserved = repository.query_reserved_unbound_jobs()

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

    assert query[0]["error_code"] == "file_journal_unsafe_scanned_entry"
    assert query[0]["file_journal"]["field"] == "latest"


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

    query = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
    assert query == [
        {
            "job_id": "file_journal_read_blocked",
            "idempotency_key": None,
            "cycle_id": cycle_id_for("gfs", cycle_time),
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

    assert repository.has_active_pipeline(source_id="gfs", cycle_time=cycle_time, model_id="model_a") is True
    state = _candidate_state(repository, cycle_time=cycle_time)
    assert state is not None
    assert state["pipeline_status"] == "running"
    assert state["file_journal"]["reason"] == "file_journal_malformed_json"
    assert state["file_journal"]["field"] == "pipeline-jobs/job_cycle_ifs_2026062812_forecast.json"

    query = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
    assert query[0]["error_code"] == "file_journal_malformed_json"
    assert query[0]["file_journal"]["field"] == "pipeline-jobs/job_cycle_ifs_2026062812_forecast.json"


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

    query = FileOrchestrationJournalRepository(journal_root).query_pipeline_jobs_by_cycle(
        cycle_id_for("gfs", cycle_time)
    )

    assert query[0]["status"] == "running"
    assert query[0]["stage"] == "file_journal_read"
    assert query[0]["error_code"] == "file_journal_invalid_identity"
    assert query[0]["file_journal"]["field"] == "source_id"


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
        repository = FileOrchestrationJournalRepository(journal_root, max_depth=1)
        query = repository.query_pipeline_jobs_by_cycle(cycle_id_for("gfs", cycle_time))
        assert query[0]["error_code"] == expected_reason
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

    query = FileOrchestrationJournalRepository(journal_root, max_files=3).query_pipeline_jobs_by_cycle(
        cycle_id_for("gfs", cycle_time)
    )

    assert query[0]["error_code"] == "file_journal_file_limit_exceeded"
    assert query[0]["file_journal"]["field"] == "latest"


def test_file_orchestration_journal_oversized_non_matching_directory_listing_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    (journal_root / "latest").mkdir(parents=True)
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
    assert query[0]["file_journal"]["field"] == "latest"


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

    query = FileOrchestrationJournalRepository(journal_root, max_records=1).query_pipeline_jobs_by_cycle(
        cycle_id_for("gfs", first_cycle_time)
    )

    assert query[0]["error_code"] == "file_journal_record_limit_exceeded"
    assert query[0]["file_journal"]["field"] == "pipeline_job_records"


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
