from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.main import app
from apps.api.routes import pipeline as pipeline_routes
from services.orchestrator.persistence import Base, PipelineEvent, PipelineJob, PipelineStore
from workers.data_adapters.base import cycle_id_for


def test_pipeline_status_endpoint() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        cycle_id = cycle_id_for("GFS", cycle_time)
        _insert_cycle(store, cycle_time=cycle_time, current_state="forecast_running")
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            response = client.get(
                "/api/v1/pipeline/status",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat()},
            )

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["data"]["current_state"] == "forecast_running"
        assert body["data"]["cycle_id"] == cycle_id_for("GFS", cycle_time)
        assert body["data"]["started_at"] is not None
        assert body["data"]["updated_at"] is not None
        assert body["data"]["job_counts"] == {"succeeded": 3, "failed": 1, "running": 1, "pending": 0}


def test_pipeline_stages_endpoint() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        cycle_id = cycle_id_for("GFS", cycle_time)
        _insert_cycle(store, cycle_time=cycle_time)
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            response = client.get(
                "/api/v1/pipeline/stages",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat()},
            )

        assert response.status_code == 200
        stages = response.json()["data"]
        assert [stage["stage"] for stage in stages] == [
            "download",
            "convert",
            "forcing",
            "forecast",
            "parse",
            "frequency",
            "publish",
        ]
        download = stages[0]
        assert download["display_status"] == "succeeded"
        assert download["basin_progress"] == {"completed": 2, "total": 2, "failed": 0}
        assert len(download["basin_results"]) == 2
        convert = stages[1]
        assert convert["display_status"] == "partially_failed"
        assert convert["basin_progress"] == {"completed": 1, "total": 2, "failed": 1}


def test_pipeline_stages_missing_cycle() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        with _client(store) as client:
            response = client.get(
                "/api/v1/pipeline/stages",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat()},
            )

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "PIPELINE_CYCLE_NOT_FOUND"


def test_pipeline_stages_submitted_display_status_running() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        cycle_id = cycle_id_for("GFS", cycle_time)
        _insert_cycle(store, cycle_time=cycle_time)
        _create_job(store, job_id="job_submitted", cycle_id=cycle_id, stage="forecast", status="submitted")
        with _client(store) as client:
            response = client.get(
                "/api/v1/pipeline/stages",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat()},
            )

        assert response.status_code == 200
        forecast = next(stage for stage in response.json()["data"] if stage["stage"] == "forecast")
        assert forecast["display_status"] == "running"


def test_jobs_list_endpoint() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        cycle_id = cycle_id_for("GFS", cycle_time)
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            response = client.get(
                "/api/v1/jobs",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat(), "limit": 2, "offset": 0},
            )

        assert response.status_code == 200
        page = response.json()["data"]
        jobs = page["items"]
        assert page["total"] == 5
        assert page["limit"] == 2
        assert page["offset"] == 0
        assert len(jobs) == 2
        assert jobs[0]["job_id"] == "job_running"
        assert set(jobs[0]) >= {
            "job_id",
            "run_id",
            "cycle_id",
            "job_type",
            "slurm_job_id",
            "model_id",
            "status",
            "stage",
            "submitted_at",
            "started_at",
            "finished_at",
            "exit_code",
            "retry_count",
            "error_code",
            "error_message",
            "log_uri",
            "duration_seconds",
        }


def test_jobs_list_endpoint_server_side_sorting() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        cycle_id = cycle_id_for("GFS", cycle_time)
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            submitted_response = client.get(
                "/api/v1/jobs",
                params={
                    "source": "GFS",
                    "cycle_time": cycle_time.isoformat(),
                    "sort_by": "submitted_at",
                    "sort_order": "asc",
                    "limit": 2,
                    "offset": 0,
                },
            )
            duration_response = client.get(
                "/api/v1/jobs",
                params={
                    "source": "GFS",
                    "cycle_time": cycle_time.isoformat(),
                    "sort_by": "duration_seconds",
                    "sort_order": "desc",
                    "limit": 2,
                    "offset": 0,
                },
            )

        assert submitted_response.status_code == 200
        submitted_jobs = submitted_response.json()["data"]["items"]
        assert [job["job_id"] for job in submitted_jobs] == ["job_download_a", "job_download_b"]

        assert duration_response.status_code == 200
        duration_jobs = duration_response.json()["data"]["items"]
        assert [job["job_id"] for job in duration_jobs] == ["job_download_b", "job_download_a"]


