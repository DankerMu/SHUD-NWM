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
from tests.pipeline_job_orm_helpers import seed_pipeline_job

HYDRO_RUN_STATUS_ENUM = {
    "created",
    "staged",
    "pending",
    "submitted",
    "running",
    "succeeded",
    "parsed",
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


def test_display_retry_manual_action_does_not_mutate_pipeline_or_terminal_state(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ALLOW_DEV_ROLE_HEADER", "true")
    request_id = "req-display-retry-manual-action"
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
        gateway_probe = _GatewayDependencyProbe()
        app = create_app(_display_env(tmp_path / "display-object-store"))
        app.dependency_overrides[pipeline_routes.get_pipeline_store] = lambda: store
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = gateway_probe

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/runs/run_display_retry_state/retry",
                headers={**_operator_headers(), "X-Request-ID": request_id},
            )

        assert response.status_code == 409
        _assert_display_manual_action_error(
            response,
            request_id=request_id,
            run_id="run_display_retry_state",
            control_action="retry",
        )
        assert gateway_probe.calls == 0
        assert [job.job_id for job in store.query_jobs_by_run("run_display_retry_state")] == [
            "job_display_retry_failed"
        ]
        assert store.get_job("job_display_retry_failed").status == "failed"
        assert _hydro_status(store, "run_display_retry_state") == "failed"
        assert _cycle_status(store, "cycle_display_retry_state") == "failed_run"
        assert _events(store) == []


@pytest.mark.parametrize(
    ("role_header", "expected_status", "expected_code"),
    [
        (None, 401, "AUTH_REQUIRED"),
        ({"X-User-Role": "viewer"}, 403, "RBAC_FORBIDDEN"),
    ],
)
def test_retry_auth_rbac_denied_paths_make_no_gateway_or_state_writes(
    role_header: dict[str, str] | None,
    expected_status: int,
    expected_code: str,
) -> None:
    request_id = f"req-retry-authz-{expected_code.lower()}"
    with _store() as store:
        _insert_hydro_run(store, "run_retry_authz", status="failed")
        _insert_forecast_cycle(store, "cycle_retry_authz", status="failed_run")
        _create_job(
            store,
            job_id="job_retry_authz",
            run_id="run_retry_authz",
            cycle_id="cycle_retry_authz",
            status="failed",
            error_code="NODE_FAILURE",
        )
        gateway = _MockGateway()
        with _client(store, gateway=gateway) as client:
            response = client.post(
                "/api/v1/runs/run_retry_authz/retry",
                headers={**(role_header or {}), "X-Request-ID": request_id},
            )

        assert response.status_code == expected_status
        _assert_policy_error_response(
            response,
            request_id=request_id,
            expected_code=expected_code,
            expected_action="pipeline.retry_run",
            target_id="run_retry_authz",
        )
        assert gateway.submissions == []
        assert gateway.cancelled == []
        assert store.get_job("job_retry_authz").status == "failed"
        assert _hydro_status(store, "run_retry_authz") == "failed"
        assert _cycle_status(store, "cycle_retry_authz") == "failed_run"
        assert _events(store) == []


def test_enum_sets_match_migration() -> None:
    assert HYDRO_RUN_STATUS_ENUM <= _migration_enum_values("hydro.run_status")
    # #2048: 000062 re-adds `frequency_done` to the DB enum as a convergence-only
    # label. It is not a status the retry/cancel lanes may write or accept.
    assert "frequency_done" not in HYDRO_RUN_STATUS_ENUM
    assert _migration_enum_values("met.cycle_status") == MET_CYCLE_STATUS_ENUM


class _ClosingStore(PipelineStore):
    def __enter__(self) -> PipelineStore:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.session.close()


class _MockGateway:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.submissions: list[Any] = []

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
        self.cancelled.append(job_id)
        return {"job_id": job_id, "status": "cancelled"}


class _GatewayDependencyProbe:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> _MockGateway:
        self.calls += 1
        raise AssertionError("display manual-action path must not resolve the Slurm gateway dependency")


def _display_env(object_store_root: Path) -> dict[str, str]:
    object_store_root.mkdir(parents=True, exist_ok=True)
    return {
        "NHMS_REQUIRE_SERVICE_ROLE": "true",
        "NHMS_SERVICE_ROLE": "display_readonly",
        "OBJECT_STORE_ROOT": str(object_store_root),
    }


class _client:
    def __init__(
        self,
        store: PipelineStore,
        *,
        gateway: _MockGateway | None = None,
    ) -> None:
        self.store = store
        self.gateway = gateway or _MockGateway()
        self.client: TestClient | None = None
        self.previous_allow_dev_role_header: str | None = None

    def __enter__(self) -> TestClient:
        app.dependency_overrides[pipeline_routes.get_pipeline_store] = lambda: self.store
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = lambda: self.gateway
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
    job_type: str = "run_shud_analysis",
    stage: str = "run",
) -> PipelineJob:
    job = seed_pipeline_job(
        store.session,
        job_id=job_id,
        run_id=run_id,
        cycle_id=cycle_id,
        job_type=job_type,
        slurm_job_id=slurm_job_id,
        model_id="model_a",
        stage=stage,
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


def _events(store: PipelineStore) -> list[PipelineEvent]:
    statement = select(PipelineEvent).order_by(PipelineEvent.event_id.asc())
    return list(store.session.scalars(statement))


def _assert_policy_error_response(
    response: Any,
    *,
    request_id: str,
    expected_code: str,
    expected_action: str,
    target_id: str,
) -> None:
    body = response.json()
    assert response.headers["X-Request-ID"] == request_id
    assert body["request_id"] == request_id
    assert body["status"] == "error"
    assert body["error"]["code"] == expected_code
    details = body["error"]["details"]
    assert set(details) == {"policy_decision", "audit_record"}
    policy_decision = details["policy_decision"]
    audit = details["audit_record"]
    assert policy_decision["action_id"] == expected_action
    assert policy_decision["target_type"] == "pipeline_run"
    assert policy_decision["target_id"] == target_id
    assert policy_decision["no_mutation_expected"] is True
    assert audit["request_id"] == request_id
    assert audit["action_id"] == expected_action
    assert audit["target"] == {"type": "pipeline_run", "id": target_id}
    assert audit["decision"] == policy_decision["decision"]


# #3157: retry points at the marker lane of the compute role.
_DISPLAY_SUGGESTED_ACTIONS = {
    "retry": (
        "Ask an operator of the compute role to record a manual repair for this run with "
        "scripts/node22_manual_retry_failed_runs.py (FileJournalRetryService.record_manual_repair); "
        "the scheduler mints the retry."
    ),
}


def _assert_display_manual_action_error(
    response: Any,
    *,
    request_id: str,
    run_id: str,
    control_action: str,
) -> None:
    body = response.json()
    assert response.headers["X-Request-ID"] == request_id
    assert body["request_id"] == request_id
    assert body["status"] == "error"
    assert body["error"]["code"] == "CONTROL_PLANE_MANUAL_ACTION_REQUIRED"
    assert body["error"]["message"] == (
        f"Control-plane {control_action} requires manual action in display_readonly mode."
    )
    assert body["error"]["details"] == {
        "run_id": run_id,
        "display_mode": "display_readonly",
        "suggested_action": _DISPLAY_SUGGESTED_ACTIONS[control_action],
        "recovery_runbook": "node22-control-plane-manual-recovery",
    }


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
