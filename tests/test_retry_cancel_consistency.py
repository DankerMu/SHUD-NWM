from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.main import app, create_app
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

        retry = service.attempt_manual_retry("run_retry", gateway=gateway, trusted_internal=True)

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


@pytest.mark.parametrize("active_status", ["pending", "queued", "submitted", "running"])
def test_duplicate_retry_returns_conflict(active_status: str) -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_conflict", status="failed")
        _create_job(store, job_id=f"job_{active_status}", run_id="run_conflict", status=active_status)
        service = RetryService(store, RetryConfig(max_retries=3))
        with _client(store, retry_service=service) as client:
            response = client.post("/api/v1/runs/run_conflict/retry", headers=_operator_headers())

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "RETRY_CONFLICT"
        assert response.json()["error"]["details"]["active_job_id"] == f"job_{active_status}"
        assert response.json()["error"]["details"]["active_status"] == active_status


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

        retry = service.attempt_manual_retry("run_retry_event", gateway=gateway, trusted_internal=True)

        event = _events(store)[0]
        assert event.entity_id == retry.job_id
        assert event.details["trigger"] == "manual"
        assert event.details["previous_job_id"] == failed.job_id
        assert event.details["previous_error"] == "SBATCH_SUBMISSION_FAILED"
        assert event.details["slurm_job_id"] is None
        assert event.details["manual_retry_marker"] is True
        assert event.details["prior_failure_reason"] == "SBATCH_SUBMISSION_FAILED"
        assert event.details["failure"]["manual_retry_marker"] is True


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


def test_cancel_gateway_error_redacts_response_and_event_details() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_cancel_secret", status="running")
        _insert_forecast_cycle(store, "cycle_cancel_secret", status="forecast_running")
        _create_job(
            store,
            job_id="job_cancel_secret",
            run_id="run_cancel_secret",
            cycle_id="cycle_cancel_secret",
            status="running",
            slurm_job_id="slurm_secret",
        )
        gateway = _MockGateway(
            failures={
                "slurm_secret": SlurmGatewayError(
                    502,
                    "SLURM_ERROR",
                    "scancel failed token=tok123 Authorization: Bearer live-token-123 "
                    "{\"Authorization\": \"Bearer json-cancel-token-123\"} "
                    "for https://alice:pass123@slurm.example/cancel?signature=sig123",
                    {
                        "code": "SCANCEL_FAILED",
                        "status": "failed",
                        "password": "pass123",
                        "Authorization": "Bearer payload-token-123",
                        "stderr": (
                            "token=tok123 authorization=Basic basic-secret-123 "
                            "Proxy-Authorization: 'Basic quoted-cancel-proxy-secret-123' "
                            "url=https://alice:pass123@slurm.example/cancel?signature=sig123"
                        ),
                    },
                )
            }
        )
        with _client(store, gateway=gateway) as client:
            response = client.post("/api/v1/runs/run_cancel_secret/cancel", headers=_operator_headers())

        assert response.status_code == 200
        data = response.json()["data"]
        failure = data["failed_jobs"][0]
        assert data["partial_failure"] is True
        assert failure["job_id"] == "job_cancel_secret"
        assert failure["status"] == "running"
        assert failure["slurm_job_id"] == "slurm_secret"
        assert failure["cancellation_proven"] is False
        assert failure["error"]["status_code"] == 502
        assert failure["error"]["code"] == "SLURM_ERROR"
        assert failure["error"]["details"]["code"] == "SCANCEL_FAILED"
        assert failure["error"]["details"]["status"] == "failed"
        assert failure["error"]["details"]["password"] == "[redacted]"
        assert failure["error"]["details"]["Authorization"] == "[redacted]"
        event = next(event for event in _events(store) if event.event_type == "cancel_failed")
        assert event.details["previous_status"] == "running"
        assert event.details["cancellation_proven"] is False
        assert event.details["error"]["code"] == "SLURM_ERROR"
        response_body = json.dumps(response.json(), sort_keys=True)
        event_body = json.dumps({"message": event.message, "details": event.details}, sort_keys=True)
        for raw_secret in (
            "alice:pass123",
            "pass123",
            "tok123",
            "sig123",
            "live-token-123",
            "json-cancel-token-123",
            "payload-token-123",
            "basic-secret-123",
            "quoted-cancel-proxy-secret-123",
        ):
            assert raw_secret not in response_body
            assert raw_secret not in event_body
        assert "[redacted]" in response_body
        assert "[redacted]" in event_body


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
        assert data["partial_failure"] is True
        assert data["idempotent_jobs"][0]["job_id"] == "job_terminal_slurm"
        assert data["slurm_cancellation_gaps"][0]["job_id"] == "job_terminal_slurm"
        assert data["slurm_cancellation_gaps"][0]["cancellation_proven"] is False
        assert store.get_job("job_terminal_slurm").status == "running"
        assert _hydro_status(store, "run_terminal_slurm") == "running"
        assert _cycle_status(store, "cycle_terminal_slurm") == "forecast_running"
        event = next(event for event in _events(store) if event.event_type == "slurm_cancellation_gap")
        assert event.status_to == "blocked"
        assert event.details["cancellation_proven"] is False


