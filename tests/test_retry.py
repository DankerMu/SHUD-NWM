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


def test_should_auto_retry_poll_timeout() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_JOB_TIMEOUT")
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is True


def test_should_auto_retry_non_transient() -> None:
    with _store() as store:
        job = _create_job(store, error_code="INVALID_MANIFEST")
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is False
        assert job.status == "failed"
        assert _events(store) == []


def test_should_auto_retry_max_reached() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT", retry_count=3)
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is False
        assert job.status == "failed"
        assert _events(store) == []


def test_handle_failed_job_transient() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.handle_failed_job(job)

        store.session.refresh(job)
        assert retry.job_id == "job_1_retry_1"
        assert retry.status == "pending"
        assert retry.retry_count == 1
        assert job.status == "failed"
        assert job.retry_count == 0


def test_handle_failed_job_non_transient() -> None:
    with _store() as store:
        job = _create_job(store, error_code="INVALID_MANIFEST")
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"


def test_handle_failed_job_exhausted() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT", retry_count=3)
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"


def test_schedule_auto_retry() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.schedule_auto_retry(job)

        store.session.refresh(job)
        assert retry.job_id != job.job_id
        assert retry.job_id == "job_1_retry_1"
        assert retry.run_id == job.run_id
        assert retry.cycle_id == job.cycle_id
        assert retry.job_type == job.job_type
        assert retry.model_id == job.model_id
        assert retry.stage == job.stage
        assert retry.retry_count == 1
        assert retry.status == "pending"
        assert retry.slurm_job_id is None
        assert job.status == "failed"
        assert job.retry_count == 0
        assert job.slurm_job_id == "123"
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


def test_manual_retry_creates_new_job() -> None:
    with _store() as store:
        original = _create_job(store, run_id="run_1", error_code="NODE_FAILURE", retry_count=2)
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("run_1")

        store.session.refresh(original)
        assert retry.job_id != original.job_id
        assert retry.job_id.startswith("run_1_retry_")
        assert retry.run_id == original.run_id
        assert retry.cycle_id == original.cycle_id
        assert retry.job_type == original.job_type
        assert retry.model_id == original.model_id
        assert retry.stage == original.stage
        assert retry.status == "pending"
        assert retry.retry_count == 3
        assert retry.slurm_job_id is None
        assert retry.error_code is None
        assert original.status == "failed"
        assert original.retry_count == 2
        assert original.slurm_job_id == "123"


def test_manual_retry_conflict_409() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_1", status="failed")
        _create_job(store, job_id="job_pending", run_id="run_1", status="pending")
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryConflictError) as exc_info:
            service.attempt_manual_retry("run_1")

        assert exc_info.value.message == "A retry is already in progress for this run."
        assert exc_info.value.details["active_job_id"] == "job_pending"
        assert len(store.query_jobs_by_run("run_1")) == 2


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
            "previous_job_id": job.job_id,
            "slurm_job_id": None,
        }


def test_audit_event_manual() -> None:
    with _store() as store:
        failed = _create_job(store, run_id="run_1", error_code="SBATCH_SUBMISSION_FAILED")
        service = RetryService(store, RetryConfig(max_retries=3))

        service.attempt_manual_retry("run_1")

        event = _events(store)[0]
        assert event.details == {
            "trigger": "manual",
            "retry_count": 1,
            "previous_error": "SBATCH_SUBMISSION_FAILED",
            "previous_job_id": failed.job_id,
            "slurm_job_id": None,
        }


def test_manual_retry_audit_has_previous_job_id() -> None:
    with _store() as store:
        failed = _create_job(store, run_id="run_1", error_code="SBATCH_SUBMISSION_FAILED")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("run_1")

        event = _events(store)[0]
        assert event.entity_id == retry.job_id
        assert event.details["previous_job_id"] == failed.job_id


def test_retry_api_endpoint() -> None:
    with _store() as store:
        failed = _create_job(store, run_id="run_api", error_code="SLURM_UNAVAILABLE")
        service = RetryService(store, RetryConfig(max_retries=3))
        app.dependency_overrides[pipeline_routes.get_retry_service] = lambda: service
        try:
            client = TestClient(app)
            headers = {"X-User-Role": "operator"}

            response = client.post("/api/v1/runs/run_api/retry", headers=headers)
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
            data = response.json()["data"]
            assert data == {
                "job_id": data["job_id"],
                "pipeline_job_id": data["job_id"],
                "run_id": "run_api",
                "retry_count": 1,
                "status": "pending",
                "slurm_job_id": None,
                "execution_status": "queued",
            }
            assert data["job_id"].startswith("run_api_retry_")
            store.session.refresh(failed)
            assert failed.status == "failed"

            conflict = client.post("/api/v1/runs/run_api/retry", headers=headers)
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "RETRY_CONFLICT"
            assert conflict.json()["error"]["message"] == "A retry is already in progress for this run."
            assert conflict.json()["error"]["details"]["run_id"] == "run_api"
            assert "active_job_id" in conflict.json()["error"]["details"]

            missing = client.post("/api/v1/runs/missing/retry", headers=headers)
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "RETRY_NOT_FOUND"
            assert missing.json()["error"]["message"] == "No retryable failure found for this run."
            assert missing.json()["error"]["details"]["run_id"] == "missing"

            invalid = client.post("/api/v1/runs/-bad/retry", headers=headers)
            assert invalid.status_code == 400
            assert invalid.json()["error"]["code"] == "INVALID_RUN_ID"
        finally:
            app.dependency_overrides.pop(pipeline_routes.get_retry_service, None)


def test_permanently_failed_override() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_1", status="failed")

        updated = store.update_job_status("job_failed", "permanently_failed")

        assert updated.status == "permanently_failed"


def test_permanently_failed_is_sticky() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_1", status="permanently_failed")

        partial = store.update_job_status("job_failed", "partially_failed")
        running = store.update_job_status("job_failed", "running")

        assert partial.status == "permanently_failed"
        assert running.status == "permanently_failed"
        assert store.get_job("job_failed").status == "permanently_failed"


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
