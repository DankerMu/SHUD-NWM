from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.main import app
from apps.api.routes import pipeline as pipeline_routes
from services.orchestrator.persistence import Base, PipelineEvent, PipelineJob, PipelineStore
from services.orchestrator.retry import RetryConfig, RetryNotFoundError, RetryService
from services.slurm_gateway.gateway import SlurmGatewayError

HYDRO_RUN_STATUS_ENUM = {
    "created",
    "staged",
    "pending",
    "submitted",
    "running",
    "succeeded",
    "parsed",
    "frequency_done",
    "published",
    "failed",
    "cancelled",
    "superseded",
}

MET_CYCLE_STATUS_ENUM = {
    "discovered",
    "downloading",
    "raw_complete",
    "canonical_ready",
    "forcing_ready_partial",
    "forcing_ready",
    "forecast_running",
    "parsed_partial",
    "complete",
    "published",
    "failed_download",
    "failed_convert",
    "failed_forcing",
    "failed_run",
    "failed_parse",
    "failed_publish",
    "cancelled",
}

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_characterization_manual_retry_writes_pending_to_hydro_run() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_retry", status="failed")
        _create_job(store, job_id="job_failed", run_id="run_retry", status="failed", error_code="NODE_FAILURE")
        gateway = _MockGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("run_retry", gateway=gateway)

        assert retry.status == "submitted"
        assert _hydro_status(store, "run_retry") == "pending"
        assert store.get_job(retry.job_id).status == "submitted"


def test_manual_retry_response_includes_execution_status() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_retry_api", status="failed")
        _create_job(store, job_id="job_failed", run_id="run_retry_api", status="failed", error_code="NODE_FAILURE")
        service = RetryService(store, RetryConfig(max_retries=3))
        with _client(store, retry_service=service) as client:
            response = client.post("/api/v1/runs/run_retry_api/retry", headers=_operator_headers())

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["job_id"] == data["pipeline_job_id"]
        assert data["slurm_job_id"] == "slurm_retry"
        assert data["execution_status"] == "submitted"
        assert data["status"] == "submitted"


def test_duplicate_retry_returns_conflict() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_conflict", status="failed")
        _create_job(store, job_id="job_pending", run_id="run_conflict", status="pending")
        service = RetryService(store, RetryConfig(max_retries=3))
        with _client(store, retry_service=service) as client:
            response = client.post("/api/v1/runs/run_conflict/retry", headers=_operator_headers())

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RETRY_CONFLICT"
        assert response.json()["error"]["details"]["active_job_id"] == "job_pending"


def test_retry_event_records_trigger_and_previous_error() -> None:
    with _store() as store:
        failed = _create_job(
            store,
            job_id="job_failed",
            run_id="run_retry_event",
            status="failed",
            error_code="SBATCH_SUBMISSION_FAILED",
        )
        gateway = _MockGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("run_retry_event", gateway=gateway)

        event = _events(store)[0]
        assert event.entity_id == retry.job_id
        assert event.details["trigger"] == "manual"
        assert event.details["previous_job_id"] == failed.job_id
        assert event.details["previous_error"] == "SBATCH_SUBMISSION_FAILED"
        assert event.details["slurm_job_id"] is None


def test_characterization_cancel_writes_cancelled_to_forecast_cycle() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_cancel", status="running")
        _insert_forecast_cycle(store, "cycle_cancel", status="forecast_running")
        _create_job(store, job_id="job_running", run_id="run_cancel", cycle_id="cycle_cancel", status="running")
        gateway = _MockGateway()
        with _client(store, gateway=gateway) as client:
            response = client.post("/api/v1/runs/run_cancel/cancel", headers=_operator_headers())

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["partial_failure"] is False
        assert store.get_job("job_running").status == "cancelled"
        assert _hydro_status(store, "run_cancel") == "cancelled"
        # Characterization: before enum remediation, SQLite allowed this value even though PostgreSQL did not.
        assert _cycle_status(store, "cycle_cancel") == "cancelled"


def test_cancel_preserves_terminal_forecast_cycle() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_published", status="running")
        _insert_forecast_cycle(store, "cycle_published", status="published")
        _create_job(store, job_id="job_running", run_id="run_published", cycle_id="cycle_published", status="running")
        with _client(store, gateway=_MockGateway()) as client:
            response = client.post("/api/v1/runs/run_published/cancel", headers=_operator_headers())

        assert response.status_code == 200
        assert _hydro_status(store, "run_published") == "cancelled"
        assert _cycle_status(store, "cycle_published") == "published"
        assert _cycle_status(store, "cycle_published") in MET_CYCLE_STATUS_ENUM
        assert response.json()["data"]["forecast_cycle"]["preserved"] is True


