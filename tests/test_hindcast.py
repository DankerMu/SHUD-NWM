from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.main import app
from apps.api.routes import forecast as forecast_routes
from apps.api.routes import hindcast as hindcast_routes
from apps.api.routes import pipeline as pipeline_routes
from services.orchestrator.persistence import Base, PipelineJob, PipelineStore
from services.orchestrator.retry import RetryConfig, RetryService
from workers.flood_frequency import cli as flood_cli
from workers.flood_frequency.config import HindcastConfig
from workers.flood_frequency.hindcast import (
    HINDCAST_FORCING_PACKAGE_UNAVAILABLE,
    INSUFFICIENT_ERA5_COVERAGE,
    HindcastError,
    HindcastForcingResult,
    _write_hindcast_manifest,
    calendar_years,
    hindcast_year,
    produce_hindcast_forcing,
    run_id_for_year,
    submit_hindcast,
    submit_hindcast_slurm,
)


def test_year_slice_generation_1993_to_2023_is_31_years() -> None:
    years = calendar_years("1993-01-01T00:00:00Z", "2023-12-31T23:00:00Z")

    assert years[0] == 1993
    assert years[-1] == 2023
    assert len(years) == 31


def test_idempotent_skip_already_succeeded_year() -> None:
    with _store() as session:
        _insert_hydro_run(session, run_id_for_year("yangtze_shud_v12", 1993), 1993, status="succeeded")

        result = submit_hindcast(
            "yangtze_shud_v12",
            "ERA5",
            "1993-01-01T00:00:00Z",
            "1994-12-31T23:00:00Z",
            "flood_frequency_sample",
            session,
        )

        assert result.total_runs == 1
        assert result.skipped_years == [1993]
        assert result.active_years == []
        assert result.run_ids == [run_id_for_year("yangtze_shud_v12", 1994)]


def test_idempotent_skip_already_parsed_year() -> None:
    with _store() as session:
        _insert_hydro_run(session, run_id_for_year("yangtze_shud_v12", 1993), 1993, status="parsed")

        result = submit_hindcast(
            "yangtze_shud_v12",
            "ERA5",
            "1993-01-01T00:00:00Z",
            "1993-12-31T23:00:00Z",
            "flood_frequency_sample",
            session,
        )

        assert result.total_runs == 0
        assert result.skipped_years == [1993]
        assert result.active_years == []
        assert result.run_ids == []
        assert _hydro_run(session, run_id_for_year("yangtze_shud_v12", 1993))["status"] == "parsed"


def test_submit_hindcast_skips_active_years_without_resetting() -> None:
    with _store() as session:
        running_id = run_id_for_year("yangtze_shud_v12", 1993)
        submitted_id = run_id_for_year("yangtze_shud_v12", 1994)
        _insert_hydro_run(session, running_id, 1993, status="running", error_code="KEEP_RUNNING")
        _insert_hydro_run(session, submitted_id, 1994, status="submitted", error_code="KEEP_SUBMITTED")

        result = submit_hindcast(
            "yangtze_shud_v12",
            "ERA5",
            "1993-01-01T00:00:00Z",
            "1995-12-31T23:00:00Z",
            "flood_frequency_sample",
            session,
        )

        assert result.total_runs == 1
        assert result.active_years == [1993, 1994]
        assert result.skipped_years == []
        assert result.run_ids == [run_id_for_year("yangtze_shud_v12", 1995)]
        assert _hydro_run(session, running_id)["status"] == "running"
        assert _hydro_run(session, running_id)["error_code"] == "KEEP_RUNNING"
        assert _hydro_run(session, submitted_id)["status"] == "submitted"
        assert _hydro_run(session, submitted_id)["error_code"] == "KEEP_SUBMITTED"