def test_unproven_cancel_gateway_response_redacts_response_and_event_details() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_unproven_secret", status="running")
        _insert_forecast_cycle(store, "cycle_unproven_secret", status="forecast_running")
        _create_job(
            store,
            job_id="job_unproven_secret",
            run_id="run_unproven_secret",
            cycle_id="cycle_unproven_secret",
            status="submitted",
            slurm_job_id="slurm_unproven_secret",
        )
        gateway = _MockGateway(
            responses={
                "slurm_unproven_secret": {
                    "job_id": "slurm_unproven_secret",
                    "status": "pending",
                    "cancellation_proven": False,
                    "token": "tok987",
                    "authorization": "Bearer response-token-987",
                    "auth": {
                        "issuer_url": "https://service:issuer-pass@idp.example.invalid/auth?token=issuer-query-secret",
                        "value": "opaque-cancel-auth-token-987",
                        "permissions": ["jobs.cancel", {"provider": "opaque-cancel-permission-token-987"}],
                        "errors": [{"status": "opaque-cancel-error-token-987"}],
                        "scope": {
                            "provider": "opaque-cancel-provider-token-987",
                            "status": "opaque-cancel-status-token-987",
                            "message": "opaque-cancel-scope-token-987",
                        },
                    },
                    "callback_url": "https://bob:pass987@slurm.example/cancel?X-Amz-Signature=sig987",
                    "details": {
                        "code": "SCANCEL_PENDING",
                        "status": "pending",
                        "password": "pass987",
                        "stderr": "Authorization: Basic basic-secret-987",
                    },
                }
            }
        )
        with _client(store, gateway=gateway) as client:
            response = client.post("/api/v1/runs/run_unproven_secret/cancel", headers=_operator_headers())

        assert response.status_code == 200
        data = response.json()["data"]
        gap = data["slurm_cancellation_gaps"][0]
        assert data["partial_failure"] is True
        assert gap["job_id"] == "job_unproven_secret"
        assert gap["run_id"] == "run_unproven_secret"
        assert gap["status"] == "submitted"
        assert gap["slurm_job_id"] == "slurm_unproven_secret"
        assert gap["cancellation_proven"] is False
        assert gap["gateway_response"]["job_id"] == "slurm_unproven_secret"
        assert gap["gateway_response"]["status"] == "pending"
        assert gap["gateway_response"]["cancellation_proven"] is False
        assert gap["gateway_response"]["token"] == "[redacted]"
        assert gap["gateway_response"]["authorization"] == "[redacted]"
        assert gap["gateway_response"]["auth"]["issuer_url"] == "https://idp.example.invalid/auth"
        assert gap["gateway_response"]["auth"]["value"] == "[redacted]"
        assert gap["gateway_response"]["auth"]["permissions"] == ["jobs.cancel", {"provider": "[redacted]"}]
        assert gap["gateway_response"]["auth"]["errors"] == [{"status": "[redacted]"}]
        assert gap["gateway_response"]["auth"]["scope"]["provider"] == "[redacted]"
        assert gap["gateway_response"]["auth"]["scope"]["status"] == "[redacted]"
        assert gap["gateway_response"]["auth"]["scope"]["message"] == "[redacted]"
        assert gap["gateway_response"]["details"]["code"] == "SCANCEL_PENDING"
        assert gap["gateway_response"]["details"]["status"] == "pending"
        assert gap["gateway_response"]["details"]["password"] == "[redacted]"
        event = next(event for event in _events(store) if event.event_type == "slurm_cancellation_gap")
        assert event.details["previous_status"] == "submitted"
        assert event.details["cancellation_proven"] is False
        assert event.details["gateway_response"]["status"] == "pending"
        assert event.details["gateway_response"]["auth"]["issuer_url"] == "https://idp.example.invalid/auth"
        assert event.details["gateway_response"]["auth"]["value"] == "[redacted]"
        assert event.details["gateway_response"]["auth"]["permissions"] == ["jobs.cancel", {"provider": "[redacted]"}]
        assert event.details["gateway_response"]["auth"]["errors"] == [{"status": "[redacted]"}]
        assert event.details["gateway_response"]["auth"]["scope"]["provider"] == "[redacted]"
        assert event.details["gateway_response"]["auth"]["scope"]["status"] == "[redacted]"
        assert event.details["gateway_response"]["auth"]["scope"]["message"] == "[redacted]"
        response_body = json.dumps(response.json(), sort_keys=True)
        event_body = json.dumps({"message": event.message, "details": event.details}, sort_keys=True)
        for raw_secret in (
            "bob:pass987",
            "pass987",
            "tok987",
            "sig987",
            "response-token-987",
            "service:issuer-pass",
            "issuer-query-secret",
            "opaque-cancel-auth-token-987",
            "opaque-cancel-permission-token-987",
            "opaque-cancel-error-token-987",
            "opaque-cancel-provider-token-987",
            "opaque-cancel-status-token-987",
            "opaque-cancel-scope-token-987",
            "basic-secret-987",
        ):
            assert raw_secret not in response_body
            assert raw_secret not in event_body
        assert "[redacted]" in response_body
        assert "[redacted]" in event_body


