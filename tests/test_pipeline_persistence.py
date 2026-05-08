from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from services.orchestrator.persistence import Base, PipelineStore


def test_pipeline_job_crud_with_sqlite_ops_schema() -> None:
    with _store() as store:
        job = store.create_job(
            job_id="job_1",
            run_id="run_1",
            cycle_id="gfs_2026050100",
            job_type="download_source_cycle",
            slurm_job_id="123",
            model_id="model_a",
            stage="download",
            status="pending",
        )

        assert job.job_id == "job_1"
        assert job.status == "pending"
        assert job.model_id == "model_a"

        updated = store.update_job_status(
            "job_1",
            "succeeded",
            started_at=datetime(2026, 5, 1, 0, 1, tzinfo=UTC),
            finished_at=datetime(2026, 5, 1, 0, 2, tzinfo=UTC),
            exit_code=0,
            log_uri="runs/run_1/logs/download.log",
        )

        assert updated.status == "succeeded"
        assert updated.exit_code == 0
        assert updated.log_uri == "runs/run_1/logs/download.log"
        assert store.get_job("job_1") is not None


def test_pipeline_event_append_and_bidirectional_queries() -> None:
    with _store() as store:
        store.create_job(
            job_id="job_download",
            run_id="run_1",
            cycle_id="gfs_2026050100",
            job_type="download_source_cycle",
            slurm_job_id="123",
            model_id=None,
            stage="download",
            status="succeeded",
        )
        store.create_job(
            job_id="job_forecast",
            run_id="run_1",
            cycle_id="gfs_2026050100",
            job_type="run_shud_forecast_array",
            slurm_job_id="124",
            model_id="model_a",
            stage="forecast",
            status="running",
        )

        event = store.insert_event(
            entity_type="pipeline_job",
            entity_id="job_forecast",
            event_type="status_change",
            status_from="pending",
            status_to="running",
            message="forecast started",
            details={"slurm_job_id": "124"},
        )

        assert event.event_id == 1
        assert event.details == {"slurm_job_id": "124"}
        assert store.query_jobs_by_slurm_id("124").job_id == "job_forecast"
        assert [job.job_id for job in store.query_jobs_by_run("run_1")] == ["job_download", "job_forecast"]
        assert [job.job_id for job in store.query_jobs_by_cycle("gfs_2026050100")] == [
            "job_download",
            "job_forecast",
        ]


def _store() -> "_ClosingStore":
    engine = create_engine("sqlite:///:memory:", future=True)

    @event.listens_for(engine, "connect")
    def _attach_ops_schema(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")

    Base.metadata.create_all(engine)
    session = Session(engine)
    return _ClosingStore(session)


class _ClosingStore(PipelineStore):
    def __init__(self, session: Session) -> None:
        super().__init__(session)

    def __enter__(self) -> PipelineStore:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.session.close()