def test_single_year_full_flow_forcing_shud_parse_and_river_timeseries(monkeypatch: pytest.MonkeyPatch) -> None:
    with _store() as session:
        submit_hindcast(
            "yangtze_shud_v12",
            "ERA5",
            "1993-01-01T00:00:00Z",
            "1993-12-31T23:00:00Z",
            "flood_frequency_sample",
            session,
        )
        _insert_era5_hours(session, 1993, 24 * 365)

        calls: list[str] = []

        def fake_run(run_id: str, *_args: Any) -> dict[str, Any]:
            calls.append("shud")
            return {"run_id": run_id, "status": "succeeded"}

        def fake_parse(run_id: str) -> dict[str, Any]:
            calls.append("parse")
            session.execute(
                text(
                    """
                    INSERT INTO hydro.river_timeseries (
                        run_id, basin_version_id, river_network_version_id, river_segment_id,
                        valid_time, variable, value, unit
                    )
                    VALUES (:run_id, 'basin_v1', 'rnv_v1', 'seg_001', :valid_time, 'q_down', 42.0, 'm3/s')
                    """
                ),
                {"run_id": run_id, "valid_time": datetime(1993, 1, 1, tzinfo=UTC)},
            )
            session.commit()
            return {"run_id": run_id, "status": "parsed", "rows_written": 1}

        monkeypatch.setattr("workers.flood_frequency.hindcast.run_shud_hindcast", fake_run)
        monkeypatch.setattr("workers.flood_frequency.hindcast.parse_hindcast_output", fake_parse)

        result = hindcast_year("yangtze_shud_v12", "ERA5", 1993, session)

        assert calls == ["shud", "parse"]
        assert result.status == "parsed"
        run = _hydro_run(session, result.run_id)
        assert run["status"] == "parsed"
        assert run["forcing_version_id"] == "forc_era5_hindcast_yangtze_shud_v12_1993"
        assert run["cycle_time"] is not None
        assert _count(session, "hydro.river_timeseries") == 1


def test_forcing_incomplete_failure_marks_run_failed() -> None:
    with _store() as session:
        submit_hindcast(
            "yangtze_shud_v12",
            "ERA5",
            "1993-01-01T00:00:00Z",
            "1993-12-31T23:00:00Z",
            "flood_frequency_sample",
            session,
        )
        _insert_era5_hours(session, 1993, int(24 * 365 * 0.5))

        with pytest.raises(HindcastError) as exc_info:
            hindcast_year("yangtze_shud_v12", "ERA5", 1993, session)

        assert exc_info.value.error_code == INSUFFICIENT_ERA5_COVERAGE
        run = _hydro_run(session, run_id_for_year("yangtze_shud_v12", 1993))
        assert run["status"] == "failed"
        assert run["error_code"] == INSUFFICIENT_ERA5_COVERAGE


def test_era5_coverage_reports_missing_required_variables() -> None:
    with _store() as session:
        _insert_era5_hours(session, 1993, 24 * 365, variables=("prcp_rate_or_amount",))

        with pytest.raises(HindcastError) as exc_info:
            produce_hindcast_forcing("yangtze_shud_v12", "ERA5", 1993, session)

        assert exc_info.value.error_code == INSUFFICIENT_ERA5_COVERAGE
        assert "air_temperature_2m" in exc_info.value.details["missing_variables"]
        qc = session.execute(text("SELECT checks_json FROM ops.qc_result")).mappings().one()
        assert isinstance(qc["checks_json"], str)
        assert '"missing_variables"' in qc["checks_json"]


def test_failure_retry_resets_failed_hindcast_run() -> None:
    with _store() as session:
        run_id = run_id_for_year("yangtze_shud_v12", 1993)
        _insert_hydro_run(session, run_id, 1993, status="failed", error_code="NODE_FAILURE")
        store = PipelineStore(session)
        store.create_job(
            job_id="job_failed",
            run_id=run_id,
            cycle_id="hindcast_yangtze_shud_v12_1993_1993",
            job_type="hindcast",
            slurm_job_id="123",
            model_id="yangtze_shud_v12",
            stage="hindcast",
            status="failed",
        )
        gateway = _RecordingGateway(job_id="slurm_retry")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry(run_id, gateway=gateway)

        assert retry.status == "submitted"
        assert retry.job_type == "hindcast"
        assert _hydro_run(session, run_id)["status"] == "pending"


