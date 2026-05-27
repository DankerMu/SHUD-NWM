from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.routes import pipeline as pipeline_routes
from packages.common.object_store import LocalObjectStore
from services.artifacts import ArtifactReader, ArtifactReaderConfig
from services.orchestrator.chain import ForecastOrchestrator, OrchestratorConfig
from tests.test_monitoring_api import _create_job, _store
from tests.test_orchestration_chain import FakeCycleRepository, FakeCycleSlurmClient, _basins


def test_job_logs_api_reads_published_artifact(tmp_path: Path) -> None:
    published_root = tmp_path / "published"
    log_path = published_root / "logs" / "GFS" / "2026050100" / "run_1" / "job_1.out"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("published api log", encoding="utf-8")

    with _store() as store:
        _create_job(store, job_id="job_1", log_uri="published://logs/GFS/2026050100/run_1/job_1.out")
        with _client(store, _reader(published_root)) as client:
            response = client.get("/api/v1/jobs/job_1/logs")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "job_id": "job_1",
        "log_uri": "published://logs/GFS/2026050100/run_1/job_1.out",
        "content": "published api log",
    }


def test_job_logs_api_maps_missing_log_uri_to_not_published(tmp_path: Path) -> None:
    with _store() as store:
        _create_job(store, job_id="job_no_log", log_uri=None)
        with _client(store, _reader(tmp_path / "published")) as client:
            response = client.get("/api/v1/jobs/job_no_log/logs")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_LOG_NOT_PUBLISHED"


def test_job_logs_api_preserves_job_not_found(tmp_path: Path) -> None:
    with _store() as store:
        with _client(store, _reader(tmp_path / "published")) as client:
            response = client.get("/api/v1/jobs/missing/logs")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_job_logs_api_maps_unsupported_private_uri_without_path_leak(tmp_path: Path) -> None:
    private_uri = "https://user:pass@example.test/logs/job.out?token=supersecret"
    with _store() as store:
        _create_job(store, job_id="job_private", log_uri=private_uri)
        with _client(store, _reader(tmp_path / "published")) as client:
            response = client.get("/api/v1/jobs/job_private/logs")

    body = json.dumps(response.json(), sort_keys=True)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "JOB_LOG_URI_UNSUPPORTED"
    assert "pass" not in body
    assert "supersecret" not in body
    assert "/logs/job.out?token" not in body


def test_job_logs_api_maps_unsafe_published_uri_to_access_denied(tmp_path: Path) -> None:
    with _store() as store:
        _create_job(store, job_id="job_traversal", log_uri="published://logs/GFS/2026050100/run_1/../job.out")
        with _client(store, _reader(tmp_path / "published")) as client:
            response = client.get("/api/v1/jobs/job_traversal/logs")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "JOB_LOG_ACCESS_DENIED"


def test_job_logs_api_maps_supported_missing_file_to_not_found(tmp_path: Path) -> None:
    with _store() as store:
        _create_job(store, job_id="job_missing", log_uri="published://logs/GFS/2026050100/run_1/missing.out")
        with _client(store, _reader(tmp_path / "published")) as client:
            response = client.get("/api/v1/jobs/job_missing/logs")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "JOB_LOG_NOT_FOUND"


def test_compute_pipeline_emits_published_log_uri_and_writes_published_log(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    published_root = tmp_path / "published"
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(published_root))
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=tmp_path / "object-store",
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
            job_timeout_seconds=5,
        ),
        repository=repository,
        slurm_client=client,
        object_store=LocalObjectStore(tmp_path / "object-store", "s3://nhms"),
    )

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.status == "complete"
    first_stage = result.stages[0]
    expected = (
        "published://logs/gfs/2026050100/"
        "cycle_gfs_2026050100/"
        f"{first_stage.pipeline_job_id}.out"
    )
    assert first_stage.log_uri == expected
    assert repository.jobs[first_stage.pipeline_job_id]["log_uri"] == expected
    assert (
        published_root
        / "logs"
        / "gfs"
        / "2026050100"
        / "cycle_gfs_2026050100"
        / f"{first_stage.pipeline_job_id}.out"
    ).read_text(encoding="utf-8") == "ok"


def test_compute_pipeline_keeps_legacy_object_store_uri_without_publish_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.delenv("NHMS_PUBLISHED_ARTIFACT_ROOT", raising=False)
    repository = FakeCycleRepository()
    client = FakeCycleSlurmClient()
    orchestrator = ForecastOrchestrator(
        config=OrchestratorConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=tmp_path / "object-store",
            object_store_prefix="s3://nhms",
            poll_interval_seconds=0,
            job_timeout_seconds=5,
        ),
        repository=repository,
        slurm_client=client,
        object_store=LocalObjectStore(tmp_path / "object-store", "s3://nhms"),
    )

    result = orchestrator.orchestrate_cycle("gfs", "2026050100", _basins(1))

    assert result.stages[0].log_uri == "s3://nhms/runs/cycle_gfs_2026050100/logs/download.log"


class _client:
    def __init__(self, store: Any, reader: ArtifactReader) -> None:
        self.store = store
        self.reader = reader
        self.client: TestClient | None = None

    def __enter__(self) -> TestClient:
        app = create_app(
            {
                "NHMS_SERVICE_ROLE": "display_readonly",
                "NHMS_REQUIRE_SERVICE_ROLE": "true",
                "NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS": "false",
            }
        )
        app.dependency_overrides[pipeline_routes.get_pipeline_store] = lambda: self.store
        app.state.artifact_reader = self.reader
        self.client = TestClient(app)
        return self.client

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        assert self.client is not None
        self.client.close()


def _reader(root: Path) -> ArtifactReader:
    return ArtifactReader(
        ArtifactReaderConfig(
            published_root=root,
            tail_max_bytes=1024,
            allow_legacy_local_file_logs=False,
            display_readonly=True,
            legacy_log_root=root,
        )
    )