def test_cancel_conflict_terminal_slurm_job_preserves_local_state() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_conflict_terminal_slurm", status="running")
        _insert_forecast_cycle(store, "cycle_conflict_terminal_slurm", status="forecast_running")
        _create_job(
            store,
            job_id="job_conflict_terminal_slurm",
            run_id="run_conflict_terminal_slurm",
            cycle_id="cycle_conflict_terminal_slurm",
            status="submitted",
            slurm_job_id="slurm_terminal",
        )
        gateway = _MockGateway(
            failures={
                "slurm_terminal": SlurmGatewayError(
                    409,
                    "JOB_ALREADY_TERMINAL",
                    "Job is already terminal with status succeeded.",
                    {"job_id": "slurm_terminal", "status": "succeeded"},
                )
            }
        )
        with _client(store, gateway=gateway) as client:
            response = client.post("/api/v1/runs/run_conflict_terminal_slurm/cancel", headers=_operator_headers())

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["partial_failure"] is True
        assert data["cancelled_jobs"] == []
        assert data["slurm_cancellation_gaps"][0]["error"]["code"] == "JOB_ALREADY_TERMINAL"
        assert store.get_job("job_conflict_terminal_slurm").status == "submitted"
        assert _hydro_status(store, "run_conflict_terminal_slurm") == "running"
        assert _cycle_status(store, "cycle_conflict_terminal_slurm") == "forecast_running"