def test_hindcast_submit_permission_denied_for_viewer_and_analyst() -> None:
    with _store() as session, _api_client(session) as client:
        body = _submit_body()

        viewer = client.post("/api/v1/hindcast/submit", json=body, headers={"X-User-Role": "viewer"})
        analyst = client.post("/api/v1/hindcast/submit", json=body, headers={"X-User-Role": "analyst"})

        assert viewer.status_code == 403
        assert analyst.status_code == 403
        assert viewer.json()["error"]["code"] == "RBAC_FORBIDDEN"


def test_data_isolation_forecast_series_default_excludes_hindcast() -> None:
    store = _ForecastIsolationStore()
    app.dependency_overrides[forecast_routes.get_forecast_store] = lambda: store
    try:
        with TestClient(app) as client:
            default = client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series"
                "?river_network_version_id=rnv_v1"
            )
            denied = client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series"
                "?river_network_version_id=rnv_v1&run_types=hindcast",
                headers={"X-User-Role": "viewer"},
            )
            explicit = client.get(
                "/api/v1/basin-versions/basin_v1/river-segments/seg_001/forecast-series"
                "?river_network_version_id=rnv_v1&run_types=hindcast",
                headers={"X-User-Role": "analyst"},
            )
    finally:
        app.dependency_overrides.pop(forecast_routes.get_forecast_store, None)

    assert default.status_code == 200
    assert default.json()["series"] == []
    assert denied.status_code == 403
    assert explicit.status_code == 200
    assert explicit.json()["series"][0]["scenario_id"] == "hindcast_replay"
    assert store.calls[0]["run_types"] is None
    assert store.calls[1]["run_types"] == ["hindcast"]


def test_forecast_series_unsupported_variable_returns_empty_response() -> None:
    store = forecast_routes.PsycopgForecastStore("postgresql://test")

    response = store.forecast_series(
        basin_version_id="basin_v1",
        segment_id="seg_001",
        river_network_version_id="rnv_v1",
        issue_time="latest",
        variables=["temperature"],
        scenarios=["GFS"],
    )

    assert response["series"] == []
    assert response["issue_time"] is None


def test_hindcast_submit_input_validation() -> None:
    with _store() as session, _api_client(session) as client:
        bad_time = client.post(
            "/api/v1/hindcast/submit",
            json={**_submit_body(), "start_time": "2024-01-01T00:00:00Z", "end_time": "2023-01-01T00:00:00Z"},
            headers={"X-User-Role": "operator"},
        )
        missing_model = client.post(
            "/api/v1/hindcast/submit",
            json={**_submit_body(), "model_id": "missing_model"},
            headers={"X-User-Role": "operator"},
        )

        assert bad_time.status_code == 400
        assert missing_model.status_code == 404


def test_hindcast_runs_do_not_create_state_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    with _store() as session:
        submit_hindcast(
            "yangtze_shud_v12",
            "ERA5",
            "1993-01-01T00:00:00Z",
            "1993-12-31T23:00:00Z",
            "flood_frequency_sample",
            session,
        )

        def fake_forcing(*_args: Any) -> HindcastForcingResult:
            return HindcastForcingResult(
                forcing_version_id="forc_era5_hindcast_yangtze_shud_v12_1993",
                coverage=1.0,
                missing_rate=0.0,
                start_time=datetime(1993, 1, 1, tzinfo=UTC),
                end_time=datetime(1994, 1, 1, tzinfo=UTC),
                forcing_package_uri="object://forcing/package",
            )

        monkeypatch.setattr("workers.flood_frequency.hindcast.produce_hindcast_forcing", fake_forcing)
        monkeypatch.setattr("workers.flood_frequency.hindcast.run_shud_hindcast", lambda *args: {"status": "succeeded"})
        monkeypatch.setattr(
            "workers.flood_frequency.hindcast.parse_hindcast_output",
            lambda run_id: {"status": "parsed"},
        )

        hindcast_year("yangtze_shud_v12", "ERA5", 1993, session)

        assert _count(session, "hydro.state_snapshot") == 0


