from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

import pytest

from packages.common import safe_fs
from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator import scheduler as scheduler_module
from services.orchestrator.chain_types import OrchestratorError
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


@pytest.mark.parametrize(
    "method_name",
    [
        "ensure_forecast_cycle",
        "create_hydro_run",
        "create_hydro_run_from_basin",
        "update_hydro_run_status",
        "upsert_pipeline_job",
        "reserve_pipeline_job",
        "reclaim_pipeline_job_reservation",
        "bind_pipeline_job_reservation",
        "update_pipeline_job_status",
        "insert_pipeline_event",
        "update_forecast_cycle_status",
    ],
)
def test_file_orchestration_journal_write_methods_fail_not_implemented(
    tmp_path: Path,
    method_name: str,
) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")

    with pytest.raises(OrchestratorError) as error:
        getattr(repository, method_name)()

    assert error.value.error_code == "FILE_JOURNAL_WRITE_NOT_IMPLEMENTED"


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

    assert result.status == "preflight_blocked"
    assert result.evidence["skipped_candidates"][0]["reason"] == "active_slurm_status_sync_deferred"
    sync_evidence = result.evidence["model_run_evidence"][0]
    assert sync_evidence["error_code"] == "DB_FREE_FILE_JOURNAL_WRITE_NOT_IMPLEMENTED"
    assert sync_evidence["sync_attempted"] is False
    assert sync_evidence["evidence_pre_execution"]["reason"] == "db_free_file_journal_write_not_implemented"
    assert result.evidence["slurm_status_sync_proof"]["status"] == "preflight_blocked"
    assert result.evidence["slurm_status_sync_proof"]["sync_called"] is False
    assert result.evidence["slurm_status_sync_proof"]["block_reason"] == (
        "db_free_file_journal_write_not_implemented"
    )
    assert result.evidence["no_mutation_proof"]["slurm_status_sync_called"] is False
    assert result.evidence["no_mutation_proof"]["pipeline_status_writes"] is False


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

    assert result.status == "preflight_blocked"
    assert result.evidence["skipped_candidates"][0]["reason"] == "cancel_requested_active_slurm"
    cancellation = result.evidence["slurm_cancellation_evidence"][0]
    assert cancellation["error_code"] == "DB_FREE_FILE_JOURNAL_WRITE_NOT_IMPLEMENTED"
    assert cancellation["cancel_attempted"] is False
    assert cancellation["replacement_submitted"] is False
    assert cancellation["evidence_pre_execution"]["reason"] == "db_free_file_journal_write_not_implemented"
    assert result.evidence["slurm_cancellation_proof"]["status"] == "preflight_blocked"
    assert result.evidence["slurm_cancellation_proof"]["cancel_called"] is False
    assert result.evidence["slurm_cancellation_proof"]["block_reason"] == "db_free_file_journal_write_not_implemented"
    assert result.evidence["counts"]["slurm_cancellation_blocked_count"] == 1
    assert result.evidence["no_mutation_proof"]["slurm_cancellation_called"] is False
    assert result.evidence["no_mutation_proof"]["pipeline_status_writes"] is False


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
