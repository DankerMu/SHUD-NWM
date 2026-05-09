from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.main import app
from apps.api.routes import pipeline as pipeline_routes
from services.orchestrator.persistence import Base, PipelineEvent, PipelineJob, PipelineStore
from services.orchestrator.retry import (
    NON_TRANSIENT_ERROR_CODES,
    TRANSIENT_ERROR_CODES,
    RetryConfig,
    RetryConflictError,
    RetryNotFoundError,
    RetryService,
    compute_backoff_seconds,
    is_transient_error,
)


def test_transient_error_classification() -> None:
    for error_code in TRANSIENT_ERROR_CODES:
        assert is_transient_error(error_code) is True
    for error_code in NON_TRANSIENT_ERROR_CODES:
        assert is_transient_error(error_code) is False
    assert is_transient_error("UNKNOWN_ERROR") is False
    assert is_transient_error(None) is False


def test_backoff_calculation() -> None:
    assert compute_backoff_seconds(0) == 60
    assert compute_backoff_seconds(1) == 300
    assert compute_backoff_seconds(2) == 900
    assert compute_backoff_seconds(3) == 900
    assert compute_backoff_seconds(0, [5, 10]) == 5
    assert compute_backoff_seconds(4, [5, 10]) == 10


def test_should_auto_retry_transient() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT")
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is True


def test_should_auto_retry_non_transient() -> None:
    with _store() as store:
        job = _create_job(store, error_code="INVALID_MANIFEST")
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is False
        assert job.status == "failed"


def test_should_auto_retry_max_reached() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT", retry_count=3)
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is False
        assert job.status == "permanently_failed"


def test_schedule_auto_retry() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT")
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.schedule_auto_retry(job)

        assert updated.retry_count == 1
        assert updated.status == "pending"
        event = _events(store)[0]
        assert event.event_type == "retry"
        assert event.status_from == "failed"
        assert event.status_to == "pending"


def test_mark_permanently_failed() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT", retry_count=3)
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.mark_permanently_failed(job)

        assert updated.status == "permanently_failed"
        event = _events(store)[0]
        assert event.event_type == "permanently_failed"
        assert event.status_from == "failed"
        assert event.status_to == "permanently_failed"
        assert event.details == {"final_retry_count": 3, "last_error": "SLURM_TIMEOUT"}


def test_manual_retry_success() -> None:
    with _store() as store:
        _create_job(store, run_id="run_1", error_code="NODE_FAILURE")
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.attempt_manual_retry("run_1")

        assert updated.status == "pending"
        assert updated.retry_count == 1
        assert updated.error_code == "NODE_FAILURE"


def test_manual_retry_conflict_409() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_1", status="failed")
        _create_job(store, job_id="job_pending", run_id="run_1", status="pending")
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryConflictError):
            service.attempt_manual_retry("run_1")


def test_manual_retry_no_failed_job() -> None:
    with _store() as store:
        _create_job(store, run_id="run_1", status="succeeded", error_code=None)
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryNotFoundError):
            service.attempt_manual_retry("run_1")


def test_audit_event_auto() -> None:
    with _store() as store:
        job = _create_job(store, error_code="STORAGE_WRITE_FAILED")
        service = RetryService(store, RetryConfig(max_retries=3))

        service.schedule_auto_retry(job)

        event = _events(store)[0]
        assert event.details == {
            "trigger": "auto",
            "retry_count": 1,
            "previous_error": "STORAGE_WRITE_FAILED",
            "backoff_seconds": 60,
        }


def test_audit_event_manual() -> None:
    with _store() as store:
        _create_job(store, run_id="run_1", error_code="SBATCH_SUBMISSION_FAILED")
        service = RetryService(store, RetryConfig(max_retries=3))

        service.attempt_manual_retry("run_1")

        event = _events(store)[0]
        assert event.details == {
            "trigger": "manual",
            "retry_count": 1,
            "previous_error": "SBATCH_SUBMISSION_FAILED",
        }


def test_retry_api_endpoint() -> None:
    with _store() as store:
        _create_job(store, run_id="run_api", error_code="SLURM_UNAVAILABLE")
        service = RetryService(store, RetryConfig(max_retries=3))
        app.dependency_overrides[pipeline_routes.get_retry_service] = lambda: service
        try:
            client = TestClient(app)

            response = client.post("/api/v1/runs/run_api/retry")
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
            assert response.json()["data"] == {
                "job_id": "job_1",
                "run_id": "run_api",
                "retry_count": 1,
                "status": "pending",
            }

            conflict = client.post("/api/v1/runs/run_api/retry")
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "RETRY_CONFLICT"

            missing = client.post("/api/v1/runs/missing/retry")
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "RETRY_NOT_FOUND"
        finally:
            app.dependency_overrides.pop(pipeline_routes.get_retry_service, None)


def _store() -> "_ClosingStore":
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_ops_schema(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")

    Base.metadata.create_all(engine)
    return _ClosingStore(Session(engine))


class _ClosingStore(PipelineStore):
    def __init__(self, session: Session) -> None:
        super().__init__(session)

    def __enter__(self) -> PipelineStore:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.session.close()


def _create_job(
    store: PipelineStore,
    *,
    job_id: str = "job_1",
    run_id: str = "run_1",
    status: str = "failed",
    error_code: str | None = "SLURM_TIMEOUT",
    retry_count: int = 0,
) -> PipelineJob:
    job = store.create_job(
        job_id=job_id,
        run_id=run_id,
        cycle_id="gfs_2026050100",
        job_type="download_source_cycle",
        slurm_job_id="123",
        model_id="model_a",
        stage="download",
        status=status,
    )
    job.error_code = error_code
    job.error_message = f"{error_code} failed" if error_code else None
    job.retry_count = retry_count
    store.session.add(job)
    store.session.commit()
    store.session.refresh(job)
    return job


def _events(store: PipelineStore) -> list[PipelineEvent]:
    statement = select(PipelineEvent).order_by(PipelineEvent.event_id.asc())
    return list(store.session.scalars(statement))
