from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.routes import pipeline as pipeline_routes
from packages.common.object_store import LocalObjectStore
from services.artifacts import ArtifactReader, ArtifactReaderConfig
from services.orchestrator.chain import ForecastOrchestrator, OrchestratorConfig
from tests.test_monitoring_api import _create_job, _cycle_time, _insert_cycle, _store
from tests.test_orchestration_chain import (
    FakeCycleRepository,
    FakeCycleSlurmClient,
    _basins,
    _successful_control_node_publisher,
)
from workers.data_adapters.base import cycle_id_for


class StubObjectReader:
    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self.objects = objects
        self.calls: list[tuple[str, str, int]] = []

    def read_tail_bytes(self, bucket: str, key: str, *, max_bytes: int) -> bytes:
        self.calls.append((bucket, key, max_bytes))
        return self.objects[(bucket, key)][-max_bytes:]


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


def test_job_logs_api_reads_legacy_object_store_run_log_uri(tmp_path: Path) -> None:
    log_uri = "s3://nhms/runs/cycle_gfs_2026050100/logs/download.log"
    object_reader = StubObjectReader({("nhms", "runs/cycle_gfs_2026050100/logs/download.log"): b"legacy api log"})
    reader = ArtifactReader(
        ArtifactReaderConfig(
            published_root=tmp_path / "published",
            s3_bucket="nhms",
            s3_prefix="runs",
            allow_legacy_local_file_logs=False,
            display_readonly=True,
        ),
        object_reader=object_reader,
    )

    with _store() as store:
        _create_job(store, job_id="job_legacy_s3", log_uri=log_uri)
        with _client(store, reader) as client:
            response = client.get("/api/v1/jobs/job_legacy_s3/logs")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "job_id": "job_legacy_s3",
        "log_uri": log_uri,
        "content": "legacy api log",
    }
    assert object_reader.calls == [("nhms", "runs/cycle_gfs_2026050100/logs/download.log", 1024 * 1024)]


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


@pytest.mark.parametrize(
    "uri_name",
    ["published", "file"],
)
def test_job_logs_api_maps_decoded_nul_supported_uri_forms_without_500(
    tmp_path: Path,
    uri_name: str,
) -> None:
    published_root = tmp_path / "published"
    if uri_name == "published":
        log_uri = "published://logs/GFS/2026050100/run_1/bad%00.out"
    else:
        log_uri = (
            (published_root / "logs" / "GFS" / "2026050100" / "run_1" / "bad.out")
            .as_uri()
            .replace("bad.out", "bad%00.out")
        )

    with _store() as store:
        _create_job(store, job_id=f"job_nul_{uri_name}", log_uri=log_uri)
        with _client(store, _reader(published_root)) as client:
            response = client.get(f"/api/v1/jobs/job_nul_{uri_name}/logs")

    body = json.dumps(response.json(), sort_keys=True)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "JOB_LOG_URI_UNSUPPORTED"
    assert response.json()["error"]["details"]["reason"] == "malformed_path"
    assert "%00" not in body
    assert "\\u0000" not in body
    assert "embedded null" not in body.lower()
    assert str(published_root) not in body
    assert "bad.out" not in body


def test_job_logs_api_env_reader_honors_tail_max_bytes(tmp_path: Path, monkeypatch: Any) -> None:
    published_root = tmp_path / "published"
    log_path = published_root / "logs" / "GFS" / "2026050100" / "run_1" / "job_1.out"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("0123456789abcdef", encoding="utf-8")
    _set_display_artifact_env(
        monkeypatch,
        published_root=published_root,
        tail_max_bytes=8,
        allow_legacy_local=False,
    )

    with _store() as store:
        _create_job(store, job_id="job_env_tail", log_uri="published://logs/GFS/2026050100/run_1/job_1.out")
        with _env_client(store) as client:
            response = client.get("/api/v1/jobs/job_env_tail/logs")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "job_id": "job_env_tail",
        "log_uri": "published://logs/GFS/2026050100/run_1/job_1.out",
        "content": "89abcdef",
    }


