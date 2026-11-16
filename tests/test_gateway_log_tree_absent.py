"""Compute-side handling of the gateway ``log_tree_present`` signal (#3144).

A gateway that reports ``log_tree_present: false`` means the job's log tree is
gone, not that the log is empty: nothing is written and no ``log_uri`` is
recorded, so a later pass may fetch it again.  A response without the field
(an older gateway) or with a non-bool value means present.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from services.orchestrator.chain import (
    M3_STAGES,
    CycleOrchestrationContext,
    ForecastOrchestrator,
    OrchestratorConfig,
)
from services.slurm_gateway.models import SlurmJobRecord, SlurmJobStatus
from tests.test_orchestration_chain import FakeCycleRepository, FakeCycleSlurmClient, _basins
from tests.test_real_slurm_gateway import _production_gateway, _write_array_manifest_index

_ABSENT = object()
_RUN_ID = "cycle_gfs_2026050100"
_CYCLE_ID = "gfs_2026050100"
_FORCING_JOB_ID = "job_cycle_gfs_2026050100_forcing"
_PUBLISHED_FORCING_URI = (
    "published://logs/gfs/2026050100/cycle_gfs_2026050100/job_cycle_gfs_2026050100_forcing.out"
)
_LEGACY_FORCING_URI = "s3://nhms/runs/cycle_gfs_2026050100/logs/forcing.log"


class _GatewayLogsClient(FakeCycleSlurmClient):
    """Slurm gateway client double whose ``fetch_logs`` body is fixed per test."""

    def __init__(self, *, logs: str = "ok", log_tree_present: Any = _ABSENT, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.logs_body = logs
        self.log_tree_present = log_tree_present

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        self.fetch_log_calls.append(job_id)
        response: dict[str, Any] = {"job_id": job_id, "run_id": _RUN_ID, "complete": True, "logs": self.logs_body}
        if self.log_tree_present is not _ABSENT:
            response["log_tree_present"] = self.log_tree_present
        return response


def _orchestrator(tmp_path: Path, client: Any, repository: FakeCycleRepository | None = None) -> ForecastOrchestrator:
    return ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=tmp_path / "object-store",
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
            job_timeout_seconds=120,
        ),
        repository=repository or FakeCycleRepository(),
        slurm_client=client,
        object_store=LocalObjectStore(tmp_path / "object-store", "s3://nhms"),
    )


def _terminal_job(**overrides: Any) -> dict[str, Any]:
    job = {
        "job_id": _FORCING_JOB_ID,
        "run_id": _RUN_ID,
        "cycle_id": _CYCLE_ID,
        "stage": "forcing",
        "slurm_job_id": "3002",
        "status": "succeeded",
        "log_uri": None,
    }
    job.update(overrides)
    return job


def _published_forcing_log(published_root: Path) -> Path:
    return published_root / "logs" / "gfs" / "2026050100" / _RUN_ID / f"{_FORCING_JOB_ID}.out"


def _publish(orchestrator: ForecastOrchestrator, job: dict[str, Any]) -> Any:
    publication = orchestrator._display_log_publication_for_pipeline_job(job)
    assert publication is not None
    return orchestrator._try_publish_log_for_advertise("3002", publication)


def test_absent_log_tree_publishes_nothing_and_advertises_no_uri(tmp_path: Path, monkeypatch) -> None:
    published_root = tmp_path / "published"
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(published_root))
    client = _GatewayLogsClient(logs="", log_tree_present=False)
    orchestrator = _orchestrator(tmp_path, client)

    attempt = _publish(orchestrator, _terminal_job())

    assert client.fetch_log_calls == ["3002"]
    assert attempt.advertised_uri is None
    assert attempt.error is None
    assert not _published_forcing_log(published_root).exists()
    assert not published_root.exists() or not any(published_root.rglob("*.out"))


def test_empty_but_present_log_is_published_and_advertised(tmp_path: Path, monkeypatch) -> None:
    published_root = tmp_path / "published"
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(published_root))
    client = _GatewayLogsClient(logs="", log_tree_present=True)
    orchestrator = _orchestrator(tmp_path, client)

    attempt = _publish(orchestrator, _terminal_job())

    assert attempt.advertised_uri == _PUBLISHED_FORCING_URI
    assert attempt.error is None
    assert _published_forcing_log(published_root).read_bytes() == b""


def test_existing_log_uri_is_never_refetched_or_overwritten(tmp_path: Path, monkeypatch) -> None:
    published_root = tmp_path / "published"
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(published_root))
    existing = _published_forcing_log(published_root)
    existing.parent.mkdir(parents=True)
    existing.write_text("original log", encoding="utf-8")
    client = _GatewayLogsClient(logs="", log_tree_present=False)
    orchestrator = _orchestrator(tmp_path, client)

    attempt = _publish(orchestrator, _terminal_job(log_uri=_PUBLISHED_FORCING_URI))

    assert client.fetch_log_calls == []
    assert attempt.advertised_uri == _PUBLISHED_FORCING_URI
    assert existing.read_text(encoding="utf-8") == "original log"


@pytest.mark.parametrize(
    "log_tree_present",
    [
        pytest.param(_ABSENT, id="field-missing-older-gateway"),
        pytest.param(None, id="null"),
        pytest.param("false", id="string-false"),
        pytest.param(0, id="zero"),
    ],
)
def test_missing_or_non_bool_log_tree_present_means_present(
    tmp_path: Path, monkeypatch, log_tree_present: Any
) -> None:
    published_root = tmp_path / "published"
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(published_root))
    client = _GatewayLogsClient(logs="tail", log_tree_present=log_tree_present)
    orchestrator = _orchestrator(tmp_path, client)

    attempt = _publish(orchestrator, _terminal_job())

    assert attempt.advertised_uri == _PUBLISHED_FORCING_URI
    assert _published_forcing_log(published_root).read_text(encoding="utf-8") == "tail"


def test_persist_gateway_logs_returns_false_only_when_it_skips(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NHMS_PUBLISHED_ARTIFACT_ROOT", raising=False)
    skipped_store = tmp_path / "skipped"
    skipping = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=skipped_store,
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
        ),
        repository=FakeCycleRepository(),
        slurm_client=_GatewayLogsClient(logs="", log_tree_present=False),
        object_store=LocalObjectStore(skipped_store, "s3://nhms"),
    )
    writing = _orchestrator(tmp_path, _GatewayLogsClient(logs="", log_tree_present=True))

    assert skipping._persist_gateway_logs("3002", _LEGACY_FORCING_URI) is False
    assert writing._persist_gateway_logs("3002", _LEGACY_FORCING_URI) is None
    assert not skipped_store.exists() or not any(path.is_file() for path in skipped_store.rglob("*"))
    assert (tmp_path / "object-store" / "runs" / _RUN_ID / "logs" / "forcing.log").read_bytes() == b""


def _running_forcing_master(**overrides: Any) -> dict[str, Any]:
    job = {
        "job_id": _FORCING_JOB_ID,
        "run_id": _RUN_ID,
        "cycle_id": _CYCLE_ID,
        "job_type": "produce_forcing_array",
        "slurm_job_id": "3002",
        "model_id": None,
        "status": "running",
        "stage": "forcing",
        "submitted_at": "2026-05-01T00:02:00Z",
        "started_at": "2026-05-01T00:03:00Z",
        "finished_at": None,
        "exit_code": None,
        "error_code": None,
        "error_message": None,
        "log_uri": None,
    }
    job.update(overrides)
    return job


def _register_forcing_slurm_job(client: FakeCycleSlurmClient) -> None:
    client.jobs["3002"] = {
        "job_id": "3002",
        "run_id": _RUN_ID,
        "model_id": "model_0",
        "stage": "forcing",
        "status": "running",
        "submitted_at": "2026-05-01T00:02:00Z",
        "finished_at": "2026-05-01T00:04:00Z",
        "exit_code": 0,
        "error_code": None,
        "error_message": None,
        "payload": {"tasks": [{}, {}]},
        "stage_attempt": 0,
    }
    client.poll_counts["3002"] = 1


def _forcing_context() -> CycleOrchestrationContext:
    return CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=datetime(2026, 5, 1, tzinfo=UTC),
        cycle_id=_CYCLE_ID,
        run_id=_RUN_ID,
        all_basins=_basins(2),
        active_basins=_basins(2),
        restart_stage="forcing",
    )


def _resume_legacy_forcing(tmp_path: Path, monkeypatch, log_tree_present: Any) -> tuple[Any, FakeCycleRepository]:
    # No published root and no ``stage`` on the row: the resume falls through to
    # the legacy object-store branch, which hard-codes ``should_persist_logs``.
    monkeypatch.delenv("NHMS_PUBLISHED_ARTIFACT_ROOT", raising=False)
    repository = FakeCycleRepository()
    job = _running_forcing_master()
    del job["stage"]
    repository.jobs[_FORCING_JOB_ID] = job
    client = _GatewayLogsClient(
        logs="", log_tree_present=log_tree_present, array_results_by_stage={"forcing": ["succeeded", "succeeded"]}
    )
    _register_forcing_slurm_job(client)
    orchestrator = _orchestrator(tmp_path, client, repository)

    result, aggregation = orchestrator._resume_cycle_stage(
        M3_STAGES[1], _forcing_context(), repository.jobs[_FORCING_JOB_ID]
    )

    assert aggregation is not None
    assert client.fetch_log_calls == ["3002"]
    return result, repository


def test_legacy_resume_branch_with_absent_log_tree_writes_and_records_nothing(tmp_path: Path, monkeypatch) -> None:
    result, repository = _resume_legacy_forcing(tmp_path, monkeypatch, log_tree_present=False)

    assert result.status == "succeeded"
    assert result.log_uri is None
    assert repository.jobs[_FORCING_JOB_ID]["log_uri"] is None
    assert not (tmp_path / "object-store" / "runs" / _RUN_ID / "logs" / "forcing.log").exists()


def test_legacy_resume_branch_without_field_still_publishes_legacy_log(tmp_path: Path, monkeypatch) -> None:
    result, repository = _resume_legacy_forcing(tmp_path, monkeypatch, log_tree_present=_ABSENT)

    assert result.status == "succeeded"
    assert repository.jobs[_FORCING_JOB_ID]["log_uri"] == _LEGACY_FORCING_URI
    assert (tmp_path / "object-store" / "runs" / _RUN_ID / "logs" / "forcing.log").read_bytes() == b""


class _RealGatewayLogsClient(FakeCycleSlurmClient):
    """Routes ``fetch_logs`` through a real gateway backend, as the HTTP route does."""

    def __init__(self, gateway: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.gateway = gateway

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        self.fetch_log_calls.append(job_id)
        return self.gateway.fetch_logs(job_id).model_dump(mode="json")


def test_bound_array_master_with_deleted_stored_log_dir_records_no_log_uri(tmp_path: Path, monkeypatch) -> None:
    published_root = tmp_path / "published"
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(published_root))
    gateway_root = tmp_path / "gateway"
    gateway_root.mkdir()
    gateway = _production_gateway(gateway_root)
    monkeypatch.setattr(
        "subprocess.run", lambda *args, **kwargs: pytest.fail("the gateway must not call Slurm for a bound record")
    )
    index_path = _write_array_manifest_index(
        gateway_root / "workspace" / _CYCLE_ID / "manifests" / "forcing_index_20260501T000000.json",
        (_RUN_ID, "model_0"),
    )
    now = datetime.now(UTC)
    gateway._jobs["3002"] = SlurmJobRecord(
        job_id="3002",
        run_id=_RUN_ID,
        model_id="model_0",
        status=SlurmJobStatus.SUCCEEDED,
        submitted_at=now,
        updated_at=now,
        finished_at=now,
        manifest={"run_id": _RUN_ID, "cycle_id": _CYCLE_ID, "manifest_index_path": str(index_path)},
    )
    # Retention removed the stored array log dir; the run's own ``logs`` lane
    # never existed for this array master.
    array_logs = gateway_root / "workspace" / _CYCLE_ID / "array_logs"
    (array_logs / index_path.stem).mkdir(parents=True)
    (array_logs / index_path.stem / "3002_0.out").write_text("task stdout", encoding="utf-8")
    shutil.rmtree(array_logs)
    assert not (gateway_root / "workspace" / _RUN_ID).exists()
    repository = FakeCycleRepository()
    repository.jobs[_FORCING_JOB_ID] = _running_forcing_master()
    client = _RealGatewayLogsClient(gateway, array_results_by_stage={"forcing": ["succeeded", "succeeded"]})
    _register_forcing_slurm_job(client)
    orchestrator = _orchestrator(tmp_path, client, repository)

    result, aggregation = orchestrator._resume_cycle_stage(
        M3_STAGES[1], _forcing_context(), repository.jobs[_FORCING_JOB_ID]
    )

    assert aggregation is not None
    assert client.fetch_log_calls[0] == "3002"
    assert result.status == "succeeded"
    assert repository.jobs[_FORCING_JOB_ID]["log_uri"] is None
    assert result.log_uri is None
    assert not _published_forcing_log(published_root).exists()
    assert gateway.fetch_logs("3002").log_tree_present is False
