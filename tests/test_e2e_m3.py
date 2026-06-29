from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.main import app
from apps.api.routes import pipeline as pipeline_routes
from packages.common.object_store import LocalObjectStore
from services.orchestrator import chain as chain_module
from services.orchestrator.chain import M3_STAGES, ForecastOrchestrator, OrchestratorConfig
from services.orchestrator.persistence import Base, PipelineEvent, PipelineJob, PipelineStore
from services.orchestrator.retry import RetryConfig, RetryService
from tests.test_orchestration_chain import FakeCycleSlurmClient
from workers.data_adapters.base import cycle_id_for, format_cycle_time


def test_e2e_m3_complete_cycle_monitoring_api(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(chain_module.time, "sleep", lambda _seconds: None)
    with _store() as store:
        repository = _SqliteCycleRepository(store)
        slurm = FakeCycleSlurmClient()
        cycle_time = _dt("2026-05-01T00:00:00Z")
        orchestrator = _orchestrator(tmp_path, repository, slurm)

        result = orchestrator.orchestrate_cycle("GFS", cycle_time, _basins(3))

        assert result.status == "complete"
        assert [submission["stage"] for submission in slurm.submissions] == [stage.stage for stage in M3_STAGES]
        assert [stage.status for stage in result.stages] == ["succeeded"] * len(M3_STAGES)

        with _client(store) as client:
            stages_response = client.get(
                "/api/v1/pipeline/stages",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat()},
            )
            jobs_response = client.get(
                "/api/v1/jobs",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat(), "limit": 50},
            )

        assert stages_response.status_code == 200
        stages = stages_response.json()["data"]
        assert [stage["stage"] for stage in stages] == ["download", *(stage.stage for stage in M3_STAGES)]
        stage_statuses = {stage["stage"]: stage["display_status"] for stage in stages}
        assert stage_statuses["download"] == "pending"
        assert {stage_statuses[stage.stage] for stage in M3_STAGES} == {"succeeded"}

        assert jobs_response.status_code == 200
        jobs = jobs_response.json()["data"]["items"]
        assert jobs_response.json()["data"]["total"] == len(M3_STAGES)
        assert {job["status"] for job in jobs} == {"succeeded"}
        assert {job["run_type"] for job in jobs} == {"forecast"}
        assert {job["scenario"] for job in jobs} == {"forecast_gfs_deterministic"}


def test_e2e_m3_partial_success_continues_to_publish(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(chain_module.time, "sleep", lambda _seconds: None)
    with _store() as store:
        repository = _SqliteCycleRepository(store)
        slurm = FakeCycleSlurmClient(array_results_by_stage={"forcing": ["succeeded", "failed", "succeeded"]})
        cycle_time = _dt("2026-05-01T06:00:00Z")
        orchestrator = _orchestrator(tmp_path, repository, slurm)

        result = orchestrator.orchestrate_cycle("GFS", cycle_time, _basins(3))

        forecast_submission = next(submission for submission in slurm.submissions if submission["stage"] == "forecast")
        publish_submission = slurm.submissions[-1]
        assert result.status == "parsed_partial"
        assert "forcing_ready_partial" in repository.cycle_statuses
        assert repository.cycle_statuses[-1] == "parsed_partial"
        assert [task["model_id"] for task in forecast_submission["tasks"]] == ["model_0", "model_2"]
        assert publish_submission["metadata"]["published_basins"] == 2
        assert publish_submission["metadata"]["excluded_basins"] == ["basin_1"]

        with _client(store) as client:
            status_response = client.get(
                "/api/v1/pipeline/status",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat()},
            )
            stages_response = client.get(
                "/api/v1/pipeline/stages",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat()},
            )

        assert status_response.status_code == 200
        assert status_response.json()["data"]["current_state"] == "parsed_partial"
        stages = stages_response.json()["data"]
        forcing = next(stage for stage in stages if stage["stage"] == "forcing")
        publish = next(stage for stage in stages if stage["stage"] == "publish")
        assert forcing["display_status"] == "partially_failed"
        assert publish["display_status"] == "succeeded"


