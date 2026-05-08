from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from packages.common.state_manager import StateSnapshot
from services.orchestrator.chain import (
    ANALYSIS_SCENARIO_ID,
    ANALYSIS_STAGES,
    AnalysisOrchestrator,
    AnalysisPipelineAlreadyActiveError,
    ForcingContext,
    ModelContext,
    OrchestratorConfig,
)
from workers.output_parser.parser import HydroRunContext, RiverSegmentOrder, parse_rivqdown_file


class FakeSlurmClient:
    def __init__(self, *, fail_stage: str | None = None, fail_error_code: str = "FORCED_FAILURE") -> None:
        self.fail_stage = fail_stage
        self.fail_error_code = fail_error_code
        self.submissions: list[dict[str, Any]] = []
        self.jobs: dict[str, dict[str, Any]] = {}
        self.poll_counts: dict[str, int] = {}
        self.next_job = 2000

    def submit_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        active = [job for job in self.jobs.values() if job["status"] not in {"succeeded", "failed", "cancelled"}]
        if active:
            raise AssertionError("submitted a stage before the previous stage reached terminal status")
        self.next_job += 1
        job_id = f"mock_{self.next_job}"
        stage = payload["manifest"]["stage"]
        submitted_at = _dt("2026-05-01T00:00:00Z") + timedelta(minutes=len(self.submissions) * 3)
        job = {
            "job_id": job_id,
            "run_id": payload["run_id"],
            "model_id": payload["model_id"],
            "stage": stage,
            "status": "submitted",
            "submitted_at": _fmt(submitted_at),
            "started_at": None,
            "finished_at": None,
            "exit_code": None,
            "error_code": None,
            "error_message": None,
        }
        self.submissions.append(payload)
        self.jobs[job_id] = job
        self.poll_counts[job_id] = 0
        return dict(job)

    def get_job_status(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        self.poll_counts[job_id] += 1
        count = self.poll_counts[job_id]
        submitted_at = datetime.fromisoformat(job["submitted_at"].replace("Z", "+00:00"))
        if count == 1:
            job["status"] = "running"
            job["started_at"] = _fmt(submitted_at + timedelta(minutes=1))
        else:
            failed = self.fail_stage == job["stage"]
            job["status"] = "failed" if failed else "succeeded"
            job["finished_at"] = _fmt(submitted_at + timedelta(minutes=2))
            job["exit_code"] = 1 if failed else 0
            if failed:
                job["error_code"] = self.fail_error_code
                job["error_message"] = "forced test failure"
        return dict(job)

    def fetch_logs(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        return {"job_id": job_id, "run_id": job["run_id"], "complete": True, "logs": f"log for {job['stage']}"}


class FakeStateManager:
    def __init__(self, state: StateSnapshot | None = None) -> None:
        self.state = state
        self.requests: list[tuple[str, datetime]] = []

    def get_latest_usable_state(self, *, model_id: str, before_time: datetime) -> StateSnapshot | None:
        self.requests.append((model_id, before_time))
        return self.state


class FakeAnalysisRepository:
    def __init__(self, *, active: bool = False) -> None:
        self.active = active
        self.model = ModelContext(
            model_id="demo_model",
            basin_id="yangtze",
            basin_version_id="basin_v1",
            river_network_version_id="rivnet_v1",
            segment_count=2,
            model_package_uri="models/demo_model/package/",
        )
        self.forcing = ForcingContext(
            forcing_version_id="forc_era5_2026050100_demo_model",
            forcing_package_uri="forcing/era5/2026050100/basin_v1/demo_model/",
            start_time=_dt("2026-05-01T00:00:00Z"),
            end_time=_dt("2026-05-02T00:00:00Z"),
            source_id="ERA5",
        )
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.cycle_statuses: list[str] = []
        self.hydro_updates: list[dict[str, Any]] = []
        self.created_runs: list[tuple[Any, dict[str, Any]]] = []

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        return False

    def has_active_analysis_run(self, *, model_id: str, start_time: datetime, end_time: datetime) -> bool:
        assert model_id == "demo_model"
        assert start_time == _dt("2026-05-01T00:00:00Z")
        assert end_time == _dt("2026-05-02T00:00:00Z")
        return self.active

    def load_model_context(self, model_id: str) -> ModelContext:
        assert model_id == "demo_model"
        return self.model

    def find_forcing_context(self, *, source_id: str, cycle_time: datetime, model_id: str) -> ForcingContext:
        assert source_id == "ERA5"
        assert cycle_time == _dt("2026-05-01T00:00:00Z")
        assert model_id == "demo_model"
        return self.forcing

    def ensure_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any]:
        assert source_id == "ERA5"
        return {"cycle_id": "era5_2026050100", "cycle_time": cycle_time}

    def create_hydro_run(self, context: Any, manifest: dict[str, Any]) -> dict[str, Any]:
        self.created_runs.append((context, manifest))
        self.hydro_updates.append({"status": "created", "run_id": context.run_id})
        return {"run_id": context.run_id, "status": "created"}

    def update_hydro_run_status(
        self,
        run_id: str,
        status: str,
        *,
        slurm_job_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "run_id": run_id,
            "status": status,
            "slurm_job_id": slurm_job_id,
            "error_code": error_code,
            "error_message": error_message,
        }
        self.hydro_updates.append(record)
        return record

    def upsert_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any]:
        self.jobs[record["job_id"]] = dict(record)
        return dict(record)

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
    ) -> tuple[str | None, dict[str, Any]]:
        previous = self.jobs[job_id]["status"]
        self.jobs[job_id].update(
            {
                "status": status,
                "started_at": started_at or self.jobs[job_id].get("started_at"),
                "finished_at": finished_at or self.jobs[job_id].get("finished_at"),
                "exit_code": exit_code,
                "error_code": error_code,
                "error_message": error_message,
            }
        )
        return previous, dict(self.jobs[job_id])

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
        event = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "status_from": status_from,
            "status_to": status_to,
            "message": message,
            "details": details or {},
        }
        self.events.append(event)
        return event

    def update_forecast_cycle_status(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        assert source_id == "ERA5"
        assert cycle_time == _dt("2026-05-01T00:00:00Z")
        self.cycle_statuses.append(status)
        return {"status": status, "error_code": error_code, "error_message": error_message}

    def list_stage_statuses(
        self,
        *,
        source_id: str | None,
        cycle_time: datetime,
        model_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return list(self.jobs.values())


def test_analysis_run_creation_uses_scenario_and_latest_init_state(tmp_path: Path) -> None:
    state = StateSnapshot(
        state_id="state_demo_model_2026043000",
        model_id="demo_model",
        run_id="analysis_previous",
        valid_time=_dt("2026-04-30T00:00:00Z"),
        state_uri="states/demo_model/2026043000/state.cfg.ic",
        checksum="abc123",
        usable_flag=True,
    )
    repository = FakeAnalysisRepository()
    state_manager = FakeStateManager(state)
    orchestrator = _build_orchestrator(tmp_path, repository, FakeSlurmClient(), state_manager)

    result = orchestrator.trigger_analysis(model_id="demo_model", date_range="2026-05-01/2026-05-02")

    context, manifest = repository.created_runs[0]
    assert result.status == "succeeded"
    assert manifest["run_type"] == "analysis"
    assert manifest["scenario_id"] == ANALYSIS_SCENARIO_ID
    assert context.init_state_id == state.state_id
    assert manifest["initial_state"]["ic_file_uri"] == state.state_uri
    assert manifest["runtime"]["init_mode"] == 3
    assert state_manager.requests == [("demo_model", _dt("2026-05-01T00:00:00Z"))]


def test_analysis_duplicate_prevention_rejects_before_submission(tmp_path: Path) -> None:
    repository = FakeAnalysisRepository(active=True)
    client = FakeSlurmClient()
    orchestrator = _build_orchestrator(tmp_path, repository, client, FakeStateManager())

    with pytest.raises(AnalysisPipelineAlreadyActiveError):
        orchestrator.trigger_analysis(model_id="demo_model", date_range="2026-05-01/2026-05-02")

    assert client.submissions == []
    assert repository.created_runs == []


def test_analysis_lazy_submission_runs_six_stages_in_order(tmp_path: Path) -> None:
    repository = FakeAnalysisRepository()
    client = FakeSlurmClient()
    orchestrator = _build_orchestrator(tmp_path, repository, client, FakeStateManager())

    result = orchestrator.trigger_analysis(model_id="demo_model", date_range="2026-05-01/2026-05-02")

    assert result.status == "succeeded"
    assert [payload["manifest"]["stage"] for payload in client.submissions] == [
        stage.stage for stage in ANALYSIS_STAGES
    ]
    assert [stage.status for stage in result.stages] == ["succeeded"] * 6
    assert [event["entity_type"] for event in repository.events] == ["analysis_pipeline"] * 18
    assert {job["status"] for job in repository.jobs.values()} == {"succeeded"}
    assert [update["status"] for update in repository.hydro_updates] == [
        "created",
        "staged",
        "submitted",
        "running",
        "succeeded",
        "parsed",
    ]


def test_analysis_stage_failure_aborts_subsequent_stages(tmp_path: Path) -> None:
    repository = FakeAnalysisRepository()
    client = FakeSlurmClient(fail_stage="forcing_produce")
    orchestrator = _build_orchestrator(tmp_path, repository, client, FakeStateManager())

    result = orchestrator.trigger_analysis(model_id="demo_model", date_range="2026-05-01/2026-05-02")

    assert result.status == "failed"
    assert [payload["manifest"]["stage"] for payload in client.submissions] == [
        "era5_download",
        "canonical_convert",
        "forcing_produce",
    ]
    assert repository.hydro_updates[-1]["status"] == "failed"
    assert repository.cycle_statuses[-1] == "failed_forcing"


def test_analysis_run_timeout_maps_to_slurm_timeout(tmp_path: Path) -> None:
    repository = FakeAnalysisRepository()
    client = FakeSlurmClient(fail_stage="analysis_run", fail_error_code="TIMEOUT")
    orchestrator = _build_orchestrator(tmp_path, repository, client, FakeStateManager())

    orchestrator.trigger_analysis(model_id="demo_model", date_range="2026-05-01/2026-05-02")

    assert repository.hydro_updates[-1]["status"] == "failed"
    assert repository.hydro_updates[-1]["error_code"] == "SLURM_TIMEOUT"


def test_analysis_output_parser_uses_null_lead_time(tmp_path: Path) -> None:
    rivqdown = tmp_path / "demo.rivqdown"
    rivqdown.write_text("time,seg_a\n2026-05-01T00:00:00Z,86400\n", encoding="utf-8")
    context = HydroRunContext(
        run_id="analysis_001",
        model_id="demo_model",
        basin_version_id="basin_v1",
        river_network_version_id="rivnet_v1",
        source_id="ERA5",
        cycle_id="era5_2026050100",
        cycle_time=_dt("2026-05-01T00:00:00Z"),
        start_time=_dt("2026-05-01T00:00:00Z"),
        run_type="analysis",
        scenario_id=ANALYSIS_SCENARIO_ID,
    )

    rows = parse_rivqdown_file(rivqdown, context, (RiverSegmentOrder("seg_a", "rivnet_v1", 1),))

    assert rows[0].lead_time_hours is None
    assert rows[0].value == pytest.approx(1.0)


def _build_orchestrator(
    tmp_path: Path,
    repository: FakeAnalysisRepository,
    client: FakeSlurmClient,
    state_manager: FakeStateManager,
) -> AnalysisOrchestrator:
    workspace = tmp_path / "workspace"
    object_root = tmp_path / "object-store"
    config = OrchestratorConfig(
        workspace_root=workspace,
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        poll_interval_seconds=0,
        job_timeout_seconds=5,
    )
    return AnalysisOrchestrator(
        config=config,
        repository=repository,
        state_manager=state_manager,
        slurm_client=client,
        object_store=LocalObjectStore(object_root, "s3://nhms"),
    )


def _dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        candidate = value
    else:
        candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if candidate.tzinfo is None:
        return candidate.replace(tzinfo=UTC)
    return candidate.astimezone(UTC)


def _fmt(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
