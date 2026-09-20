"""Pipeline, ops and job-lifecycle API contract partition.

The #2074 partition of the former 2,132-line `tests/test_api_contract.py` that
owns the control-plane route contracts: `/api/v1/runs`, the QHH-latest product,
`/api/v1/jobs`, pipeline status / ops / stage evidence, queue depth, stage
duration metrics, and the retry / cancel envelopes.

The shared doubles and assertion helpers live in `tests/api_contract_helpers.py`;
the published-document and generated-type cases stayed on
`tests/test_api_contract.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from apps.api.main import app
from apps.api.routes import pipeline as pipeline_routes
from apps.api.routes.forecast import get_forecast_store
from services.orchestrator.production_contract import (
    PRODUCTION_STAGE_TAXONOMY,
    PRODUCTION_STATUS_TAXONOMY,
)
from tests.api_contract_helpers import (
    QHH_LATEST_REFLECTED_PREFIX_LIMIT,
    _assert_schema_example_shape,
    _assert_success_envelope,
    _parameter_names,
    _persisted_pipeline_job_statuses,
    _RetryGateway,
    _RunStore,
)
from tests.test_monitoring_api import (
    GENERIC_RETRY_JOB_TYPE,
    _client,
    _create_job,
    _cycle_time,
    _insert_cycle,
    _MockGateway,
    _seed_monitoring_jobs,
    _store,
)
from workers.data_adapters.base import cycle_id_for

PIPELINE_JOB_KEYS = {
    "job_id",
    "run_id",
    "cycle_id",
    "run_type",
    "scenario",
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


OPS_JOB_STATUS_ENUM = [
    "pending",
    "queued",
    "submitted",
    "running",
    "succeeded",
    "partially_failed",
    "failed",
    "submission_failed",
    "permanently_failed",
    "cancelled",
    "skipped",
]


STAGE_JOB_EVIDENCE_KEYS = {
    "job_id",
    "run_id",
    "cycle_id",
    "job_type",
    "slurm_job_id",
    "model_id",
    "basin_id",
    "status",
    "stage",
    "submitted_at",
    "started_at",
    "finished_at",
    "duration_seconds",
    "retry_count",
    "error_code",
    "error_message",
    "log_uri",
}


def test_runs_contract_uses_success_envelope_and_paginated_data() -> None:
    app.dependency_overrides[get_forecast_store] = lambda: _RunStore()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/runs", params={"status": "parsed", "limit": 10, "offset": 0})
    finally:
        app.dependency_overrides.pop(get_forecast_store, None)

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {"items", "total_count", "limit", "offset", "total"}
    assert data["total"] == 1
    assert data["total_count"] == 1
    assert data["limit"] == 10
    assert data["offset"] == 0
    run = data["items"][0]
    assert run["run_id"] == "run_parsed"
    assert run["run_type"] == "forecast"
    assert run["status"] == "parsed"
    assert run["river_network_version_id"] == "network_v1"
    assert isinstance(run["start_time"], str)
    assert isinstance(run["end_time"], str)


def test_qhh_latest_product_contract_uses_success_envelope_and_bootstrap_identity() -> None:
    app.dependency_overrides[get_forecast_store] = lambda: _RunStore()
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/mvp/qhh/latest-product", params={"source": "GFS"})
            unavailable = client.get("/api/v1/mvp/qhh/latest-product", params={"source": "IFS"})
    finally:
        app.dependency_overrides.pop(get_forecast_store, None)

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {
        "basin_id",
        "model_id",
        "basin_version_id",
        "river_network_version_id",
        "source_id",
        "cycle_time",
        "run_id",
        "forcing_version_id",
        "station_count",
        "expected_station_count",
        "segment_count",
        "expected_segment_count",
        "status",
        "run_status",
        "valid_time_start",
        "valid_time_end",
        "river_valid_time_start",
        "river_valid_time_end",
        "forcing_valid_time_start",
        "forcing_valid_time_end",
        "available_horizon_hours",
        "expected_horizon_hours",
        "shorter_horizon",
        "availability",
        "quality",
    }
    assert data["basin_id"] == "basins_qhh"
    assert data["model_id"] == "basins_qhh_shud"
    assert data["river_network_version_id"] == "basins_qhh_rivnet_vbasins"
    assert data["source_id"] == "GFS"
    assert data["status"] == "ready"
    assert data["availability"]["ready"] is True
    assert data["availability"]["unavailable_reasons"] == []
    assert data["quality"]["required_station_variables"] == ["PRCP", "TEMP", "RH", "wind", "Rn", "Press"]

    assert unavailable.status_code == 404
    unavailable_body = unavailable.json()
    assert unavailable_body["status"] == "error"
    assert unavailable_body["error"]["code"] == "QHH_LATEST_PRODUCT_UNAVAILABLE"
    assert unavailable_body["error"]["details"]["unavailable_reasons"][0]["code"] == "NO_CANDIDATES"


def test_qhh_latest_product_strict_identity_contract_and_partial_validation() -> None:
    store = _RunStore()
    app.dependency_overrides[get_forecast_store] = lambda: store
    try:
        with TestClient(app) as client:
            strict_success = client.get(
                "/api/v1/mvp/qhh/latest-product",
                params={
                    "source": "GFS",
                    "run_id": "qhh_gfs_2026050700",
                    "cycle_time": "2026-05-07T00:00:00Z",
                    "model_id": "basins_qhh_shud",
                },
            )
            strict_unavailable = client.get(
                "/api/v1/mvp/qhh/latest-product",
                params={
                    "source": "GFS",
                    "run_id": "wrong_run",
                    "cycle_time": "2026-05-07T00:00:00Z",
                    "model_id": "basins_qhh_shud",
                },
            )
            partial = client.get(
                "/api/v1/mvp/qhh/latest-product",
                params={"source": "GFS", "run_id": "qhh_gfs_2026050700"},
            )
            blank_run_id = client.get(
                "/api/v1/mvp/qhh/latest-product",
                params={
                    "source": "GFS",
                    "run_id": " " * 200,
                    "cycle_time": "2026-05-07T00:00:00Z",
                    "model_id": "basins_qhh_shud",
                },
            )
            date_only_cycle = client.get(
                "/api/v1/mvp/qhh/latest-product",
                params={
                    "source": "GFS",
                    "run_id": "qhh_gfs_2026050700",
                    "cycle_time": "2026-05-07",
                    "model_id": "basins_qhh_shud",
                },
            )
            malformed_cycle = client.get(
                "/api/v1/mvp/qhh/latest-product",
                params={
                    "source": "GFS",
                    "run_id": "qhh_gfs_2026050700",
                    "cycle_time": "not-a-time",
                    "model_id": "basins_qhh_shud",
                },
            )
            whitespace_run_id = client.get(
                "/api/v1/mvp/qhh/latest-product",
                params={
                    "source": "GFS",
                    "run_id": " qhh_gfs_2026050700 ",
                    "cycle_time": "2026-05-07T00:00:00Z",
                    "model_id": "basins_qhh_shud",
                },
            )
            whitespace_model_id = client.get(
                "/api/v1/mvp/qhh/latest-product",
                params={
                    "source": "GFS",
                    "run_id": "qhh_gfs_2026050700",
                    "cycle_time": "2026-05-07T00:00:00Z",
                    "model_id": " basins_qhh_shud ",
                },
            )
            blank_source = client.get(
                "/api/v1/mvp/qhh/latest-product",
                params={
                    "source": " " * 200,
                    "run_id": "qhh_gfs_2026050700",
                    "cycle_time": "2026-05-07T00:00:00Z",
                    "model_id": "basins_qhh_shud",
                },
            )
            run_id = "run-" + ("r" * 200)
            model_id = "model-" + ("m" * 200)
            bounded_unavailable = client.get(
                "/api/v1/mvp/qhh/latest-product",
                params={
                    "source": "GFS",
                    "run_id": run_id,
                    "cycle_time": "2026-05-07T00:00:00Z",
                    "model_id": model_id,
                },
            )
    finally:
        app.dependency_overrides.pop(get_forecast_store, None)

    assert strict_success.status_code == 200
    assert _assert_success_envelope(strict_success.json())["run_id"] == "qhh_gfs_2026050700"

    assert strict_unavailable.status_code == 404
    unavailable_error = strict_unavailable.json()["error"]
    assert unavailable_error["code"] == "QHH_LATEST_PRODUCT_UNAVAILABLE"
    assert unavailable_error["details"]["strict_identity"] is True
    assert unavailable_error["details"]["requested_identity"] == {
        "source": "GFS",
        "source_id": "GFS",
        "run_id": "wrong_run",
        "cycle_time": "2026-05-07T00:00:00Z",
        "model_id": "basins_qhh_shud",
    }

    assert partial.status_code == 422
    partial_error = partial.json()["error"]
    assert partial_error["code"] == "VALIDATION_ERROR"
    assert partial_error["details"] == {
        "missing_fields": ["cycle_time", "model_id"],
        "provided_fields": ["source", "run_id"],
        "required_fields": ["source", "run_id", "cycle_time", "model_id"],
        "strict_identity_required": True,
    }

    assert blank_run_id.status_code == 422
    assert blank_run_id.json()["error"]["details"]["rejected_values"]["run_id"] == f"{' ' * 61}..."
    assert date_only_cycle.status_code == 422
    assert date_only_cycle.json()["error"]["details"] == {
        "field": "cycle_time",
        "rejected_value": "2026-05-07",
    }
    assert malformed_cycle.status_code == 422
    assert malformed_cycle.json()["error"]["details"] == {
        "field": "cycle_time",
        "rejected_value": "not-a-time",
    }
    assert whitespace_run_id.status_code == 422
    assert whitespace_run_id.json()["error"]["details"]["field"] == "run_id"
    assert whitespace_model_id.status_code == 422
    assert whitespace_model_id.json()["error"]["details"]["field"] == "model_id"
    assert blank_source.status_code == 422
    assert blank_source.json()["error"]["details"]["rejected_values"]["source"] == f"{' ' * 61}..."

    assert bounded_unavailable.status_code == 404
    bounded_details = bounded_unavailable.json()["error"]["details"]
    expected_run_id = f"{run_id[:QHH_LATEST_REFLECTED_PREFIX_LIMIT]}..."
    expected_model_id = f"{model_id[:QHH_LATEST_REFLECTED_PREFIX_LIMIT]}..."
    assert bounded_details["requested_identity"]["run_id"] == expected_run_id
    assert bounded_details["requested_identity"]["model_id"] == expected_model_id
    assert bounded_details["unavailable_reasons"][0]["requested_identity"]["run_id"] == expected_run_id
    assert run_id not in bounded_unavailable.text
    assert model_id not in bounded_unavailable.text
    assert store.latest_qhh_calls == [
        {
            "source": "GFS",
            "run_id": "qhh_gfs_2026050700",
            "cycle_time": "2026-05-07T00:00:00Z",
            "model_id": "basins_qhh_shud",
        },
        {
            "source": "GFS",
            "run_id": "wrong_run",
            "cycle_time": "2026-05-07T00:00:00Z",
            "model_id": "basins_qhh_shud",
        },
        {
            "source": "GFS",
            "run_id": run_id,
            "cycle_time": "2026-05-07T00:00:00Z",
            "model_id": model_id,
        },
    ]


def test_jobs_contract_uses_success_envelope_and_paginated_pipeline_jobs() -> None:
    with _store() as store:
        cycle_time = _cycle_time()
        cycle_id = cycle_id_for("GFS", cycle_time)
        _insert_cycle(store, cycle_time=cycle_time)
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            response = client.get(
                "/api/v1/jobs",
                params={"source": "GFS", "cycle_time": cycle_time.isoformat(), "limit": 2, "offset": 0},
            )

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {"items", "total", "limit", "offset"}
    assert data["total"] == 5
    assert data["limit"] == 2
    assert data["offset"] == 0
    assert len(data["items"]) == 2
    assert set(data["items"][0]) == PIPELINE_JOB_KEYS
    assert data["items"][0]["run_type"] is None
    assert data["items"][0]["scenario"] is None


def test_pipeline_status_contract_uses_success_envelope() -> None:
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
    data = _assert_success_envelope(response.json())
    assert set(data) == {"cycle_id", "source", "cycle_time", "current_state", "started_at", "updated_at", "job_counts"}
    assert data["cycle_id"] == cycle_id
    assert data["current_state"] == "forecast_running"
    assert data["job_counts"] == {"succeeded": 3, "failed": 1, "running": 1, "pending": 0}


def test_pipeline_ops_strict_identity_query_contract() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    paths = spec["paths"]

    status_params = _parameter_names(paths["/api/v1/pipeline/status"]["get"], spec)
    stages_params = _parameter_names(paths["/api/v1/pipeline/stages"]["get"], spec)
    jobs_params = _parameter_names(paths["/api/v1/jobs"]["get"], spec)
    logs_params = _parameter_names(paths["/api/v1/jobs/{job_id}/logs"]["get"], spec)
    assert status_params == ["source", "cycle_time", "run_id", "model_id"]
    assert stages_params == ["source", "cycle_time", "run_id", "model_id"]
    assert jobs_params[:4] == ["source", "cycle_time", "run_id", "status"]
    assert "model_id" in jobs_params
    assert logs_params == ["job_id", "source", "cycle_time", "run_id", "model_id"]

    generated_types = (
        Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    ).read_text(encoding="utf-8")
    status_start = generated_types.index("getPipelineStatus:")
    stages_start = generated_types.index("listPipelineStages:")
    jobs_start = generated_types.index("listPipelineJobs:")
    logs_start = generated_types.index("getPipelineJobLogs:")
    queue_start = generated_types.index("queue_depth_api_v1_queue_depth_get:")
    status_types = generated_types[status_start:stages_start]
    stages_types = generated_types[stages_start:jobs_start]
    jobs_types = generated_types[jobs_start:logs_start]
    logs_types = generated_types[logs_start:queue_start]
    for snippet in (status_types, stages_types, jobs_types, logs_types):
        assert "run_id?: string;" in snippet
    assert "source: string;" in status_types
    assert "cycle_time: string;" in status_types
    assert "source: string;" in stages_types
    assert "cycle_time: string;" in stages_types
    assert "source?: string;" in logs_types
    assert "model_id?: string;" in logs_types
    for path in (
        "/api/v1/pipeline/status",
        "/api/v1/pipeline/stages",
        "/api/v1/jobs",
        "/api/v1/jobs/{job_id}/logs",
    ):
        run_id = next(parameter for parameter in paths[path]["get"]["parameters"] if parameter.get("name") == "run_id")
        assert run_id["schema"] == {"type": "string", "maxLength": 128}
    for path in (
        "/api/v1/pipeline/status",
        "/api/v1/pipeline/stages",
        "/api/v1/jobs",
        "/api/v1/jobs/{job_id}/logs",
    ):
        model_id = next(
            parameter
            for parameter in paths[path]["get"]["parameters"]
            if parameter.get("name") == "model_id"
        )
        assert model_id["schema"] == {"type": "string", "maxLength": 128}


def test_pipeline_stage_contract_exposes_formal_job_evidence_and_ops_statuses() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]

    basin_result = schemas["BasinResult"]
    assert set(basin_result["required"]) == STAGE_JOB_EVIDENCE_KEYS
    persisted_statuses = _persisted_pipeline_job_statuses()
    assert OPS_JOB_STATUS_ENUM == persisted_statuses
    assert basin_result["properties"]["status"]["enum"] == persisted_statuses
    assert schemas["PipelineJob"]["properties"]["status"]["enum"] == persisted_statuses
    assert schemas["RetryRunResult"]["properties"]["status"]["enum"] == ["submitted"]
    assert schemas["PipelineStage"]["properties"]["basin_results"]["maxItems"] == (
        pipeline_routes.PIPELINE_STAGE_BASIN_RESULTS_LIMIT
    )
    assert schemas["PipelineStage"]["properties"]["basin_results_total"]["type"] == "integer"
    assert schemas["PipelineStage"]["properties"]["basin_results_truncated"]["type"] == "boolean"
    max_public_log_uri_length = pipeline_routes.PIPELINE_PUBLIC_LOG_URI_MAX_LENGTH
    assert schemas["PipelineJob"]["properties"]["log_uri"]["maxLength"] == max_public_log_uri_length
    assert basin_result["properties"]["log_uri"]["maxLength"] == max_public_log_uri_length

    generated_types = (
        Path(__file__).resolve().parents[1] / "apps" / "frontend" / "src" / "api" / "types.ts"
    ).read_text(encoding="utf-8")
    basin_result_start = generated_types.index("BasinResult:")
    pipeline_job_start = generated_types.index("PipelineJob:")
    basin_result_types = generated_types[basin_result_start:pipeline_job_start]
    assert "job_id: string;" in basin_result_types
    assert "run_id: string | null;" in basin_result_types
    assert "slurm_job_id: string | null;" in basin_result_types
    assert "submitted_at: string | null;" in basin_result_types
    assert "duration_seconds: number | null;" in basin_result_types
    assert "retry_count: number;" in basin_result_types
    pipeline_stage_start = generated_types.index("PipelineStage:")
    pipeline_stage_types = generated_types[pipeline_stage_start:pipeline_job_start]
    assert "basin_results_truncated: boolean;" in pipeline_stage_types
    assert '"queued"' in basin_result_types
    assert '"skipped"' in basin_result_types
    assert '"submission_failed"' in basin_result_types
    assert '"permanently_failed"' in basin_result_types
    retry_start = generated_types.index("RetryRunResult:")
    runtime_config_start = generated_types.index("RuntimeConfig:")
    retry_types = generated_types[retry_start:runtime_config_start]
    assert 'status: "submitted";' in retry_types
    assert '"submission_failed"' not in retry_types


def test_production_identity_status_schema_examples_match_contract_constants() -> None:
    schema_dir = Path(__file__).resolve().parents[1] / "schemas"
    pairs = {
        "pipeline_job": (
            json.loads((schema_dir / "pipeline_job.schema.json").read_text(encoding="utf-8")),
            json.loads((schema_dir / "examples" / "pipeline_job.example.json").read_text(encoding="utf-8")),
        ),
        "run_manifest": (
            json.loads((schema_dir / "run_manifest.schema.json").read_text(encoding="utf-8")),
            json.loads((schema_dir / "examples" / "run_manifest.example.json").read_text(encoding="utf-8")),
        ),
        "run_status": (
            json.loads((schema_dir / "run_status.schema.json").read_text(encoding="utf-8")),
            json.loads((schema_dir / "examples" / "run_status.example.json").read_text(encoding="utf-8")),
        ),
    }

    for name, (schema, example) in pairs.items():
        _assert_schema_example_shape(schema, example, path=name)
        identity_schema = schema["properties"]["identity"]
        identity_example = example["identity"]
        assert "basin_id" in identity_schema["required"]
        assert "pipeline_job_id" not in identity_schema["required"]
        for field in identity_schema["required"]:
            assert identity_example.get(field) not in (None, "")
        if "production_stage" in schema["properties"]:
            assert schema["properties"]["production_stage"]["enum"] == list(PRODUCTION_STAGE_TAXONOMY)
            assert example["production_stage"] in PRODUCTION_STAGE_TAXONOMY
        if "production_status" in schema["properties"]:
            assert schema["properties"]["production_status"]["enum"] == list(PRODUCTION_STATUS_TAXONOMY)
            assert example["production_status"] in PRODUCTION_STATUS_TAXONOMY


def test_queue_depth_contract_uses_success_envelope() -> None:
    with _store() as store:
        with _client(store, _MockGateway(depth={"running": 2, "pending": 3, "idle": 1})) as client:
            response = client.get("/api/v1/queue/depth")

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {"running", "pending", "idle"}
    assert data == {"running": 2, "pending": 3, "idle": 1}


def test_stage_duration_metrics_contract_uses_success_envelope() -> None:
    with _store() as store:
        cycle_id = cycle_id_for("GFS", _cycle_time())
        _seed_monitoring_jobs(store, cycle_id=cycle_id)
        with _client(store) as client:
            response = client.get("/api/v1/metrics/stage-duration", params={"days": 30})

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert isinstance(data, list)
    metric = next(row for row in data if row["stage"] == "download")
    assert set(metric) == {"date", "stage", "average_duration_seconds", "job_count"}
    assert isinstance(metric["date"], str)
    assert isinstance(metric["average_duration_seconds"], float)
    assert isinstance(metric["job_count"], int)


def test_retry_contract_documents_pipeline_job_and_execution_status_fields() -> None:
    with _store() as store:
        _create_job(
            store,
            job_id="job_retry_contract",
            run_id="run_retry_contract",
            job_type=GENERIC_RETRY_JOB_TYPE,
            stage="forecast",
            status="failed",
        )
        with _client(store, _RetryGateway(), allow_dev_role_header=True) as client:
            response = client.post("/api/v1/runs/run_retry_contract/retry", headers={"X-User-Role": "operator"})

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {
        "job_id",
        "pipeline_job_id",
        "run_id",
        "retry_count",
        "status",
        "slurm_job_id",
        "execution_status",
    }
    assert data["pipeline_job_id"] == data["job_id"]
    assert data["run_id"] == "run_retry_contract"
    assert data["execution_status"] == "submitted"
    assert data["slurm_job_id"] == "slurm_retry_contract"


def test_cancel_contract_documents_cancelled_jobs_and_slurm_failures() -> None:
    with _store() as store:
        gateway = _MockGateway()
        _create_job(
            store,
            job_id="job_cancel_contract",
            run_id="run_cancel_contract",
            status="running",
            slurm_job_id="slurm_cancel_contract",
        )
        with _client(store, gateway, allow_dev_role_header=True) as client:
            response = client.post("/api/v1/runs/run_cancel_contract/cancel", headers={"X-User-Role": "operator"})

    assert response.status_code == 200
    data = _assert_success_envelope(response.json())
    assert set(data) == {
        "run_id",
        "cancelled_jobs",
        "cancelled",
        "failed_jobs",
        "slurm_failures",
        "blocked_jobs",
        "slurm_cancellation_gaps",
        "partial_failure",
        "idempotent_jobs",
        "hydro_run",
        "forecast_cycle",
    }
    assert data["run_id"] == "run_cancel_contract"
    assert data["failed_jobs"] == []
    assert data["slurm_failures"] == []
    assert data["blocked_jobs"] == []
    assert data["slurm_cancellation_gaps"] == []
    assert data["partial_failure"] is False
    assert data["cancelled"] == data["cancelled_jobs"]
    assert len(data["cancelled_jobs"]) == 1
    assert set(data["cancelled_jobs"][0]) == PIPELINE_JOB_KEYS
    assert data["cancelled_jobs"][0]["status"] == "cancelled"