def test_partial_slurm_cancel_reflected_in_response() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_partial", status="running")
        _insert_forecast_cycle(store, "cycle_partial", status="forecast_running")
        _create_job(
            store,
            job_id="job_cancel_ok",
            run_id="run_partial",
            cycle_id="cycle_partial",
            status="running",
            slurm_job_id="slurm_ok",
        )
        _create_job(
            store,
            job_id="job_cancel_fail",
            run_id="run_partial",
            cycle_id="cycle_partial",
            status="running",
            slurm_job_id="slurm_fail",
        )
        gateway = _MockGateway(failures={"slurm_fail": SlurmGatewayError(502, "SLURM_ERROR", "scancel failed")})
        with _client(store, gateway=gateway) as client:
            response = client.post("/api/v1/runs/run_partial/cancel", headers=_operator_headers())

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["partial_failure"] is True
        assert [job["job_id"] for job in data["cancelled_jobs"]] == ["job_cancel_ok"]
        assert data["failed_jobs"][0]["job_id"] == "job_cancel_fail"
        assert store.get_job("job_cancel_ok").status == "cancelled"
        assert store.get_job("job_cancel_fail").status == "running"
        assert _hydro_status(store, "run_partial") == "running"
        assert _cycle_status(store, "cycle_partial") == "forecast_running"
        assert data["hydro_run"] is None
        assert data["forecast_cycle"] is None


def test_cancel_idempotent_for_terminal_slurm_job() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_terminal_slurm", status="running")
        _insert_forecast_cycle(store, "cycle_terminal_slurm", status="forecast_running")
        _create_job(
            store,
            job_id="job_terminal_slurm",
            run_id="run_terminal_slurm",
            cycle_id="cycle_terminal_slurm",
            status="running",
            slurm_job_id="slurm_missing",
        )
        gateway = _MockGateway(
            failures={
                "slurm_missing": SlurmGatewayError(
                    404,
                    "JOB_NOT_FOUND",
                    "Invalid job id specified or job not found.",
                )
            }
        )
        with _client(store, gateway=gateway) as client:
            response = client.post("/api/v1/runs/run_terminal_slurm/cancel", headers=_operator_headers())

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["partial_failure"] is False
        assert data["idempotent_jobs"][0]["job_id"] == "job_terminal_slurm"
        assert store.get_job("job_terminal_slurm").status == "cancelled"
        assert _hydro_status(store, "run_terminal_slurm") == "cancelled"


def test_cancel_event_records_slurm_context() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_event", status="running")
        _insert_forecast_cycle(store, "cycle_event", status="forecast_running")
        _create_job(
            store,
            job_id="job_event",
            run_id="run_event",
            cycle_id="cycle_event",
            status="submitted",
            slurm_job_id="slurm_event",
        )
        with _client(store, gateway=_MockGateway()) as client:
            response = client.post("/api/v1/runs/run_event/cancel", headers=_operator_headers())

        assert response.status_code == 200
        event = next(event for event in _events(store) if event.entity_type == "pipeline_job")
        assert event.details["run_id"] == "run_event"
        assert event.details["slurm_job_id"] == "slurm_event"
        assert event.details["previous_status"] == "submitted"


def test_cancelled_run_does_not_block_active_guard() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_guard", status="running", source_id="gfs", cycle_time=_cycle_time())
        _insert_forecast_cycle(store, "cycle_guard", status="forecast_running")
        _create_job(store, job_id="job_guard", run_id="run_guard", cycle_id="cycle_guard", status="running")
        assert _has_active_pipeline(store, source_id="gfs", cycle_time=_cycle_time(), model_id="model_a") is True

        with _client(store, gateway=_MockGateway()) as client:
            response = client.post("/api/v1/runs/run_guard/cancel", headers=_operator_headers())

        assert response.status_code == 200
        assert _has_active_pipeline(store, source_id="gfs", cycle_time=_cycle_time(), model_id="model_a") is False


def test_retry_status_is_valid_hydro_enum() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_retry_valid_enum", status="failed")
        _create_job(
            store,
            job_id="job_retry_valid_enum",
            run_id="run_retry_valid_enum",
            status="failed",
            error_code="NODE_FAILURE",
        )
        gateway = _MockGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        service.attempt_manual_retry("run_retry_valid_enum", gateway=gateway)

        assert _hydro_status(store, "run_retry_valid_enum") == "pending"
        assert _hydro_status(store, "run_retry_valid_enum") in HYDRO_RUN_STATUS_ENUM