def test_hindcast_submit_api_returns_slurm_job_array_id() -> None:
    with _store() as session, _api_client(session) as client:
        _insert_forcing_version(session, 1993, forcing_package_uri="object://forcing/package/1993")

        response = client.post(
            "/api/v1/hindcast/submit",
            json=_submit_body(),
            headers={"X-User-Role": "operator"},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total_runs"] == 1
        assert data["slurm_job_array_id"] == "slurm_array_1"
        jobs = list(session.scalars(select(PipelineJob)))
        assert len(jobs) == 1
        assert jobs[0].array_task_id == 0


def test_hindcast_submit_api_marks_created_run_failed_when_forcing_preflight_fails() -> None:
    with _store() as session, _api_client(session) as client:
        response = client.post(
            "/api/v1/hindcast/submit",
            json=_submit_body(),
            headers={"X-User-Role": "operator"},
        )

        assert response.status_code == 400
        payload = response.json()["error"]
        assert payload["code"] == HINDCAST_FORCING_PACKAGE_UNAVAILABLE
        assert list(session.scalars(select(PipelineJob))) == []

        run = _hydro_run(session, run_id_for_year("yangtze_shud_v12", 1993))
        assert run["status"] == "failed"
        assert run["error_code"] == HINDCAST_FORCING_PACKAGE_UNAVAILABLE
        assert run["error_message"] == payload["message"]


def test_hindcast_submit_cli_marks_created_run_failed_when_forcing_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _store() as session:
        monkeypatch.setattr(flood_cli, "_session_from_env", lambda: session)
        monkeypatch.setattr(
            HindcastConfig,
            "from_env",
            staticmethod(
                lambda: HindcastConfig(
                    workspace_root=Path(".").resolve(),
                    object_store_root=Path(".").resolve(),
                    slurm_client=_FakeSlurmClient(),
                )
            ),
        )

        with pytest.raises(HindcastError) as exc_info:
            flood_cli._hindcast_submit(
                "yangtze_shud_v12",
                "ERA5",
                "1993-01-01T00:00:00Z",
                "1993-12-31T23:00:00Z",
                "flood_frequency_sample",
            )

        assert exc_info.value.error_code == HINDCAST_FORCING_PACKAGE_UNAVAILABLE
        assert list(session.scalars(select(PipelineJob))) == []

        run = _hydro_run(session, run_id_for_year("yangtze_shud_v12", 1993))
        assert run["status"] == "failed"
        assert run["error_code"] == HINDCAST_FORCING_PACKAGE_UNAVAILABLE
        assert run["error_message"] == exc_info.value.message


def test_submit_hindcast_slurm_manifest_includes_runtime_context(tmp_path: Path) -> None:
    with _store() as session:
        _insert_forcing_version(session, 1993, forcing_package_uri="object://forcing/package/1993")
        config = HindcastConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=tmp_path / "object-store",
            object_store_prefix="hindcast/prod",
            db_session=session,
            slurm_client=_FakeSlurmClient(),
        )

        result = submit_hindcast_slurm("yangtze_shud_v12", "ERA5", [1993], config)

        assert result.slurm_job_array_id == "slurm_array_1"
        assert result.job_ids == ["hindcast_era5_yangtze_shud_v12_1993_hindcast_0"]


def test_submit_hindcast_slurm_requires_real_forcing_before_submission(tmp_path: Path) -> None:
    with _store() as session:
        slurm_client = _FakeSlurmClient()
        config = HindcastConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=tmp_path / "object-store",
            object_store_prefix="hindcast/prod",
            db_session=session,
            slurm_client=slurm_client,
        )

        with pytest.raises(HindcastError) as exc_info:
            submit_hindcast_slurm("yangtze_shud_v12", "ERA5", [1993], config)

        assert exc_info.value.error_code == HINDCAST_FORCING_PACKAGE_UNAVAILABLE
        assert slurm_client.submissions == 0


def test_submit_hindcast_slurm_rejects_metadata_only_forcing_before_submission(tmp_path: Path) -> None:
    with _store() as session:
        _insert_forcing_version(session, 1993, forcing_package_uri="")
        slurm_client = _FakeSlurmClient()
        config = HindcastConfig(
            workspace_root=tmp_path / "workspace",
            object_store_root=tmp_path / "object-store",
            object_store_prefix="hindcast/prod",
            db_session=session,
            slurm_client=slurm_client,
        )

        with pytest.raises(HindcastError) as exc_info:
            submit_hindcast_slurm("yangtze_shud_v12", "ERA5", [1993], config)

        assert exc_info.value.error_code == HINDCAST_FORCING_PACKAGE_UNAVAILABLE
        assert slurm_client.submissions == 0