def test_jobs_list_filter_by_status() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        cycle_id = cycle_id_for("GFS", cycle_time)
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            response = client.get("/api/v1/jobs", params={"status": "failed"})

        assert response.status_code == 200
        jobs = response.json()["data"]["items"]
        assert [job["job_id"] for job in jobs] == ["job_convert_failed"]
        assert jobs[0]["error_code"] == "NODE_FAILURE"


def test_job_logs_endpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    with _store() as store:
        log_path = tmp_path / "job.log"
        log_path.write_text("line 1\nline 2\n", encoding="utf-8")
        _create_job(store, job_id="job_logs", log_uri="job.log")
        with _client(store) as client:
            response = client.get("/api/v1/jobs/job_logs/logs")

        assert response.status_code == 200
        assert response.json()["data"] == {
            "job_id": "job_logs",
            "log_uri": "job.log",
            "content": "line 1\nline 2\n",
        }


def test_job_logs_tails_large_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    monkeypatch.setattr(pipeline_routes, "_MAX_LOG_BYTES", 8)
    with _store() as store:
        log_path = tmp_path / "large.log"
        log_path.write_text("0123456789abcdef", encoding="utf-8")
        _create_job(store, job_id="job_large_logs", log_uri="large.log")
        with _client(store) as client:
            response = client.get("/api/v1/jobs/job_large_logs/logs")

        assert response.status_code == 200
        assert response.json()["data"]["content"] == "89abcdef"


