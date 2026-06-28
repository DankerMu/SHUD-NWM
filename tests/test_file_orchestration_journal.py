from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import pytest

from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.file_orchestration_journal import (
    FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
    FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.scheduler import ProductionScheduler, ProductionSchedulerConfig
from tests.test_production_scheduler import (
    FakeAdapter,
    _dt,
    _model,
    _set_db_free_scheduler_env,
    _write_db_free_file_provider_fixtures,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, sort_keys=True, default=str) + "\n" for row in rows),
        encoding="utf-8",
    )


def _candidate_state(
    repository: FileOrchestrationJournalRepository,
    *,
    cycle_time: datetime,
    source_id: str = "gfs",
    model_id: str = "model_a",
    job_limit: int = 100,
) -> dict[str, Any] | None:
    return repository.candidate_state(
        source_id=source_id,
        cycle_time=cycle_time,
        model_id=model_id,
        run_id=f"fcst_{source_id}_{format_cycle_time(cycle_time)}_{model_id}",
        forcing_version_id=f"forc_{source_id}_{format_cycle_time(cycle_time)}_{model_id}",
        candidate_id=f"{source_id}:{cycle_time.isoformat()}:{model_id}:forecast_{source_id}_deterministic",
        job_limit=job_limit,
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


def test_file_orchestration_journal_read_contract_active_completed_and_contexts(tmp_path: Path) -> None:
    cycle_time = _dt("2026-06-28T00:00:00Z")
    journal_root = tmp_path / "journal"
    active_job = _active_job(cycle_time)
    _write_json(
        journal_root / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="created", jobs=[active_job]),
    )
    _write_json(journal_root / "models/model_a.json", {"payload": _model_context()})
    _write_json(
        journal_root / "forcing/gfs/2026062800/model_a.json",
        {
            "payload": {
                "forcing_version_id": "forc-file",
                "forcing_package_uri": "s3://nhms/forcing/file.tar",
                "source_id": "gfs",
                "max_lead_hours": 9,
            }
        },
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
    intruder_job = dict(scoped_job)
    intruder_job["status"] = "queued"
    intruder_job["updated_at"] = "2026-06-28T12:02:00Z"
    _write_json(
        journal_root / "latest/ifs/2026062812/model_a.json",
        _latest_view(source_id="ifs", cycle_time=other_cycle_time, jobs=[intruder_job]),
    )
    _write_json(
        journal_root / "latest/z-bad/2026062812/model_a.json",
        {
            "schema_version": "wrong",
            "source_id": "z-bad",
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
    _write_json(
        paths["NHMS_SCHEDULER_JOURNAL_ROOT"] / "latest/gfs/2026062800/model_a.json",
        _latest_view(cycle_time=cycle_time, hydro_status="complete"),
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
    assert result.evidence["skipped_candidates"][0]["reason"] == "completed_duplicate_pipeline"


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