def test_produce_hindcast_forcing_success_lineage() -> None:
    with _store() as session:
        _insert_era5_hours(session, 1993, 24 * 365)

        result = produce_hindcast_forcing("yangtze_shud_v12", "ERA5", 1993, session)

        row = session.execute(
            text("SELECT * FROM met.forcing_version WHERE forcing_version_id = :id"),
            {"id": result.forcing_version_id},
        ).mappings().one()
        assert result.coverage == pytest.approx(1.0)
        assert isinstance(row["lineage_json"], str)
        assert '"purpose": "hindcast"' in row["lineage_json"]
        assert '"year": 1993' in row["lineage_json"]
        qc = session.execute(text("SELECT checks_json FROM ops.qc_result WHERE passed = 1")).mappings().one()
        assert isinstance(qc["checks_json"], str)
        assert '"required_variables"' in qc["checks_json"]


def test_produce_hindcast_forcing_subthreshold_gap_marks_incomplete_forcing() -> None:
    with _store() as session:
        _insert_era5_hours(session, 1993, int(24 * 365 * 0.95))

        result = produce_hindcast_forcing("yangtze_shud_v12", "ERA5", 1993, session)

        assert result.missing_rate == pytest.approx(0.05)
        qc = session.execute(text("SELECT checks_json FROM ops.qc_result WHERE passed = 1")).mappings().one()
        checks = json.loads(qc["checks_json"])
        assert checks["quality_flag"] == "incomplete_forcing"


def test_produce_hindcast_forcing_uses_producer_result(monkeypatch: pytest.MonkeyPatch) -> None:
    produced = SimpleNamespace(
        forcing_version_id="forc_era5_1993010100_yangtze_shud_v12",
        forcing_package_uri="forcing/era5/1993010100/basin_v1/yangtze_shud_v12/",
    )

    with _store() as session:
        _insert_era5_hours(session, 1993, 24 * 365)
        monkeypatch.setattr(
            "workers.flood_frequency.hindcast._produce_forcing_package_with_producer",
            lambda **_kwargs: produced,
        )

        result = produce_hindcast_forcing("yangtze_shud_v12", "ERA5", 1993, session)

        assert result.forcing_version_id == produced.forcing_version_id
        assert result.forcing_package_uri == produced.forcing_package_uri