@pytest.mark.parametrize("active_status", ["queued", "submitted", "running"])
def test_cancel_treats_queued_job_as_active_like_submitted_and_running(active_status: str) -> None:
    with _store() as store:
        _insert_hydro_run(store, f"run_cancel_{active_status}", status="running")
        _insert_forecast_cycle(store, f"cycle_cancel_{active_status}", status="forecast_running")
        _create_job(
            store,
            job_id=f"job_cancel_{active_status}",
            run_id=f"run_cancel_{active_status}",
            cycle_id=f"cycle_cancel_{active_status}",
            status=active_status,
            slurm_job_id=f"slurm_{active_status}",
        )
        gateway = _MockGateway()
        with _client(store, gateway=gateway) as client:
            response = client.post(f"/api/v1/runs/run_cancel_{active_status}/cancel", headers=_operator_headers())

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["partial_failure"] is False
        assert [job["job_id"] for job in data["cancelled_jobs"]] == [f"job_cancel_{active_status}"]
        assert gateway.cancelled == [f"slurm_{active_status}"]
        assert store.get_job(f"job_cancel_{active_status}").status == "cancelled"
        event = next(event for event in _events(store) if event.entity_type == "pipeline_job")
        assert event.status_from == active_status
        assert event.status_to == "cancelled"


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


def test_cancel_success_projection_redacts_persisted_log_uri_and_error_message() -> None:
    secret_uri = "s3://bucket/logs/job.out?token=supersecret"
    secret_message = "old error https://user:pass@example.test/log?signature=abc token=rawsecret"
    with _store() as store:
        _insert_hydro_run(store, "run_cancel_success_secret", status="running")
        _insert_forecast_cycle(store, "cycle_cancel_success_secret", status="forecast_running")
        job = _create_job(
            store,
            job_id="job_cancel_success_secret",
            run_id="run_cancel_success_secret",
            cycle_id="cycle_cancel_success_secret",
            status="running",
            slurm_job_id=None,
        )
        job.log_uri = secret_uri
        job.error_message = secret_message
        store.session.add(job)
        store.session.commit()

        with _client(store) as client:
            response = client.post("/api/v1/runs/run_cancel_success_secret/cancel", headers=_operator_headers())

        assert response.status_code == 200
        data = response.json()["data"]
        cancelled = data["cancelled_jobs"][0]
        response_text = json.dumps(data)
        assert cancelled["job_id"] == "job_cancel_success_secret"
        assert cancelled["status"] == "cancelled"
        assert cancelled["log_uri"] == "s3://bucket/logs/job.out"
        assert "supersecret" not in response_text
        assert "rawsecret" not in response_text
        assert "user:pass" not in response_text
        assert "signature=abc" not in response_text