def test_job_logs_path_traversal(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    with _store() as store:
        _create_job(store, job_id="job_traversal", log_uri="../../../etc/passwd")
        with _client(store) as client:
            response = client.get("/api/v1/jobs/job_traversal/logs")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"


def test_job_logs_not_found(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    with _store() as store:
        _create_job(store, job_id="job_no_log", log_uri=None)
        _create_job(store, job_id="job_missing_log", log_uri="missing.log")
        with _client(store) as client:
            no_log = client.get("/api/v1/jobs/job_no_log/logs")
            missing_log = client.get("/api/v1/jobs/job_missing_log/logs")

        assert no_log.status_code == 404
        assert no_log.json()["error"]["code"] == "JOB_LOG_NOT_FOUND"
        assert missing_log.status_code == 404
        assert missing_log.json()["error"]["code"] == "JOB_LOG_NOT_FOUND"


def test_retry_rbac() -> None:
    with _store() as store:
        _create_job(store, job_id="job_retry", run_id="run_retry", status="failed", error_code="SLURM_TIMEOUT")
        with _client(store) as client:
            allowed = client.post("/api/v1/runs/run_retry/retry", headers={"X-User-Role": "operator"})
            denied = client.post("/api/v1/runs/run_retry/retry", headers={"X-User-Role": "viewer"})

        assert allowed.status_code == 200
        assert allowed.json()["data"]["status"] == "submitted"
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "FORBIDDEN"


def test_retry_no_role_header() -> None:
    with _store() as store:
        _create_job(store, job_id="job_retry_no_role", run_id="run_retry_no_role", status="failed")
        with _client(store) as client:
            response = client.post("/api/v1/runs/run_retry_no_role/retry")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"
        assert response.json()["error"]["message"] == "Operator role required."


def test_cancel_endpoint() -> None:
    with _store() as store:
        gateway = _MockGateway()
        _create_job(store, job_id="job_cancel_pending", run_id="run_cancel", status="pending", slurm_job_id="slurm_1")
        _create_job(store, job_id="job_cancel_running", run_id="run_cancel", status="running", slurm_job_id="slurm_2")
        _create_job(store, job_id="job_cancel_done", run_id="run_cancel", status="succeeded", slurm_job_id="slurm_3")
        with _client(store, gateway) as client:
            response = client.post("/api/v1/runs/run_cancel/cancel", headers={"X-User-Role": "operator"})

        assert response.status_code == 200
        cancelled = response.json()["data"]["cancelled_jobs"]
        assert [job["job_id"] for job in cancelled] == ["job_cancel_pending", "job_cancel_running"]
        assert gateway.cancelled == ["slurm_1", "slurm_2"]
        assert store.get_job("job_cancel_pending").status == "cancelled"
        assert store.get_job("job_cancel_running").status == "cancelled"
        assert store.get_job("job_cancel_done").status == "succeeded"
        events = _events(store)
        assert [event.event_type for event in events] == ["cancel", "cancel"]
        assert {event.details["slurm_job_id"] for event in events} == {"slurm_1", "slurm_2"}


def test_cancel_rbac() -> None:
    with _store() as store:
        gateway = _MockGateway()
        _create_job(store, job_id="job_cancel_denied", run_id="run_cancel_denied", status="running")
        with _client(store, gateway) as client:
            response = client.post("/api/v1/runs/run_cancel_denied/cancel", headers={"X-User-Role": "viewer"})

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"
        assert gateway.cancelled == []
        assert store.get_job("job_cancel_denied").status == "running"


def test_cancel_no_role_header() -> None:
    with _store() as store:
        gateway = _MockGateway()
        _create_job(store, job_id="job_cancel_no_role", run_id="run_cancel_no_role", status="running")
        with _client(store, gateway) as client:
            response = client.post("/api/v1/runs/run_cancel_no_role/cancel")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"
        assert response.json()["error"]["message"] == "Operator role required."
        assert gateway.cancelled == []
        assert store.get_job("job_cancel_no_role").status == "running"


def test_metrics_stage_duration() -> None:
    with _store() as store:
        cycle_id = cycle_id_for("GFS", _cycle_time())
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            response = client.get("/api/v1/metrics/stage-duration", params={"days": 30})

        assert response.status_code == 200
        rows = response.json()["data"]
        download = next(row for row in rows if row["stage"] == "download")
        assert download["average_duration_seconds"] == 360
        assert download["job_count"] == 2


def test_metrics_days_upper_bound() -> None:
    with _store() as store:
        with _client(store) as client:
            response = client.get("/api/v1/metrics/stage-duration", params={"days": 366})

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_metrics_success_rate() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        failed_cycle = cycle_id_for("GFS", cycle_time)
        success_cycle = cycle_id_for("IFS", cycle_time)
        _seed_monitoring_jobs(store, cycle_id=failed_cycle)
        _create_job(store, job_id="job_success_1", cycle_id=success_cycle, status="succeeded")
        _create_job(store, job_id="job_success_2", cycle_id=success_cycle, status="succeeded")
        with _client(store) as client:
            response = client.get("/api/v1/metrics/success-rate", params={"days": 30})

        assert response.status_code == 200
        assert response.json()["data"] == [
            {
                "date": cycle_time.date().isoformat(),
                "success_rate": 0.5,
                "succeeded_cycles": 1,
                "total_cycles": 2,
            }
        ]


def test_metrics_filter_by_source_and_scenario() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        gfs_cycle = cycle_id_for("GFS", cycle_time)
        ifs_cycle = cycle_id_for("IFS", cycle_time)
        _seed_monitoring_jobs(store, cycle_id=gfs_cycle)
        _create_job(
            store,
            job_id="job_ifs_success",
            run_id="forecast_ifs_deterministic_run",
            cycle_id=ifs_cycle,
            stage="download",
            status="succeeded",
            submitted_at=cycle_time,
            started_at=cycle_time,
            finished_at=cycle_time + timedelta(minutes=2),
        )
        with _client(store) as client:
            stage_response = client.get(
                "/api/v1/metrics/stage-duration",
                params={"days": 30, "source": "IFS", "scenario": "forecast_ifs_deterministic"},
            )
            success_response = client.get(
                "/api/v1/metrics/success-rate",
                params={"days": 30, "source": "IFS", "scenario": "forecast_ifs_deterministic"},
            )

        assert stage_response.status_code == 200
        assert stage_response.json()["data"] == [
            {
                "date": cycle_time.date().isoformat(),
                "stage": "download",
                "average_duration_seconds": 120.0,
                "job_count": 1,
            }
        ]
        assert success_response.status_code == 200
        assert success_response.json()["data"] == [
            {
                "date": cycle_time.date().isoformat(),
                "success_rate": 1.0,
                "succeeded_cycles": 1,
                "total_cycles": 1,
            }
        ]


def test_queue_depth() -> None:
    with _store() as store:
        gateway = _MockGateway(depth={"running": 2, "pending": 3, "idle": 1})
        with _client(store, gateway) as client:
            response = client.get("/api/v1/queue/depth")

        assert response.status_code == 200
        assert response.json()["data"] == {"running": 2, "pending": 3, "idle": 1}


def test_response_wrapper() -> None:
    with _store() as store:
        gateway = _MockGateway()
        with _client(store, gateway) as client:
            response = client.get("/api/v1/queue/depth")

        assert response.status_code == 200
        assert set(response.json()) == {"request_id", "status", "data"}
        assert response.json()["status"] == "ok"
        assert response.json()["request_id"]


def test_error_response_wrapper() -> None:
    with _store() as store:
        with _client(store) as client:
            response = client.get("/api/v1/jobs/missing/logs")

        assert response.status_code == 404
        assert set(response.json()) == {"request_id", "status", "error"}
        assert response.json()["status"] == "error"
        assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


def _store() -> "_ClosingStore":
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_schemas(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS met")

    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE met.forecast_cycle (
                    cycle_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    cycle_time DATETIME NOT NULL,
                    current_state TEXT NOT NULL,
                    started_at DATETIME,
                    updated_at DATETIME,
                    created_at DATETIME
                )
                """
            )
        )
    return _ClosingStore(Session(engine))


class _ClosingStore(PipelineStore):
    def __enter__(self) -> PipelineStore:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.session.close()


class _MockGateway:
    def __init__(self, depth: dict[str, int] | None = None) -> None:
        self.cancelled: list[str] = []
        self.submissions: list[Any] = []
        self.depth = depth or {"running": 0, "pending": 0, "idle": 0}

    def submit_job(self, request: Any) -> dict[str, Any]:
        self.submissions.append(request)
        return {
            "job_id": "slurm_retry",
            "run_id": request.run_id,
            "model_id": request.model_id,
            "status": "submitted",
            "submitted_at": "2026-05-15T00:00:00Z",
            "updated_at": "2026-05-15T00:00:00Z",
        }

    def cancel_job(self, job_id: str) -> dict[str, str]:
        self.cancelled.append(job_id)
        return {"job_id": job_id, "status": "cancelled"}

    def queue_depth(self) -> dict[str, int]:
        return self.depth


class _client:
    def __init__(self, store: PipelineStore, gateway: _MockGateway | None = None) -> None:
        self.store = store
        self.gateway = gateway or _MockGateway()
        self.client: TestClient | None = None

    def __enter__(self) -> TestClient:
        app.dependency_overrides[pipeline_routes.get_pipeline_store] = lambda: self.store
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = lambda: self.gateway
        self.client = TestClient(app)
        return self.client

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        app.dependency_overrides.pop(pipeline_routes.get_pipeline_store, None)
        app.dependency_overrides.pop(pipeline_routes.get_slurm_gateway, None)
        if self.client is not None:
            self.client.close()


def _cycle_time() -> datetime:
    return (datetime.now(UTC) - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)


def _insert_cycle(
    store: PipelineStore,
    *,
    cycle_time: datetime,
    source: str = "GFS",
    current_state: str = "discovered",
) -> None:
    store.session.execute(
        text(
            """
            INSERT INTO met.forecast_cycle (
                cycle_id,
                source_id,
                cycle_time,
                current_state,
                started_at,
                updated_at,
                created_at
            )
            VALUES (
                :cycle_id,
                :source_id,
                :cycle_time,
                :current_state,
                :started_at,
                :updated_at,
                :created_at
            )
            """
        ),
        {
            "cycle_id": cycle_id_for(source, cycle_time),
            "source_id": source,
            "cycle_time": cycle_time,
            "current_state": current_state,
            "started_at": cycle_time,
            "updated_at": cycle_time + timedelta(minutes=10),
            "created_at": cycle_time,
        },
    )
    store.session.commit()


def _seed_monitoring_jobs(store: PipelineStore, *, cycle_id: str) -> None:
    base_time = _cycle_time()
    _create_job(
        store,
        job_id="job_download_a",
        run_id="run_forecast_gfs_a",
        cycle_id=cycle_id,
        stage="download",
        status="succeeded",
        model_id="model_a",
        submitted_at=base_time,
        started_at=base_time,
        finished_at=base_time + timedelta(minutes=5),
    )
    _create_job(
        store,
        job_id="job_download_b",
        run_id="run_forecast_gfs_b",
        cycle_id=cycle_id,
        stage="download",
        status="succeeded",
        model_id="model_b",
        submitted_at=base_time + timedelta(minutes=1),
        started_at=base_time + timedelta(minutes=1),
        finished_at=base_time + timedelta(minutes=8),
    )
    _create_job(
        store,
        job_id="job_convert_ok",
        run_id="run_forecast_gfs_c",
        cycle_id=cycle_id,
        stage="convert",
        status="succeeded",
        model_id="model_a",
        submitted_at=base_time + timedelta(minutes=9),
        started_at=base_time + timedelta(minutes=9),
        finished_at=base_time + timedelta(minutes=11),
    )
    _create_job(
        store,
        job_id="job_convert_failed",
        run_id="run_forecast_gfs_d",
        cycle_id=cycle_id,
        stage="convert",
        status="failed",
        model_id="model_b",
        submitted_at=base_time + timedelta(minutes=10),
        started_at=base_time + timedelta(minutes=10),
        finished_at=base_time + timedelta(minutes=12),
        error_code="NODE_FAILURE",
        error_message="Node failed during conversion.",
    )
    _create_job(
        store,
        job_id="job_running",
        run_id="run_forecast_gfs_e",
        cycle_id=cycle_id,
        stage="forecast",
        status="running",
        model_id="model_c",
        submitted_at=base_time + timedelta(minutes=13),
        started_at=base_time + timedelta(minutes=13),
        finished_at=None,
    )


def _create_job(
    store: PipelineStore,
    *,
    job_id: str,
    run_id: str = "run_1",
    cycle_id: str = "gfs_2026050100",
    job_type: str = "download_source_cycle",
    slurm_job_id: str | None = "slurm_1",
    model_id: str | None = "model_a",
    status: str = "succeeded",
    stage: str | None = "download",
    submitted_at: datetime | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    exit_code: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    log_uri: str | None = None,
) -> PipelineJob:
    submitted = submitted_at or _cycle_time()
    job = store.create_job(
        job_id=job_id,
        run_id=run_id,
        cycle_id=cycle_id,
        job_type=job_type,
        slurm_job_id=slurm_job_id,
        model_id=model_id,
        stage=stage,
        status=status,
        commit=False,
    )
    job.submitted_at = submitted
    job.started_at = started_at if started_at is not None else submitted
    if finished_at is not None:
        job.finished_at = finished_at
    elif status in {"pending", "submitted", "running"}:
        job.finished_at = None
    else:
        job.finished_at = submitted + timedelta(minutes=1)
    if exit_code is not None:
        job.exit_code = exit_code
    elif status == "failed":
        job.exit_code = 1
    elif status in {"pending", "submitted", "running"}:
        job.exit_code = None
    else:
        job.exit_code = 0
    job.error_code = error_code
    job.error_message = error_message
    job.log_uri = log_uri
    store.session.add(job)
    store.session.commit()
    store.session.refresh(job)
    return job


def _events(store: PipelineStore) -> list[PipelineEvent]:
    statement = select(PipelineEvent).order_by(PipelineEvent.event_id.asc())
    return list(store.session.scalars(statement))