def test_hindcast_manifest_uses_shud_nested_schema(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with _store() as session:
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        _insert_era5_hours(session, 1993, 24 * 365)
        produced = SimpleNamespace(
            forcing_version_id="forc_era5_1993010100_yangtze_shud_v12",
            forcing_package_uri="object://forcing/package",
        )
        monkeypatch.setattr(
            "workers.flood_frequency.hindcast._produce_forcing_package_with_producer",
            lambda **_kwargs: produced,
        )
        forcing = produce_hindcast_forcing("yangtze_shud_v12", "ERA5", 1993, session)
        run_id = run_id_for_year("yangtze_shud_v12", 1993)
        _insert_hydro_run(session, run_id, 1993, status="running")
        session.execute(
            text(
                """
                INSERT INTO met.forcing_version (
                    forcing_version_id, model_id, source_id, cycle_time, start_time, end_time,
                    station_count, forcing_package_uri, checksum, lineage_json
                )
                VALUES (
                    :forcing_version_id, 'yangtze_shud_v12', 'ERA5', :start_time, :start_time, :end_time,
                    1, :forcing_package_uri, 'abc', '{}'
                )
                """
            ),
            {
                "forcing_version_id": forcing.forcing_version_id,
                "forcing_package_uri": forcing.forcing_package_uri,
                "start_time": datetime(1993, 1, 1, tzinfo=UTC),
                "end_time": datetime(1994, 1, 1, tzinfo=UTC),
            },
        )
        session.execute(
            text("UPDATE hydro.hydro_run SET forcing_version_id = :forcing_version_id WHERE run_id = :run_id"),
            {"run_id": run_id, "forcing_version_id": forcing.forcing_version_id},
        )
        session.commit()

        manifest_path = _write_hindcast_manifest(run_id, "yangtze_shud_v12", "ERA5", 1993, session)

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["cycle_time"] == "1993-01-01T00:00:00Z"
        assert manifest["model"]["model_id"] == "yangtze_shud_v12"
        assert manifest["model"]["model_package_uri"] == "object://models/yangtze"
        assert manifest["forcing"]["forcing_version_id"] == forcing.forcing_version_id
        assert manifest["forcing"]["forcing_uri"] == "object://forcing/package"
        assert manifest["outputs"]["run_manifest_uri"] == f"runs/{run_id}/input/manifest.json"
        assert manifest["outputs"]["output_uri"] == f"runs/{run_id}/output/"
        assert not manifest["outputs"]["run_manifest_uri"].startswith("object://")


def test_metadata_only_hindcast_forcing_cannot_enter_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    with _store() as session:
        monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
        _insert_era5_hours(session, 1993, 24 * 365)
        submit_hindcast(
            "yangtze_shud_v12",
            "ERA5",
            "1993-01-01T00:00:00Z",
            "1993-12-31T23:00:00Z",
            "flood_frequency_sample",
            session,
        )

        with pytest.raises(HindcastError) as exc_info:
            hindcast_year("yangtze_shud_v12", "ERA5", 1993, session)

        run = _hydro_run(session, run_id_for_year("yangtze_shud_v12", 1993))
        assert exc_info.value.error_code == HINDCAST_FORCING_PACKAGE_UNAVAILABLE
        assert run["status"] == "failed"
        assert run["error_code"] == HINDCAST_FORCING_PACKAGE_UNAVAILABLE


def test_hindcast_run_uris_use_plain_object_keys() -> None:
    with _store() as session:
        run_id = run_id_for_year("yangtze_shud_v12", 1993)

        submit_hindcast(
            "yangtze_shud_v12",
            "ERA5",
            "1993-01-01T00:00:00Z",
            "1993-12-31T23:00:00Z",
            "flood_frequency_sample",
            session,
        )

        run = _hydro_run(session, run_id)
        assert run["run_manifest_uri"] == f"runs/{run_id}/input/manifest.json"
        assert run["output_uri"] == f"runs/{run_id}/output/"
        assert run["log_uri"] == f"runs/{run_id}/logs/hindcast.log"
        assert not run["run_manifest_uri"].startswith("object://")


class _FakeSlurmClient:
    def __init__(self) -> None:
        self.submissions = 0

    def submit_job_array(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.submissions += 1
        assert payload["job_type"] == "hindcast"
        assert payload["tasks"][0]["array_task_id"] == 0
        assert payload["tasks"][0]["run_id"] == "hindcast_era5_yangtze_shud_v12_1993"
        assert payload["tasks"][0]["model_id"] == "yangtze_shud_v12"
        assert payload["tasks"][0]["source_id"] == "ERA5"
        assert payload["tasks"][0]["year"] == 1993
        assert payload["tasks"][0]["basin_version_id"] == "basin_v1"
        assert payload["tasks"][0]["river_network_version_id"] == "rnv_v1"
        assert payload["tasks"][0]["forcing_version_id"] == "forc_era5_hindcast_yangtze_shud_v12_1993"
        assert payload["tasks"][0]["forcing_package_uri"] == "object://forcing/package/1993"
        assert "object_store_root" in payload["tasks"][0]
        assert "object_store_prefix" in payload["tasks"][0]
        assert "workspace_dir" in payload["tasks"][0]
        assert payload["manifest"]["basin_version_id"] == "basin_v1"
        assert payload["manifest"]["river_network_version_id"] == "rnv_v1"
        assert payload["manifest"]["object_store_root"]
        assert "object_store_prefix" in payload["manifest"]
        return {"job_id": "slurm_array_1", "status": "submitted"}


class _ForecastIsolationStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def forecast_series(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if kwargs.get("run_types") == ["hindcast"]:
            return {
                "segment_id": "seg_001",
                "issue_time": "1993-01-01T00:00:00Z",
                "unit": "m3/s",
                "series": [{"scenario_id": "hindcast_replay", "points": [[725846400000, 42.0]]}],
                "frequency_thresholds": {},
            }
        return {
            "segment_id": "seg_001",
            "issue_time": None,
            "unit": "m3/s",
            "series": [],
            "frequency_thresholds": {},
        }


def _submit_body() -> dict[str, str]:
    return {
        "model_id": "yangtze_shud_v12",
        "source_id": "ERA5",
        "start_time": "1993-01-01T00:00:00Z",
        "end_time": "1993-12-31T23:00:00Z",
        "purpose": "flood_frequency_sample",
    }


@contextmanager
def _api_client(session: Session) -> Iterator[TestClient]:
    config = HindcastConfig(
        workspace_root=Path(".").resolve(),
        object_store_root=Path(".").resolve(),
        slurm_client=_FakeSlurmClient(),
    )
    app.dependency_overrides[hindcast_routes.get_hindcast_session] = lambda: session
    app.dependency_overrides[hindcast_routes.get_hindcast_config] = lambda: config
    app.dependency_overrides[pipeline_routes.get_pipeline_store] = lambda: PipelineStore(session)
    previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
    os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
    try:
        with TestClient(app) as client:
            yield client
    finally:
        if previous_allow_dev_role_header is None:
            os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
        else:
            os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
        app.dependency_overrides.pop(hindcast_routes.get_hindcast_session, None)
        app.dependency_overrides.pop(hindcast_routes.get_hindcast_config, None)
        app.dependency_overrides.pop(pipeline_routes.get_pipeline_store, None)


@contextmanager
def _store() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    _attach_schemas(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        _create_tables(connection)
        _seed_model(connection)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


def _attach_schemas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection: Any, _connection_record: Any) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS core")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS met")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS hydro")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")


def _create_tables(connection: Any) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE core.model_instance (
                model_id TEXT PRIMARY KEY,
                basin_version_id TEXT NOT NULL,
                river_network_version_id TEXT NOT NULL,
                model_package_uri TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE met.canonical_met_product (
                canonical_product_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                variable TEXT NOT NULL,
                valid_time DATETIME NOT NULL,
                quality_flag TEXT DEFAULT 'ok'
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE met.interp_weight (
                weight_id INTEGER PRIMARY KEY,
                source_id TEXT,
                model_id TEXT,
                station_id TEXT,
                variable TEXT
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE met.forcing_version (
                forcing_version_id TEXT PRIMARY KEY,
                model_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                cycle_time DATETIME,
                start_time DATETIME NOT NULL,
                end_time DATETIME NOT NULL,
                station_count INTEGER NOT NULL,
                forcing_package_uri TEXT,
                checksum TEXT,
                lineage_json TEXT NOT NULL
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
                forcing_version_id TEXT,
                init_state_id TEXT,
                source_id TEXT,
                cycle_time DATETIME,
                start_time DATETIME NOT NULL,
                end_time DATETIME NOT NULL,
                status TEXT NOT NULL,
                slurm_job_id TEXT,
                run_manifest_uri TEXT NOT NULL,
                output_uri TEXT,
                log_uri TEXT,
                error_code TEXT,
                error_message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE hydro.river_timeseries (
                run_id TEXT NOT NULL,
                basin_version_id TEXT NOT NULL,
                river_network_version_id TEXT NOT NULL,
                river_segment_id TEXT NOT NULL,
                valid_time DATETIME NOT NULL,
                lead_time_hours INTEGER,
                variable TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL,
                quality_flag TEXT DEFAULT 'ok',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE hydro.state_snapshot (
                state_id TEXT PRIMARY KEY,
                model_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                valid_time DATETIME NOT NULL,
                state_uri TEXT NOT NULL,
                checksum TEXT NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE ops.qc_result (
                qc_id INTEGER PRIMARY KEY,
                qc_checkpoint TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                run_id TEXT,
                passed BOOLEAN NOT NULL,
                severity TEXT NOT NULL,
                checks_json TEXT NOT NULL,
                message TEXT
            )
            """
        )
    )


def _seed_model(connection: Any) -> None:
    connection.execute(
        text(
            """
            INSERT INTO core.model_instance (
                model_id, basin_version_id, river_network_version_id, model_package_uri
            )
            VALUES ('yangtze_shud_v12', 'basin_v1', 'rnv_v1', 'object://models/yangtze')
            """
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO met.interp_weight (source_id, model_id, station_id, variable)
            VALUES ('ERA5', 'yangtze_shud_v12', 'sta_001', 'PRCP')
            """
        )
    )


def _insert_hydro_run(
    session: Session,
    run_id: str,
    year: int,
    *,
    status: str,
    error_code: str | None = None,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO hydro.hydro_run (
                run_id, run_type, scenario_id, model_id, basin_version_id, source_id,
                start_time, end_time, status, run_manifest_uri, error_code, error_message
            )
            VALUES (
                :run_id, 'hindcast', 'hindcast_replay', 'yangtze_shud_v12', 'basin_v1', 'ERA5',
                :start_time, :end_time, :status, :run_manifest_uri, :error_code, :error_message
            )
            """
        ),
        {
            "run_id": run_id,
            "start_time": datetime(year, 1, 1, tzinfo=UTC),
            "end_time": datetime(year + 1, 1, 1, tzinfo=UTC),
            "status": status,
            "run_manifest_uri": f"runs/{run_id}/input/manifest.json",
            "error_code": error_code,
            "error_message": error_code,
        },
    )
    session.commit()


def _insert_forcing_version(
    session: Session,
    year: int,
    *,
    forcing_package_uri: str | None,
) -> None:
    start_time = datetime(year, 1, 1, tzinfo=UTC)
    session.execute(
        text(
            """
            INSERT INTO met.forcing_version (
                forcing_version_id, model_id, source_id, cycle_time, start_time, end_time,
                station_count, forcing_package_uri, checksum, lineage_json
            )
            VALUES (
                :forcing_version_id, 'yangtze_shud_v12', 'ERA5', :start_time, :start_time, :end_time,
                1, :forcing_package_uri, 'abc', '{}'
            )
            """
        ),
        {
            "forcing_version_id": f"forc_era5_hindcast_yangtze_shud_v12_{year}",
            "forcing_package_uri": forcing_package_uri,
            "start_time": start_time,
            "end_time": datetime(year + 1, 1, 1, tzinfo=UTC),
        },
    )
    session.commit()


def _insert_era5_hours(
    session: Session,
    year: int,
    hours: int,
    *,
    variables: tuple[str, ...] | None = None,
) -> None:
    start = datetime(year, 1, 1, tzinfo=UTC)
    variables = variables or (
        "prcp_rate_or_amount",
        "air_temperature_2m",
        "relative_humidity_2m",
        "wind_u_10m",
        "wind_v_10m",
        "pressure_surface",
        "net_radiation",
    )
    rows = [
        {
            "canonical_product_id": f"era5_{year}_{variable}_{index}",
            "source_id": "ERA5",
            "variable": variable,
            "valid_time": start + timedelta(hours=index),
            "quality_flag": "ok",
        }
        for variable in variables
        for index in range(hours)
    ]
    session.execute(
        text(
            """
            INSERT INTO met.canonical_met_product (
                canonical_product_id, source_id, variable, valid_time, quality_flag
            )
            VALUES (:canonical_product_id, :source_id, :variable, :valid_time, :quality_flag)
            """
        ),
        rows,
    )
    session.commit()


def _hydro_run(session: Session, run_id: str) -> dict[str, Any]:
    return dict(
        session.execute(text("SELECT * FROM hydro.hydro_run WHERE run_id = :run_id"), {"run_id": run_id})
        .mappings()
        .one()
    )


class _RecordingGateway:
    def __init__(self, *, job_id: str = "slurm_retry") -> None:
        self.job_id = job_id
        self.submissions: list[Any] = []

    def submit_job(self, request: Any) -> dict[str, Any]:
        self.submissions.append(request)
        return {
            "job_id": self.job_id,
            "run_id": request.run_id,
            "model_id": request.model_id,
            "status": "submitted",
            "submitted_at": "2026-05-15T00:00:00Z",
            "updated_at": "2026-05-15T00:00:00Z",
        }


def _count(session: Session, table: str) -> int:
    return int(session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one())
