from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from services.production_closure.readonly_db_route_smoke import (
    _route_response_identity,
    _route_response_identity_blockers,
)
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
# Independent latest-product UTC spelling, not derived from datetime.isoformat().
CANONICAL_CYCLE_TIME = "2026-09-16T12:00:00Z"
CANONICAL_IDENTITY = {
    "source": "GFS",
    "cycle_time": CANONICAL_CYCLE_TIME,
    "run_id": SELECTED_RUN_ID,
    "model_id": SELECTED_MODEL_ID,
}


def test_strict_ops_success_bodies_carry_top_level_identity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    with _store() as store:
        _seed_selected_ops_cycle(store, tmp_path)
        strict_params = {
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
    assert status_response.json()["identity"] == CANONICAL_IDENTITY
    assert stages_response.json()["identity"] == CANONICAL_IDENTITY
    assert jobs_response.json()["identity"] == CANONICAL_IDENTITY
    assert logs_response.json()["identity"] == {**CANONICAL_IDENTITY, "job_id": SELECTED_JOB_ID}
    assert "identity" not in status_response.json()["data"]
    assert logs_response.json()["data"]["content"] == "selected log"
    assert logs_response.json()["data"]["job_id"] == SELECTED_JOB_ID


def _seed_selected_ops_cycle(store: Any, tmp_path: Path) -> None:
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


# The smoke's expected identity comes either from a latest-product-shaped selector
# (``Z``) or, on the unattended path, from discovery's ``datetime.isoformat()``
# (``+00:00``, #2484). Both spell the same cycle hour the ops echo carries as ``Z``.
DISCOVERY_CYCLE_TIME = "2026-09-16T12:00:00+00:00"


@pytest.mark.parametrize("expected_cycle_time", [CANONICAL_CYCLE_TIME, DISCOVERY_CYCLE_TIME])
@pytest.mark.parametrize("requested_cycle_time", ["2026-09-16T12:00:00Z", "2026-09-16T20:00:00+08:00"])
def test_strict_ops_success_identities_match_latest_product_shaped_canonical_selector(
    tmp_path: Path, monkeypatch, requested_cycle_time: str, expected_cycle_time: str
) -> None:
    monkeypatch.setenv("LOG_ROOT", str(tmp_path))
    latest_product_body = {
        "status": "ok",
        "data": {
            "source_id": "GFS",
            "cycle_time": CANONICAL_CYCLE_TIME,
            "run_id": SELECTED_RUN_ID,
            "model_id": SELECTED_MODEL_ID,
        },
    }
    canonical_selector = _route_response_identity("latest_product", latest_product_body)
    assert canonical_selector["cycle_time"] == CANONICAL_CYCLE_TIME
    with _store() as store:
        _seed_selected_ops_cycle(store, tmp_path)
        strict_params = {
            "source": "GFS",
            "cycle_time": requested_cycle_time,
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

    status_body = status_response.json()
    stages_body = stages_response.json()
    jobs_body = jobs_response.json()
    logs_body = logs_response.json()

    assert status_body["identity"] == CANONICAL_IDENTITY
    assert stages_body["identity"] == CANONICAL_IDENTITY
    assert jobs_body["identity"] == CANONICAL_IDENTITY
    assert logs_body["identity"] == {**CANONICAL_IDENTITY, "job_id": SELECTED_JOB_ID}
    assert "identity" not in status_body["data"]
    assert status_body["data"]["source"] == "GFS"
    assert status_body["data"]["cycle_id"] == cycle_id_for("GFS", CYCLE_TIME)
    assert [job["job_id"] for job in jobs_body["data"]["items"]] == [SELECTED_JOB_ID]
    assert "forecast" in {stage["stage"] for stage in stages_body["data"]}
    assert logs_body["data"]["content"] == "selected log"
    assert logs_body["data"]["job_id"] == SELECTED_JOB_ID

    for route_name, body in (
        ("pipeline_status", status_body),
        ("pipeline_stages", stages_body),
        ("jobs", jobs_body),
        ("job_logs", logs_body),
    ):
        observed = _route_response_identity(route_name, body)
        expected = {**canonical_selector, "cycle_time": expected_cycle_time}
        if route_name == "job_logs":
            expected["job_id"] = SELECTED_JOB_ID
        assert _route_response_identity_blockers(
            {"name": route_name, "strict_identity": expected},
            observed,
        ) == []


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