def test_job_logs_api_env_reader_rejects_symlinked_publish_root_parent_without_leak(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    private_workspace = tmp_path / "private" / ".nhms-runs"
    private_workspace.mkdir(parents=True)
    parent_link = tmp_path / "published_parent_link"
    parent_link.symlink_to(private_workspace, target_is_directory=True)
    published_root = parent_link / "published"
    log_path = published_root / "logs" / "GFS" / "2026050100" / "run_1" / "job_1.out"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("private api content", encoding="utf-8")
    _set_display_artifact_env(monkeypatch, published_root=published_root, allow_legacy_local=False)

    with _store() as store:
        _create_job(store, job_id="job_symlinked_root", log_uri="published://logs/GFS/2026050100/run_1/job_1.out")
        with _env_client(store) as client:
            response = client.get("/api/v1/jobs/job_symlinked_root/logs")

    body = json.dumps(response.json(), sort_keys=True)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "JOB_LOG_ACCESS_DENIED"
    assert response.json()["error"]["details"]["reason"] == "unsafe_local_path"
    assert "private api content" not in body
    assert str(tmp_path) not in body
    assert ".nhms-runs" not in body


def test_job_logs_api_env_reader_denies_private_legacy_when_disabled(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _set_display_artifact_env(
        monkeypatch,
        published_root=tmp_path / "published",
        allow_legacy_local=False,
        legacy_log_root=tmp_path / "legacy",
    )
    private_uri = "/scratch/node22/.nhms-runs/run_1/token-supersecret.out"

    with _store() as store:
        _create_job(store, job_id="job_private_legacy", log_uri=private_uri)
        with _env_client(store) as client:
            response = client.get("/api/v1/jobs/job_private_legacy/logs")

    body = json.dumps(response.json(), sort_keys=True)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "JOB_LOG_ACCESS_DENIED"
    assert "/scratch" not in body
    assert ".nhms-runs" not in body
    assert "token-supersecret" not in body
    assert str(tmp_path) not in body


def test_job_logs_api_env_reader_maps_malformed_uri_without_500(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _set_display_artifact_env(monkeypatch, published_root=tmp_path / "published", allow_legacy_local=False)

    with _store() as store:
        _create_job(store, job_id="job_malformed", log_uri="http://example.test:bad/log.out")
        with _env_client(store) as client:
            response = client.get("/api/v1/jobs/job_malformed/logs")

    body = json.dumps(response.json(), sort_keys=True)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "JOB_LOG_URI_UNSUPPORTED"
    assert "example.test:bad" not in body


def test_job_logs_api_env_reader_redacts_credential_path_before_query_rejection(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _set_display_artifact_env(monkeypatch, published_root=tmp_path / "published", allow_legacy_local=False)
    secret_uri = "published://logs/GFS/2026050100/run_1/token-supersecret.out?x=y"

    with _store() as store:
        _create_job(store, job_id="job_secret_path", log_uri=secret_uri)
        with _env_client(store) as client:
            response = client.get("/api/v1/jobs/job_secret_path/logs")

    body = json.dumps(response.json(), sort_keys=True)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "JOB_LOG_URI_UNSUPPORTED"
    assert "token-supersecret" not in body
    assert "?x=y" not in body


@pytest.mark.parametrize(
    ("uri", "reason", "unsafe"),
    [
        (
            "published://token-supersecret/GFS/2026050100/run_1/job.out",
            "credential_path_component",
            "token-supersecret",
        ),
        (
            "published://bad%00secret/GFS/2026050100/run_1/job.out",
            "malformed_path",
            "bad%00secret",
        ),
    ],
)
def test_job_logs_api_redacts_unsafe_published_authority_without_500(
    tmp_path: Path,
    monkeypatch: Any,
    uri: str,
    reason: str,
    unsafe: str,
) -> None:
    _set_display_artifact_env(monkeypatch, published_root=tmp_path / "published", allow_legacy_local=False)

    with _store() as store:
        _create_job(store, job_id="job_unsafe_published_authority", log_uri=uri)
        with _env_client(store) as client:
            response = client.get("/api/v1/jobs/job_unsafe_published_authority/logs")

    body = json.dumps(response.json(), sort_keys=True)
    assert response.status_code in {400, 403}
    assert response.json()["error"]["code"] in {"JOB_LOG_URI_UNSUPPORTED", "JOB_LOG_ACCESS_DENIED"}
    assert response.json()["error"]["details"]["log_uri"] == "published://redacted/[redacted]"
    assert response.json()["error"]["details"]["reason"] == reason
    assert unsafe not in body
    assert "GFS/2026050100/run_1/job.out" not in body
    assert "%00" not in body
    assert "\\u0000" not in body
    assert "embedded null" not in body.lower()


def test_job_logs_api_env_reader_denies_file_uri_non_logs_namespace(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    published_root = tmp_path / "published"
    internal_path = published_root / "internal" / "debug.txt"
    internal_path.parent.mkdir(parents=True)
    internal_path.write_text("private debug", encoding="utf-8")
    _set_display_artifact_env(monkeypatch, published_root=published_root, allow_legacy_local=False)

    with _store() as store:
        _create_job(store, job_id="job_internal_file", log_uri=internal_path.as_uri())
        with _env_client(store) as client:
            response = client.get("/api/v1/jobs/job_internal_file/logs")

    body = json.dumps(response.json(), sort_keys=True)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "JOB_LOG_URI_UNSUPPORTED"
    assert "private debug" not in body
    assert str(internal_path) not in body


def test_jobs_and_stages_metadata_redacts_stale_private_log_uri(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _set_display_artifact_env(monkeypatch, published_root=tmp_path / "published", allow_legacy_local=False)
    cycle_time = _cycle_time()
    cycle_id = cycle_id_for("GFS", cycle_time)
    private_uri = "/scratch/node22/.nhms-runs/run_1/private.out"

    with _store() as store:
        _insert_cycle(store, cycle_time=cycle_time)
        _create_job(
            store,
            job_id="job_private_metadata",
            cycle_id=cycle_id,
            stage="forecast",
            status="failed",
            log_uri=private_uri,
        )
        with _env_client(store) as client:
            jobs_response = client.get(
                "/api/v1/jobs",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat(), "limit": 20},
            )
            stages_response = client.get(
                "/api/v1/pipeline/stages",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat()},
            )

    body = json.dumps({"jobs": jobs_response.json(), "stages": stages_response.json()}, sort_keys=True)
    assert jobs_response.status_code == 200
    assert stages_response.status_code == 200
    assert "/scratch" not in body
    assert ".nhms-runs" not in body
    assert "private.out" not in body


def test_jobs_and_stages_metadata_redacts_stale_unsafe_published_authority(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _set_display_artifact_env(monkeypatch, published_root=tmp_path / "published", allow_legacy_local=False)
    cycle_time = _cycle_time()
    cycle_id = cycle_id_for("GFS", cycle_time)

    with _store() as store:
        _insert_cycle(store, cycle_time=cycle_time)
        _create_job(
            store,
            job_id="job_secret_authority_metadata",
            cycle_id=cycle_id,
            stage="forecast",
            status="failed",
            log_uri="published://token-supersecret/GFS/2026050100/run_1/job.out",
        )
        _create_job(
            store,
            job_id="job_malformed_authority_metadata",
            cycle_id=cycle_id,
            stage="forecast",
            status="failed",
            log_uri="published://bad%00secret/GFS/2026050100/run_1/job.out",
        )
        with _env_client(store) as client:
            jobs_response = client.get(
                "/api/v1/jobs",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat(), "limit": 20},
            )
            stages_response = client.get(
                "/api/v1/pipeline/stages",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat()},
            )

    body = json.dumps({"jobs": jobs_response.json(), "stages": stages_response.json()}, sort_keys=True)
    assert jobs_response.status_code == 200
    assert stages_response.status_code == 200
    assert "token-supersecret" not in body
    assert "bad%00secret" not in body
    assert "GFS/2026050100/run_1/job.out" not in body
    assert "%00" not in body
    assert "\\u0000" not in body
    assert "published://redacted/[redacted]" in body


def test_compute_pipeline_emits_published_log_uri_and_writes_published_log(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    published_root = tmp_path / "published"
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(published_root))
    monkeypatch.setattr("services.orchestrator.chain.TilePublisher", _successful_control_node_publisher())
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
    for stage in result.stages:
        expected = (
            "published://logs/gfs/2026050100/"
            "cycle_gfs_2026050100/"
            f"{stage.pipeline_job_id}.out"
        )
        assert stage.log_uri == expected
        assert repository.jobs[stage.pipeline_job_id]["log_uri"] == expected
        log_content = (
            published_root
            / "logs"
            / "gfs"
            / "2026050100"
            / "cycle_gfs_2026050100"
            / f"{stage.pipeline_job_id}.out"
        ).read_text(encoding="utf-8")
        if stage.stage == "publish":
            assert json.loads(log_content)["status"] == "published"
        else:
            assert log_content == "ok"


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
                "OBJECT_STORE_ROOT": _temp_object_store_root(),
            }
        )
        app.dependency_overrides[pipeline_routes.get_pipeline_store] = lambda: self.store
        app.state.artifact_reader = self.reader
        self.client = TestClient(app)
        return self.client

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        assert self.client is not None
        self.client.close()


class _env_client:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.client: TestClient | None = None

    def __enter__(self) -> TestClient:
        app = create_app(_display_env())
        app.dependency_overrides[pipeline_routes.get_pipeline_store] = lambda: self.store
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


def _display_env() -> dict[str, str]:
    return {
        "NHMS_SERVICE_ROLE": "display_readonly",
        "NHMS_REQUIRE_SERVICE_ROLE": "true",
        "NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS": "false",
        "OBJECT_STORE_ROOT": _temp_object_store_root(),
    }


def _temp_object_store_root() -> str:
    return tempfile.mkdtemp(prefix="nhms-display-object-store-")


def _set_display_artifact_env(
    monkeypatch: Any,
    *,
    published_root: Path,
    tail_max_bytes: int = 1024,
    allow_legacy_local: bool,
    legacy_log_root: Path | None = None,
) -> None:
    monkeypatch.setenv("NHMS_PUBLISHED_ARTIFACT_ROOT", str(published_root))
    monkeypatch.setenv("NHMS_LOG_TAIL_MAX_BYTES", str(tail_max_bytes))
    monkeypatch.setenv("NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS", "true" if allow_legacy_local else "false")
    if legacy_log_root is not None:
        monkeypatch.setenv("LOG_ROOT", str(legacy_log_root))