def test_cancel_status_is_valid_cycle_enum() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_cancel_valid_cycle", status="running")
        _insert_forecast_cycle(store, "cycle_cancel_valid_cycle", status="forecast_running")
        _create_job(
            store,
            job_id="job_cancel_valid_cycle",
            run_id="run_cancel_valid_cycle",
            cycle_id="cycle_cancel_valid_cycle",
            status="running",
        )
        with _client(store, gateway=_MockGateway()) as client:
            response = client.post("/api/v1/runs/run_cancel_valid_cycle/cancel", headers=_operator_headers())

        assert response.status_code == 200
        assert _cycle_status(store, "cycle_cancel_valid_cycle") in MET_CYCLE_STATUS_ENUM
        assert response.json()["data"]["forecast_cycle"]["status"] in MET_CYCLE_STATUS_ENUM


def test_cancel_status_is_valid_hydro_enum() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_cancel_valid_hydro", status="running")
        _insert_forecast_cycle(store, "cycle_cancel_valid_hydro", status="forecast_running")
        _create_job(
            store,
            job_id="job_cancel_valid_hydro",
            run_id="run_cancel_valid_hydro",
            cycle_id="cycle_cancel_valid_hydro",
            status="running",
        )
        with _client(store, gateway=_MockGateway()) as client:
            response = client.post("/api/v1/runs/run_cancel_valid_hydro/cancel", headers=_operator_headers())

        assert response.status_code == 200
        assert _hydro_status(store, "run_cancel_valid_hydro") == "cancelled"
        assert _hydro_status(store, "run_cancel_valid_hydro") in HYDRO_RUN_STATUS_ENUM


def test_retry_preserves_terminal_hydro_status() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_retry_terminal", status="published")
        _create_job(
            store,
            job_id="job_retry_terminal",
            run_id="run_retry_terminal",
            status="failed",
            error_code="NODE_FAILURE",
        )
        gateway = _MockGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("run_retry_terminal", gateway=gateway)

        assert retry.status == "submitted"
        assert _hydro_status(store, "run_retry_terminal") == "published"
        assert _hydro_status(store, "run_retry_terminal") in HYDRO_RUN_STATUS_ENUM


def test_cancel_preserves_terminal_hydro_run() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_cancel_terminal_hydro", status="published")
        _insert_forecast_cycle(store, "cycle_cancel_terminal_hydro", status="forecast_running")
        _create_job(
            store,
            job_id="job_cancel_terminal_hydro",
            run_id="run_cancel_terminal_hydro",
            cycle_id="cycle_cancel_terminal_hydro",
            status="running",
        )
        with _client(store, gateway=_MockGateway()) as client:
            response = client.post("/api/v1/runs/run_cancel_terminal_hydro/cancel", headers=_operator_headers())

        assert response.status_code == 200
        assert _hydro_status(store, "run_cancel_terminal_hydro") == "published"
        assert response.json()["data"]["hydro_run"]["preserved"] is True


def test_retry_nonexistent_run_raises_not_found_without_enum_write() -> None:
    with _store() as store:
        gateway = _MockGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryNotFoundError):
            service.attempt_manual_retry("run_missing", gateway=gateway)

        assert _events(store) == []


def test_enum_sets_match_migration() -> None:
    assert _migration_enum_values("hydro.run_status") == HYDRO_RUN_STATUS_ENUM
    assert _migration_enum_values("met.cycle_status") == MET_CYCLE_STATUS_ENUM


class _ClosingStore(PipelineStore):
    def __enter__(self) -> PipelineStore:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.session.close()


class _MockGateway:
    def __init__(self, failures: dict[str, SlurmGatewayError] | None = None) -> None:
        self.cancelled: list[str] = []
        self.submissions: list[Any] = []
        self.failures = failures or {}

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
        if job_id in self.failures:
            raise self.failures[job_id]
        self.cancelled.append(job_id)
        return {"job_id": job_id, "status": "cancelled"}