def test_e2e_m3_retry_chain_and_manual_conflict(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(chain_module.time, "sleep", lambda _seconds: None)
    with _store() as store:
        repository = _SqliteCycleRepository(store)
        slurm = FakeCycleSlurmClient(
            failures_before_success_by_stage={"convert": 1},
            error_code_by_stage={"convert": "SLURM_TIMEOUT"},
        )
        retry_service = RetryService(store, RetryConfig(max_retries=1, backoff_schedule=[0]))
        cycle_time = _dt("2026-05-01T12:00:00Z")
        orchestrator = _orchestrator(tmp_path, repository, slurm, retry_service=retry_service)

        result = orchestrator.orchestrate_cycle("GFS", cycle_time, _basins(2))

        assert result.status == "complete"
        retry_jobs = [job for job in store.query_jobs_by_run(result.run_id) if job.retry_count == 1]
        assert len(retry_jobs) == 1
        assert retry_jobs[0].stage == "convert"
        assert retry_jobs[0].status == "succeeded"

        store.create_job(
            job_id="manual_failed",
            run_id="manual_conflict",
            cycle_id=result.cycle_id,
            job_type="convert_canonical",
            slurm_job_id="manual_1",
            model_id="model_0",
            stage="convert",
            status="failed",
        )
        store.create_job(
            job_id="manual_pending",
            run_id="manual_conflict",
            cycle_id=result.cycle_id,
            job_type="convert_canonical",
            slurm_job_id=None,
            model_id="model_0",
            stage="convert",
            status="pending",
        )

        with _client(store) as client:
            response = client.post("/api/v1/runs/manual_conflict/retry", headers={"X-User-Role": "operator"})

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RETRY_CONFLICT"

        retry_events = list(
            store.session.scalars(
                select(PipelineEvent).where(PipelineEvent.event_type == "retry").order_by(PipelineEvent.event_id.asc())
            )
        )
        assert any(
            event.details.get("trigger") == "auto" and event.details.get("retry_count") == 1 for event in retry_events
        )


def _orchestrator(
    tmp_path: Path,
    repository: "_SqliteCycleRepository",
    slurm: FakeCycleSlurmClient,
    *,
    retry_service: RetryService | None = None,
) -> ForecastOrchestrator:
    object_root = tmp_path / "object-store"
    config = OrchestratorConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        poll_interval_seconds=0,
        job_timeout_seconds=5,
    )
    return ForecastOrchestrator(
        config=config,
        repository=repository,
        slurm_client=slurm,
        object_store=LocalObjectStore(object_root, "s3://nhms"),
        retry_service=retry_service,
    )