def test_job_log_forbidden_response_redacts_secret_file_uri(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    log_root = tmp_path / "logs"
    outside_root = tmp_path / "outside"
    log_root.mkdir()
    outside_root.mkdir()
    secret_log_uri = (
        f"file://{outside_root}/job.log?"
        "token=rawsecret&password=hunter2&X-Amz-Signature=signedsecret"
    )
    monkeypatch.setenv("LOG_ROOT", str(log_root))
    with _store() as store:
        job = _create_job(
            store,
            job_id="job_forbidden_secret_log",
            run_id="run_forbidden_secret_log",
            status="failed",
            error_code="NODE_FAILURE",
        )
        job.log_uri = secret_log_uri
        store.session.add(job)
        store.session.commit()

        with _client(store) as client:
            response = client.get("/api/v1/jobs/job_forbidden_secret_log/logs", headers=_operator_headers())

    response_text = json.dumps(response.json())
    error = response.json()["error"]
    assert response.status_code == 400
    assert error["code"] == "JOB_LOG_URI_UNSUPPORTED"
    assert error["details"] == {
        "job_id": "job_forbidden_secret_log",
        "log_uri": "file://redacted",
        "reason": "query_or_fragment_forbidden",
    }
    assert str(log_root.resolve()) not in response_text
    assert str(outside_root.resolve()) not in response_text
    assert "rawsecret" not in response_text
    assert "hunter2" not in response_text
    assert "signedsecret" not in response_text
    assert "token=" not in response_text
    assert "password=" not in response_text
    assert "X-Amz-Signature" not in response_text
    assert "unsafe_log_path" not in response_text
    assert "Traceback" not in response_text
    assert "ValueError" not in response_text


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


def test_cancelled_run_manual_retry_submits_marker_and_pending_hydro() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_retry_cancelled", status="cancelled")
        cancelled = _create_job(
            store,
            job_id="job_cancelled",
            run_id="run_retry_cancelled",
            cycle_id="cycle_retry_cancelled",
            status="cancelled",
            error_code=None,
        )
        cancelled.retry_count = 2
        store.session.add(cancelled)
        store.session.commit()
        gateway = _MockGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("run_retry_cancelled", gateway=gateway, trusted_internal=True)

        assert retry.status == "submitted"
        assert retry.retry_count == 3
        assert _hydro_status(store, "run_retry_cancelled") == "pending"
        assert _hydro_status(store, "run_retry_cancelled") in HYDRO_RUN_STATUS_ENUM
        events = _events(store)
        retry_event = next(event for event in events if event.event_type == "retry")
        assert retry_event.status_from == "cancelled"
        assert retry_event.details["manual_retry_marker"] is True
        assert retry_event.details["previous_job_id"] == "job_cancelled"
        assert retry_event.details["previous_error"] == "cancelled"
        assert retry_event.details["prior_failure_reason"] == "cancelled"
        assert retry_event.details["retry_count"] == 3
        assert gateway.submissions


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

        service.attempt_manual_retry("run_retry_valid_enum", gateway=gateway, trusted_internal=True)

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


@pytest.mark.parametrize("hydro_status", ["succeeded", "parsed", "frequency_done", "published"])
def test_retry_preserves_terminal_hydro_status(hydro_status: str) -> None:
    with _store() as store:
        run_id = f"run_retry_terminal_{hydro_status}"
        _insert_hydro_run(store, run_id, status=hydro_status)
        failed = _create_job(
            store,
            job_id=f"job_retry_terminal_{hydro_status}",
            run_id=run_id,
            status="failed",
            error_code="NODE_FAILURE",
        )
        failed.updated_at = datetime(2026, 5, 1, 6, 20, tzinfo=UTC)
        store.session.add(failed)
        store.session.commit()
        gateway = _MockGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryNotFoundError):
            service.attempt_manual_retry(run_id, gateway=gateway, trusted_internal=True)

        assert gateway.submissions == []
        assert _hydro_status(store, run_id) == hydro_status
        assert _hydro_status(store, run_id) in HYDRO_RUN_STATUS_ENUM
        assert [job.job_id for job in store.query_jobs_by_run(run_id)] == [failed.job_id]
        assert _events(store) == []


def test_retry_api_rejects_terminal_hydro_status_without_mutation() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_retry_terminal_api", status="published")
        _create_job(
            store,
            job_id="job_retry_terminal_api",
            run_id="run_retry_terminal_api",
            status="failed",
            error_code="NODE_FAILURE",
        )
        gateway = _MockGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        with _client(store, gateway=gateway, retry_service=service) as client:
            response = client.post("/api/v1/runs/run_retry_terminal_api/retry", headers=_operator_headers())

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RETRY_NOT_FOUND"
        assert response.json()["error"]["details"]["run_id"] == "run_retry_terminal_api"
        assert gateway.submissions == []
        assert _hydro_status(store, "run_retry_terminal_api") == "published"
        assert [job.job_id for job in store.query_jobs_by_run("run_retry_terminal_api")] == [
            "job_retry_terminal_api"
        ]
        assert _events(store) == []


def test_display_retry_manual_action_does_not_mutate_pipeline_or_terminal_state(monkeypatch: Any) -> None:
    monkeypatch.setenv("ALLOW_DEV_ROLE_HEADER", "true")
    with _store() as store:
        _insert_hydro_run(store, "run_display_retry_state", status="failed")
        _insert_forecast_cycle(store, "cycle_display_retry_state", status="failed_run")
        _create_job(
            store,
            job_id="job_display_retry_failed",
            run_id="run_display_retry_state",
            cycle_id="cycle_display_retry_state",
            status="failed",
            error_code="NODE_FAILURE",
        )
        gateway = _MockGateway()
        app = create_app(_display_env())
        app.dependency_overrides[pipeline_routes.get_pipeline_store] = lambda: store
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = lambda: gateway

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/runs/run_display_retry_state/retry",
                headers=_operator_headers(),
            )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONTROL_PLANE_MANUAL_ACTION_REQUIRED"
        assert gateway.submissions == []
        assert gateway.cancelled == []
        assert [job.job_id for job in store.query_jobs_by_run("run_display_retry_state")] == [
            "job_display_retry_failed"
        ]
        assert store.get_job("job_display_retry_failed").status == "failed"
        assert _hydro_status(store, "run_display_retry_state") == "failed"
        assert _cycle_status(store, "cycle_display_retry_state") == "failed_run"
        assert _events(store) == []