class _client:
    def __init__(
        self,
        store: PipelineStore,
        *,
        gateway: _MockGateway | None = None,
        retry_service: RetryService | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway or _MockGateway()
        self.retry_service = retry_service
        self.client: TestClient | None = None

    def __enter__(self) -> TestClient:
        app.dependency_overrides[pipeline_routes.get_pipeline_store] = lambda: self.store
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = lambda: self.gateway
        if self.retry_service is not None:
            app.dependency_overrides[pipeline_routes.get_retry_service] = lambda: self.retry_service
        self.client = TestClient(app)
        return self.client

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        app.dependency_overrides.pop(pipeline_routes.get_pipeline_store, None)
        app.dependency_overrides.pop(pipeline_routes.get_slurm_gateway, None)
        app.dependency_overrides.pop(pipeline_routes.get_retry_service, None)
        if self.client is not None:
            self.client.close()


def _store() -> _ClosingStore:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_schemas(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS hydro")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS met")

    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE hydro.hydro_run (
                    run_id TEXT PRIMARY KEY,
                    source_id TEXT,
                    cycle_time DATETIME,
                    model_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_code TEXT,
                    error_message TEXT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE met.forecast_cycle (
                    cycle_id TEXT PRIMARY KEY,
                    current_state TEXT NOT NULL,
                    updated_at DATETIME
                )
                """
            )
        )
    return _ClosingStore(Session(engine))


def _create_job(
    store: PipelineStore,
    *,
    job_id: str,
    run_id: str,
    cycle_id: str = "cycle_1",
    status: str,
    slurm_job_id: str | None = "slurm_1",
    error_code: str | None = None,
) -> PipelineJob:
    job = store.create_job(
        job_id=job_id,
        run_id=run_id,
        cycle_id=cycle_id,
        job_type="download_source_cycle",
        slurm_job_id=slurm_job_id,
        model_id="model_a",
        stage="download",
        status=status,
        commit=False,
    )
    job.error_code = error_code
    job.error_message = f"{error_code} failed" if error_code else None
    job.started_at = datetime.now(UTC) if status in {"running", "submitted"} else None
    store.session.add(job)
    store.session.commit()
    store.session.refresh(job)
    return job


def _insert_hydro_run(
    store: PipelineStore,
    run_id: str,
    *,
    status: str,
    source_id: str = "gfs",
    cycle_time: datetime | None = None,
    model_id: str = "model_a",
) -> None:
    store.session.execute(
        text(
            """
            INSERT INTO hydro.hydro_run (run_id, source_id, cycle_time, model_id, status)
            VALUES (:run_id, :source_id, :cycle_time, :model_id, :status)
            """
        ),
        {
            "run_id": run_id,
            "source_id": source_id,
            "cycle_time": cycle_time or _cycle_time(),
            "model_id": model_id,
            "status": status,
        },
    )
    store.session.commit()


def _insert_forecast_cycle(store: PipelineStore, cycle_id: str, *, status: str) -> None:
    store.session.execute(
        text(
            """
            INSERT INTO met.forecast_cycle (cycle_id, current_state, updated_at)
            VALUES (:cycle_id, :status, :updated_at)
            """
        ),
        {"cycle_id": cycle_id, "status": status, "updated_at": datetime.now(UTC)},
    )
    store.session.commit()


def _hydro_status(store: PipelineStore, run_id: str) -> str:
    return str(
        store.session.execute(
            text("SELECT status FROM hydro.hydro_run WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar_one()
    )


def _cycle_status(store: PipelineStore, cycle_id: str) -> str:
    return str(
        store.session.execute(
            text("SELECT current_state FROM met.forecast_cycle WHERE cycle_id = :cycle_id"),
            {"cycle_id": cycle_id},
        ).scalar_one()
    )


def _has_active_pipeline(store: PipelineStore, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
    active_hydro_statuses = {"created", "staged", "submitted", "running", "succeeded"}
    terminal_job_statuses = {
        "succeeded",
        "partially_failed",
        "failed",
        "cancelled",
        "submission_failed",
        "permanently_failed",
    }
    rows = store.session.execute(
        text(
            """
            SELECT h.status AS hydro_status, pj.status AS job_status
            FROM hydro.hydro_run h
            LEFT JOIN ops.pipeline_job pj ON pj.run_id = h.run_id
            WHERE h.source_id = :source_id
              AND h.cycle_time = :cycle_time
              AND h.model_id = :model_id
            """
        ),
        {"source_id": source_id, "cycle_time": cycle_time, "model_id": model_id},
    ).mappings()
    return any(
        row["hydro_status"] in active_hydro_statuses or (row["job_status"] or "pending") not in terminal_job_statuses
        for row in rows
    )


def _events(store: PipelineStore) -> list[PipelineEvent]:
    statement = select(PipelineEvent).order_by(PipelineEvent.event_id.asc())
    return list(store.session.scalars(statement))


def _migration_enum_values(type_name: str) -> set[str]:
    migration_text = "\n".join(
        (_REPO_ROOT / path).read_text(encoding="utf-8")
        for path in ("db/migrations/000003_enums.sql", "db/migrations/000013_enum_remediation.sql")
    )
    create_match = re.search(
        rf"CREATE TYPE {re.escape(type_name)} AS ENUM\s*\((?P<body>.*?)\);",
        migration_text,
        flags=re.DOTALL,
    )
    if create_match is None:
        raise AssertionError(f"Missing CREATE TYPE for {type_name}")

    values = set(re.findall(r"'([^']+)'", create_match.group("body")))
    alter_pattern = rf"ALTER TYPE {re.escape(type_name)} ADD VALUE IF NOT EXISTS '([^']+)'"
    values.update(re.findall(alter_pattern, migration_text))
    return values


def _operator_headers() -> dict[str, str]:
    return {"X-User-Role": "operator"}


def _cycle_time() -> datetime:
    return datetime(2026, 5, 1, tzinfo=UTC) + timedelta(hours=0)
