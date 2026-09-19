from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from tests.test_monitoring_api import (
    _client,
    _create_job,
    _insert_cycle,
    _insert_hydro_run,
    _store,
)
from workers.data_adapters.base import cycle_id_for

SELECTED_RUN_ID = "fcst_gfs_2026091612_dg_0883c7e9c1006c6fd347df500315e9df"
SELECTED_MODEL_ID = "dg_0883c7e9c1006c6fd347df500315e9df"
SELECTED_JOB_ID = "job_fcst_gfs_2026091612_dg_0883c7e9c1006c6fd347df500315e9df_forecast_reconciled_50025_0"
CYCLE_TIME = datetime(2026, 9, 16, 12, tzinfo=UTC)


def test_strict_ops_success_bodies_carry_top_level_identity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    with _store() as store:
        cycle_id = cycle_id_for("GFS", CYCLE_TIME)
        _insert_cycle(store, cycle_time=CYCLE_TIME, source="GFS", current_state="forecast_running")
        _insert_hydro_run(
            store,
            SELECTED_RUN_ID,
            status="succeeded",
            source_id="gfs",
            cycle_time=CYCLE_TIME,
            model_id=SELECTED_MODEL_ID,
        )
        (tmp_path / "selected.log").write_text("selected log", encoding="utf-8")
        _create_job(
            store,
            job_id=SELECTED_JOB_ID,
            run_id=SELECTED_RUN_ID,
            cycle_id=cycle_id,
            model_id=SELECTED_MODEL_ID,
            stage="forecast",
            status="succeeded",
            log_uri="selected.log",
        )
        strict_params = {
            "source": "GFS",
            "cycle_time": CYCLE_TIME.isoformat(),
            "run_id": SELECTED_RUN_ID,
            "model_id": SELECTED_MODEL_ID,
        }
        expected_identity = {
            "source": "GFS",
            "cycle_time": CYCLE_TIME.isoformat(),
            "run_id": SELECTED_RUN_ID,
            "model_id": SELECTED_MODEL_ID,
        }
        with _client(store) as client:
            status_response = client.get("/api/v1/pipeline/status", params=strict_params)
            stages_response = client.get("/api/v1/pipeline/stages", params=strict_params)
            jobs_response = client.get("/api/v1/jobs", params={**strict_params, "limit": 10, "sort_order": "asc"})
            logs_response = client.get(f"/api/v1/jobs/{SELECTED_JOB_ID}/logs", params=strict_params)

    assert status_response.status_code == 200
    assert stages_response.status_code == 200
    assert jobs_response.status_code == 200
    assert logs_response.status_code == 200
    assert status_response.json()["identity"] == expected_identity
    assert stages_response.json()["identity"] == expected_identity
    assert jobs_response.json()["identity"] == expected_identity
    assert logs_response.json()["identity"] == {**expected_identity, "job_id": SELECTED_JOB_ID}
    assert "identity" not in status_response.json()["data"]
    assert logs_response.json()["data"]["content"] == "selected log"
    assert logs_response.json()["data"]["job_id"] == SELECTED_JOB_ID


def test_openapi_scopes_strict_identity_to_ops_success_responses() -> None:
    spec_path = Path(__file__).resolve().parents[1] / "openapi" / "nhms.v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    success_envelope = spec["components"]["schemas"]["SuccessEnvelope"]
    assert "identity" not in success_envelope["properties"]
    identity_schema = spec["components"]["schemas"]["OpsStrictIdentity"]
    assert identity_schema["required"] == ["source", "cycle_time", "run_id", "model_id"]

    for path in ("/api/v1/pipeline/status", "/api/v1/pipeline/stages", "/api/v1/jobs"):
        response_schema = spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        response_fields = response_schema["allOf"][1]
        assert response_fields["required"] == ["data"]
        identity_property = response_fields["properties"]["identity"]
        assert identity_property["allOf"][0] == {"$ref": "#/components/schemas/OpsStrictIdentity"}


    logs_schema = spec["paths"]["/api/v1/jobs/{job_id}/logs"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    log_identity = logs_schema["allOf"][1]["properties"]["identity"]
    assert logs_schema["allOf"][1]["required"] == ["data"]
    assert log_identity["allOf"][0] == {"$ref": "#/components/schemas/OpsStrictIdentity"}
    assert log_identity["allOf"][1]["required"] == ["job_id"]


def test_non_strict_ops_success_bodies_omit_identity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    with _store() as store:
        cycle_id = cycle_id_for("GFS", CYCLE_TIME)
        _insert_cycle(store, cycle_time=CYCLE_TIME, source="GFS", current_state="forecast_running")
        _insert_hydro_run(
            store,
            SELECTED_RUN_ID,
            status="succeeded",
            source_id="gfs",
            cycle_time=CYCLE_TIME,
            model_id=SELECTED_MODEL_ID,
        )
        (tmp_path / "selected.log").write_text("selected log", encoding="utf-8")
        _create_job(
            store,
            job_id=SELECTED_JOB_ID,
            run_id=SELECTED_RUN_ID,
            cycle_id=cycle_id,
            model_id=SELECTED_MODEL_ID,
            stage="forecast",
            status="succeeded",
            log_uri="selected.log",
        )
        with _client(store) as client:
            status_response = client.get(
                "/api/v1/pipeline/status",
                params={"source": "GFS", "cycle_time": CYCLE_TIME.isoformat()},
            )
            stages_response = client.get(
                "/api/v1/pipeline/stages",
                params={"source": "GFS", "cycle_time": CYCLE_TIME.isoformat()},
            )
            jobs_response = client.get("/api/v1/jobs", params={"limit": 10, "sort_order": "asc"})
            logs_response = client.get(f"/api/v1/jobs/{SELECTED_JOB_ID}/logs")

    assert status_response.status_code == 200
    assert stages_response.status_code == 200
    assert jobs_response.status_code == 200
    assert logs_response.status_code == 200
    assert "identity" not in status_response.json()
    assert "identity" not in stages_response.json()
    assert "identity" not in jobs_response.json()
    assert "identity" not in logs_response.json()
    assert logs_response.json()["data"]["content"] == "selected log"