class _SqliteCycleRepository:
    def __init__(self, store: PipelineStore) -> None:
        self.store = store
        self.cycle_statuses: list[str] = []

    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        del source_id, cycle_time
        return False

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        del source_id, cycle_time, model_id
        return False

    def has_active_analysis_run(self, *, model_id: str, start_time: datetime, end_time: datetime) -> bool:
        del model_id, start_time, end_time
        return False

    def ensure_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any]:
        cycle_id = cycle_id_for(source_id, cycle_time)
        now = datetime.now(UTC)
        self.store.session.execute(
            text(
                """
                INSERT OR IGNORE INTO met.forecast_cycle (
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
                    'discovered',
                    :started_at,
                    :updated_at,
                    :created_at
                )
                """
            ),
            {
                "cycle_id": cycle_id,
                "source_id": source_id,
                "cycle_time": cycle_time,
                "started_at": cycle_time,
                "updated_at": now,
                "created_at": now,
            },
        )
        self._ensure_hydro_run(source_id=source_id, cycle_time=cycle_time)
        self.store.session.commit()
        return {"cycle_id": cycle_id, "source_id": source_id, "cycle_time": cycle_time, "current_state": "discovered"}

    def update_forecast_cycle_status(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        del error_code, error_message
        self.cycle_statuses.append(status)
        cycle_id = cycle_id_for(source_id, cycle_time)
        self.store.session.execute(
            text(
                """
                UPDATE met.forecast_cycle
                SET current_state = :status,
                    updated_at = :updated_at
                WHERE cycle_id = :cycle_id
                """
            ),
            {"status": status, "updated_at": datetime.now(UTC), "cycle_id": cycle_id},
        )
        self.store.session.commit()
        return {"cycle_id": cycle_id, "source_id": source_id, "cycle_time": cycle_time, "current_state": status}

    def upsert_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any]:
        job = self.store.get_job(str(record["job_id"]))
        if job is None:
            job = PipelineJob(
                job_id=str(record["job_id"]),
                run_id=record.get("run_id"),
                cycle_id=record.get("cycle_id"),
                job_type=str(record["job_type"]),
                slurm_job_id=record.get("slurm_job_id"),
                model_id=record.get("model_id"),
                stage=record.get("stage"),
                status=str(record["status"]),
                submitted_at=record.get("submitted_at"),
                started_at=record.get("started_at"),
                finished_at=record.get("finished_at"),
                exit_code=record.get("exit_code"),
                error_code=record.get("error_code"),
                error_message=record.get("error_message"),
                log_uri=record.get("log_uri"),
            )
            self.store.session.add(job)
        else:
            job.run_id = record.get("run_id")
            job.cycle_id = record.get("cycle_id")
            job.job_type = str(record["job_type"])
            job.slurm_job_id = record.get("slurm_job_id")
            job.model_id = record.get("model_id")
            job.stage = record.get("stage")
            job.status = str(record["status"])
            job.submitted_at = record.get("submitted_at")
            job.started_at = record.get("started_at")
            job.finished_at = record.get("finished_at")
            job.exit_code = record.get("exit_code")
            job.error_code = record.get("error_code")
            job.error_message = record.get("error_message")
            job.log_uri = record.get("log_uri")
            self.store.session.add(job)
        self.store.session.commit()
        self.store.session.refresh(job)
        return _job_record(job)

    def update_pipeline_job_status(
        self,
        job_id: str,
        status: str,
        *,
        started_at: datetime | None = None,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        log_uri: str | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        current = self.store.get_job(job_id)
        previous = current.status if current is not None else None
        updated = self.store.update_job_status(
            job_id,
            status,
            started_at=started_at,
            finished_at=finished_at,
            exit_code=exit_code,
            error_code=error_code,
            error_message=error_message,
            log_uri=log_uri,
        )
        return previous, _job_record(updated)

    def get_pipeline_job(self, job_id: str) -> dict[str, Any] | None:
        job = self.store.get_job(job_id)
        return _job_record(job) if job is not None else None

    def query_pipeline_jobs_by_cycle(self, cycle_id: str) -> list[dict[str, Any]]:
        return [_job_record(job) for job in self.store.query_jobs_by_cycle(cycle_id)]

    def query_pipeline_jobs_by_run(self, run_id: str) -> list[dict[str, Any]]:
        return [_job_record(job) for job in self.store.query_jobs_by_run(run_id)]

    def query_pipeline_job_by_slurm_id(self, slurm_job_id: str) -> dict[str, Any] | None:
        job = self.store.query_jobs_by_slurm_id(slurm_job_id)
        return _job_record(job) if job is not None else None

    def insert_pipeline_event(
        self,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        status_from: str | None,
        status_to: str | None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = self.store.insert_event(
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            status_from=status_from,
            status_to=status_to,
            message=message,
            details=details,
        )
        return {
            "event_id": event.event_id,
            "entity_type": event.entity_type,
            "entity_id": event.entity_id,
            "event_type": event.event_type,
            "status_from": event.status_from,
            "status_to": event.status_to,
            "message": event.message,
            "details": event.details,
        }

    def list_stage_statuses(
        self,
        *,
        source_id: str | None,
        cycle_time: datetime,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        del model_id
        if source_id is None:
            return []
        return self.query_pipeline_jobs_by_cycle(cycle_id_for(source_id, cycle_time))

    def create_hydro_run_from_basin(self, basin: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        run_id = str(manifest["run_id"])
        model = manifest["model"]
        self.store.session.execute(
            text(
                """
                INSERT OR IGNORE INTO hydro.hydro_run (
                    run_id,
                    run_type,
                    scenario_id,
                    model_id,
                    basin_version_id,
                    source_id,
                    cycle_time,
                    start_time,
                    end_time,
                    status,
                    run_manifest_uri
                )
                VALUES (
                    :run_id,
                    :run_type,
                    :scenario_id,
                    :model_id,
                    :basin_version_id,
                    :source_id,
                    :cycle_time,
                    :start_time,
                    :end_time,
                    'created',
                    :run_manifest_uri
                )
                """
            ),
            {
                "run_id": run_id,
                "run_type": manifest.get("run_type", "forecast"),
                "scenario_id": manifest["scenario_id"],
                "model_id": model["model_id"],
                "basin_version_id": model["basin_version_id"],
                "source_id": manifest["source_id"],
                "cycle_time": _parse_time(manifest["cycle_time"]),
                "start_time": _parse_time(manifest["start_time"]),
                "end_time": _parse_time(manifest["end_time"]),
                "run_manifest_uri": manifest["outputs"]["run_manifest_uri"],
            },
        )
        self.store.session.commit()
        row = self.store.session.execute(
            text("SELECT * FROM hydro.hydro_run WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).mappings().one()
        return dict(row)

    def _ensure_hydro_run(self, *, source_id: str, cycle_time: datetime) -> None:
        run_id = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
        self.store.session.execute(
            text(
                """
                INSERT OR IGNORE INTO hydro.hydro_run (
                    run_id,
                    run_type,
                    scenario_id,
                    model_id,
                    basin_version_id,
                    source_id,
                    cycle_time,
                    start_time,
                    end_time,
                    status,
                    run_manifest_uri
                )
                VALUES (
                    :run_id,
                    'forecast',
                    :scenario_id,
                    'cycle_model',
                    'cycle_basin',
                    :source_id,
                    :cycle_time,
                    :start_time,
                    :end_time,
                    'created',
                    :run_manifest_uri
                )
                """
            ),
            {
                "run_id": run_id,
                "scenario_id": _scenario_for_source(source_id),
                "source_id": source_id,
                "cycle_time": cycle_time,
                "start_time": cycle_time,
                "end_time": cycle_time + timedelta(days=7),
                "run_manifest_uri": f"s3://nhms/runs/{run_id}/manifest.json",
            },
        )


def _store() -> Iterator["_ClosingStore"]:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _attach_schemas(engine)
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
        connection.execute(
            text(
                """
                CREATE TABLE hydro.hydro_run (
                    run_id TEXT PRIMARY KEY,
                    run_type TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    basin_version_id TEXT NOT NULL,
                    source_id TEXT,
                    cycle_time DATETIME,
                    start_time DATETIME NOT NULL,
                    end_time DATETIME NOT NULL,
                    status TEXT NOT NULL,
                    slurm_job_id TEXT,
                    run_manifest_uri TEXT NOT NULL
                )
                """
            )
        )
    session = Session(engine)
    return _ClosingStore(session)


def _attach_schemas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection: Any, _connection_record: Any) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS met")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS hydro")


class _ClosingStore(PipelineStore):
    def __enter__(self) -> PipelineStore:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.session.close()


class _client:
    def __init__(self, store: PipelineStore) -> None:
        self.store = store
        self.client: TestClient | None = None
        self.previous_allow_dev_role_header: str | None = None

    def __enter__(self) -> TestClient:
        app.dependency_overrides[pipeline_routes.get_pipeline_store] = lambda: self.store
        self.previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
        os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
        self.client = TestClient(app)
        return self.client

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self.previous_allow_dev_role_header is None:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        else:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = self.previous_allow_dev_role_header
        app.dependency_overrides.pop(pipeline_routes.get_pipeline_store, None)
        if self.client is not None:
            self.client.close()


def _job_record(job: PipelineJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "run_id": job.run_id,
        "cycle_id": job.cycle_id,
        "job_type": job.job_type,
        "slurm_job_id": job.slurm_job_id,
        "model_id": job.model_id,
        "status": job.status,
        "stage": job.stage,
        "submitted_at": job.submitted_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "exit_code": job.exit_code,
        "retry_count": job.retry_count,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "log_uri": job.log_uri,
        "created_at": job.created_at,
    }


def _basins(count: int) -> list[dict[str, Any]]:
    return [
        {
            "model_id": f"model_{index}",
            "basin_id": f"basin_{index}",
            "basin_version_id": f"basin_v{index}",
            "run_id": f"run_{index}",
            "river_network_version_id": f"river_v{index}",
        }
        for index in range(count)
    ]


def _scenario_for_source(source_id: str) -> str:
    if source_id.upper() == "IFS":
        return "forecast_ifs_deterministic"
    return "forecast_gfs_deterministic"


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
