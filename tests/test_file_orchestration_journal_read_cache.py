"""Read-cache and append-coherence tests for the file orchestration journal.

Covers the stat-identity byte cache (`_read_bytes_limited_cached`), the
prevalidated fast decode, and the append-time rows-cache synchronization
that keeps materialized latest views coherent with the record appended in
the same locked write window.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
)
from workers.data_adapters.base import cycle_id_for, format_cycle_time


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


CYCLE_TIME = _dt("2026-06-28T00:00:00Z")
CYCLE_SEGMENT = format_cycle_time(CYCLE_TIME)


def _run_id(model_id: str = "model_a") -> str:
    return f"fcst_gfs_{CYCLE_SEGMENT}_{model_id}"


def _basin_manifest(model_id: str = "model_a") -> dict[str, Any]:
    return {
        "run_id": _run_id(model_id),
        "run_type": "forecast",
        "scenario_id": "scenario_a",
        "source_id": "gfs",
        "cycle_time": CYCLE_TIME.isoformat(),
        "start_time": CYCLE_TIME.isoformat(),
        "end_time": CYCLE_TIME.isoformat(),
        "model": {"model_id": model_id, "basin_version_id": "basin_version_a"},
        "forcing": {"forcing_version_id": f"forc_gfs_{CYCLE_SEGMENT}_{model_id}"},
        "outputs": {
            "run_manifest_uri": "s3://nhms/manifests/run.json",
            "output_uri": "s3://nhms/runs/output",
            "log_uri": "s3://nhms/logs/run.log",
        },
    }


def _candidate_state(
    repository: FileOrchestrationJournalRepository,
    *,
    model_id: str = "model_a",
) -> dict[str, Any] | None:
    return repository.candidate_state(
        source_id="gfs",
        cycle_time=CYCLE_TIME,
        model_id=model_id,
        run_id=_run_id(model_id),
        forcing_version_id=f"forc_gfs_{CYCLE_SEGMENT}_{model_id}",
        candidate_id=f"gfs:{CYCLE_TIME.isoformat()}:{model_id}:forecast_gfs_deterministic",
        job_limit=100,
        event_limit=100,
    )


def _latest_path(root: Path, model_id: str = "model_a") -> Path:
    return root / "latest" / "gfs" / CYCLE_SEGMENT / f"{model_id}.json"


def _job_record(model_id: str = "model_a") -> dict[str, Any]:
    return {
        "job_id": f"job_{_run_id(model_id)}_forecast",
        "run_id": _run_id(model_id),
        "cycle_id": cycle_id_for("gfs", CYCLE_TIME),
        "job_type": "forecast",
        "model_id": model_id,
        "status": "running",
        "stage": "forecast",
        "slurm_job_id": "3001",
    }


def test_read_cache_hit_skips_reread_and_returns_fresh_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": {"b": 1}}), encoding="utf-8")

    calls = 0
    real_reader = journal_module.read_bytes_limited_no_follow

    def counting_reader(path: Path, **kwargs: Any) -> bytes:
        nonlocal calls
        calls += 1
        return real_reader(path, **kwargs)

    monkeypatch.setattr(journal_module, "read_bytes_limited_no_follow", counting_reader)

    first = repository._read_optional_json(target)
    second = repository._read_optional_json(target)

    assert calls == 1
    assert first == {"a": {"b": 1}}
    assert second == {"a": {"b": 1}}
    assert first is not second
    first["a"]["b"] = 999
    assert repository._read_optional_json(target) == {"a": {"b": 1}}


def test_read_cache_misses_after_rewrite_and_append(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "journal" / "gfs" / f"{CYCLE_SEGMENT}.jsonl"
    target.parent.mkdir(parents=True)
    row = {"schema_version": "x", "value": 1}
    target.write_text(json.dumps(row) + "\n", encoding="utf-8")

    assert len(repository._read_jsonl(target)) == 1

    with target.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"schema_version": "x", "value": 2}) + "\n")
    assert len(repository._read_jsonl(target)) == 2

    replacement = target.with_suffix(".tmp")
    replacement.write_text(json.dumps({"schema_version": "x", "value": 3}) + "\n", encoding="utf-8")
    os.replace(replacement, target)
    records = repository._read_jsonl(target)
    assert len(records) == 1
    assert records[0]["value"] == 3


def test_read_cache_deleted_file_returns_missing(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": 1}), encoding="utf-8")

    assert repository._read_optional_json(target) == {"a": 1}
    target.unlink()
    assert repository._read_optional_json(target) is None


def test_read_cache_symlink_swap_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": 1}), encoding="utf-8")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"evil": True}), encoding="utf-8")

    assert repository._read_optional_json(target) == {"a": 1}
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(FileOrchestrationJournalError):
        repository._read_optional_json(target)


def test_read_cache_malformed_json_errors_repeat(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text("{not json", encoding="utf-8")

    for _ in range(2):
        with pytest.raises(FileOrchestrationJournalError):
            repository._read_optional_json(target)


def test_read_cache_byte_limit_still_enforced(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root, max_bytes=16)
    target = root / "pipeline-jobs" / "job_a.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"a": "x" * 64}), encoding="utf-8")

    for _ in range(2):
        with pytest.raises(FileOrchestrationJournalError):
            repository._read_optional_json(target)


def test_latest_view_contains_event_appended_in_same_window(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=CYCLE_TIME)
    repository.create_hydro_run_from_basin({"source_id": "gfs"}, _basin_manifest())
    repository.upsert_pipeline_job(_job_record())

    event = repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=_job_record()["job_id"],
        event_type="status_change",
        status_from="pending",
        status_to="running",
    )

    latest = json.loads(_latest_path(root).read_text(encoding="utf-8"))
    event_ids = {str(row.get("event_id")) for row in latest["pipeline_events"]}
    assert str(event["event_id"]) in event_ids


def test_latest_view_reflects_terminal_status_immediately(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=CYCLE_TIME)
    repository.create_hydro_run_from_basin({"source_id": "gfs"}, _basin_manifest())

    repository.update_hydro_run_status(_run_id(), "published", slurm_job_id="3001")

    latest = json.loads(_latest_path(root).read_text(encoding="utf-8"))
    assert latest["hydro_run"]["status"] == "published"
    fresh = FileOrchestrationJournalRepository(root)
    assert fresh.has_completed_pipeline(source_id="gfs", cycle_time=CYCLE_TIME, model_id="model_a") is True
    assert (
        repository.has_completed_pipeline(source_id="gfs", cycle_time=CYCLE_TIME, model_id="model_a")
        is True
    )


def test_writer_view_matches_fresh_instance_after_each_write(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)

    def assert_views_match() -> None:
        fresh = FileOrchestrationJournalRepository(root)
        assert _candidate_state(repository) == _candidate_state(fresh)
        assert repository.has_completed_pipeline(
            source_id="gfs", cycle_time=CYCLE_TIME, model_id="model_a"
        ) == fresh.has_completed_pipeline(source_id="gfs", cycle_time=CYCLE_TIME, model_id="model_a")

    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=CYCLE_TIME)
    assert_views_match()
    repository.create_hydro_run_from_basin({"source_id": "gfs"}, _basin_manifest())
    assert_views_match()
    repository.upsert_pipeline_job(_job_record())
    assert_views_match()
    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=_job_record()["job_id"],
        event_type="status_change",
        status_from="pending",
        status_to="running",
    )
    assert_views_match()
    repository.update_hydro_run_status(_run_id(), "succeeded", slurm_job_id="3001")
    assert_views_match()
    repository.update_forecast_cycle_status(
        source_id="gfs",
        cycle_time=CYCLE_TIME,
        status="complete",
    )
    assert_views_match()


def test_cycle_sweep_materializes_consistent_latest_sequence(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=CYCLE_TIME)
    repository.create_hydro_run_from_basin({"source_id": "gfs"}, _basin_manifest("model_a"))
    repository.create_hydro_run_from_basin({"source_id": "gfs"}, _basin_manifest("model_b"))

    repository.insert_pipeline_event(
        entity_type="forecast_cycle",
        entity_id=cycle_id_for("gfs", CYCLE_TIME),
        event_type="status_change",
        status_from="discovered",
        status_to="complete",
    )

    journal_path = root / "journal" / "gfs" / f"{CYCLE_SEGMENT}.jsonl"
    max_sequence = max(
        int(json.loads(line)["sequence"])
        for line in journal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    sequences = set()
    for model_id in ("model_a", "model_b"):
        latest = json.loads(_latest_path(root, model_id).read_text(encoding="utf-8"))
        sequences.add(int(latest["replay"]["latest_sequence"]))
    assert sequences == {max_sequence}