def test_manual_retry_uses_partial_failure_despite_later_success_when_hydro_run_failed() -> None:
    with _store() as store:
        _insert_hydro_run(store, "run_partial_then_publish", status="failed")
        base_time = _cycle_time()
        partial = _create_job(
            store,
            job_id="job_frequency_partial",
            run_id="run_partial_then_publish",
            status="partially_failed",
            error_code="NODE_FAILURE",
        )
        partial.stage = "frequency"
        partial.updated_at = base_time + timedelta(minutes=5)
        succeeded = _create_job(
            store,
            job_id="job_publish_success",
            run_id="run_partial_then_publish",
            status="succeeded",
            error_code=None,
        )
        succeeded.stage = "publish"
        succeeded.updated_at = base_time + timedelta(minutes=10)
        store.session.add_all([partial, succeeded])
        store.session.commit()
        gateway = _MockGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("run_partial_then_publish", gateway=gateway, trusted_internal=True)

        assert retry.status == "submitted"
        assert retry.stage == "frequency"
        assert retry.job_id == "run_partial_then_publish_retry_active"
        event = next(event for event in _events(store) if event.event_type == "retry")
        assert event.details["previous_job_id"] == "job_frequency_partial"
        assert gateway.submissions


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


def test_display_cancel_manual_action_does_not_mutate_pipeline_or_terminal_state(monkeypatch: Any) -> None:
    monkeypatch.setenv("ALLOW_DEV_ROLE_HEADER", "true")
    with _store() as store:
        _insert_hydro_run(store, "run_display_cancel_state", status="running")
        _insert_forecast_cycle(store, "cycle_display_cancel_state", status="forecast_running")
        _create_job(
            store,
            job_id="job_display_cancel_running",
            run_id="run_display_cancel_state",
            cycle_id="cycle_display_cancel_state",
            status="running",
            slurm_job_id="slurm_display_cancel",
        )
        gateway = _MockGateway()
        app = create_app(_display_env())
        app.dependency_overrides[pipeline_routes.get_pipeline_store] = lambda: store
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = lambda: gateway

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/runs/run_display_cancel_state/cancel",
                headers=_operator_headers(),
            )

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONTROL_PLANE_MANUAL_ACTION_REQUIRED"
        assert gateway.submissions == []
        assert gateway.cancelled == []
        assert [job.job_id for job in store.query_jobs_by_run("run_display_cancel_state")] == [
            "job_display_cancel_running"
        ]
        assert store.get_job("job_display_cancel_running").status == "running"
        assert _hydro_status(store, "run_display_cancel_state") == "running"
        assert _cycle_status(store, "cycle_display_cancel_state") == "forecast_running"
        assert _events(store) == []


def test_retry_nonexistent_run_raises_not_found_without_enum_write() -> None:
    with _store() as store:
        gateway = _MockGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryNotFoundError):
            service.attempt_manual_retry("run_missing", gateway=gateway, trusted_internal=True)

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
    def __init__(
        self,
        failures: dict[str, SlurmGatewayError] | None = None,
        responses: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.cancelled: list[str] = []
        self.submissions: list[Any] = []
        self.failures = failures or {}
        self.responses = responses or {}

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
        if job_id in self.responses:
            return self.responses[job_id]
        return {"job_id": job_id, "status": "cancelled"}


def _display_env() -> dict[str, str]:
    return {
        "NHMS_REQUIRE_SERVICE_ROLE": "true",
        "NHMS_SERVICE_ROLE": "display_readonly",
    }


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
        self.previous_allow_dev_role_header: str | None = None

    def __enter__(self) -> TestClient:
        app.dependency_overrides[pipeline_routes.get_pipeline_store] = lambda: self.store
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = lambda: self.gateway
        if self.retry_service is not None:
            app.dependency_overrides[pipeline_routes.get_retry_service] = lambda: self.retry_service
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
