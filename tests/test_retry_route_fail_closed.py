"""``POST /api/v1/runs/{run_id}/retry`` is a fail-closed manual-action endpoint (#3157).

openspec change ``retire-manual-retry-submit-lanes``: the route keeps its path,
operation and dependency order (400 ``INVALID_RUN_ID`` -> RBAC 401/403 -> the
display_readonly 409) and then ALWAYS answers 409
``CONTROL_PLANE_MANUAL_ACTION_REQUIRED``, pointing the operator at the node-22
marker lane (``FileJournalRetryService.record_manual_repair`` driven by
``scripts/node22_manual_retry_failed_runs.py``).  It constructs no DB session,
retry service or Slurm gateway, so a compute_control API without
``DATABASE_URL`` answers that 409 instead of the old 500.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.main import create_app
from apps.api.routes import pipeline as pipeline_routes
from services.orchestrator import retry as retry_module
from services.orchestrator.file_orchestration_journal import FileJournalRetryService
from services.orchestrator.persistence import Base, PipelineEvent, PipelineJob, PipelineStore
from tests.pipeline_job_orm_helpers import seed_pipeline_job

_RETRY_PATH = "/api/v1/runs/{run_id}/retry"
_RUN_ID = "run_retry_retired"
_FAILED_JOB_ID = "job_retry_retired_failed"
_MANUAL_ACTION_CODE = "CONTROL_PLANE_MANUAL_ACTION_REQUIRED"
_RECOVERY_RUNBOOK = "node22-control-plane-manual-recovery"
_CONTROL_ROLES = ("compute_control", "dev_monolith")


class _SpyGateway:
    """System-boundary stand-in for the Slurm gateway: records every call."""

    def __init__(self) -> None:
        self.submissions: list[Any] = []
        self.cancelled: list[str] = []

    def submit_job(self, request: Any) -> dict[str, Any]:
        self.submissions.append(request)
        return {
            "job_id": "slurm_retry_spy",
            "run_id": request.run_id,
            "model_id": request.model_id,
            "status": "submitted",
            "submitted_at": "2026-05-15T00:00:00Z",
            "updated_at": "2026-05-15T00:00:00Z",
        }

    def cancel_job(self, job_id: str) -> dict[str, str]:
        self.cancelled.append(job_id)
        return {"job_id": job_id, "status": "cancelled"}


def _role_env(role: str) -> dict[str, str]:
    env = {"NHMS_REQUIRE_SERVICE_ROLE": "true", "NHMS_SERVICE_ROLE": role}
    if role == "display_readonly":
        env["OBJECT_STORE_ROOT"] = tempfile.mkdtemp(prefix="nhms-retry-fail-closed-object-store-")
    return env


@pytest.fixture
def seeded_store() -> Iterator[PipelineStore]:
    """A run the retired lane WOULD have retried: one failed transient job, failed hydro run."""

    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_schemas(dbapi_connection: Any, _connection_record: Any) -> None:
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
                "INSERT INTO hydro.hydro_run (run_id, source_id, model_id, status, error_code) "
                "VALUES (:run_id, 'gfs', 'model_a', 'failed', 'NODE_FAILURE')"
            ),
            {"run_id": _RUN_ID},
        )
    session = Session(engine)
    store = PipelineStore(session)
    job = seed_pipeline_job(
        store.session,
        job_id=_FAILED_JOB_ID,
        run_id=_RUN_ID,
        cycle_id="cycle_retry_retired",
        job_type="run_shud_analysis",
        slurm_job_id="slurm_retry_retired",
        model_id="model_a",
        stage="run",
        status="failed",
        commit=False,
    )
    job.error_code = "NODE_FAILURE"
    job.error_message = "NODE_FAILURE failed"
    session.add(job)
    session.commit()
    try:
        yield store
    finally:
        session.close()


def _client_for(
    role: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    overrides: dict[Callable[..., Any], Callable[..., Any]] | None = None,
) -> TestClient:
    monkeypatch.setenv("ALLOW_DEV_ROLE_HEADER", "true")
    app = create_app(_role_env(role))
    for dependency, replacement in (overrides or {}).items():
        app.dependency_overrides[dependency] = replacement
    return TestClient(app)


def _durable_snapshot(store: PipelineStore) -> dict[str, Any]:
    store.session.expire_all()
    jobs = [
        (job.job_id, job.status, job.retry_count, job.slurm_job_id)
        for job in store.session.scalars(select(PipelineJob).order_by(PipelineJob.job_id))
    ]
    events = list(store.session.scalars(select(PipelineEvent)))
    hydro_status = store.session.execute(
        text("SELECT status FROM hydro.hydro_run WHERE run_id = :run_id"), {"run_id": _RUN_ID}
    ).scalar_one()
    return {"jobs": jobs, "event_count": len(events), "hydro_status": hydro_status}


def _assert_retired_manual_action(response: Any) -> dict[str, Any]:
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == _MANUAL_ACTION_CODE
    details = body["error"]["details"]
    assert details["run_id"] == _RUN_ID
    assert details["recovery_runbook"] == _RECOVERY_RUNBOOK
    assert "scripts/node22_manual_retry_failed_runs.py" in details["suggested_action"]
    assert "record_manual_repair" in details["suggested_action"]
    # The retired 409 is not the display 409: no display_mode, and nothing that
    # claims a job was submitted.
    assert "display_mode" not in details
    assert "job_id" not in details
    assert "slurm_job_id" not in details
    assert "data" not in body
    return body


@pytest.mark.parametrize("role", _CONTROL_ROLES)
def test_authorized_control_retry_answers_manual_action_and_writes_nothing(
    role: str,
    seeded_store: PipelineStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gateway = _SpyGateway()
    before = _durable_snapshot(seeded_store)
    for user_role in ("operator", "model_admin", "sys_admin"):
        with _client_for(
            role,
            monkeypatch,
            overrides={
                pipeline_routes.get_pipeline_store: lambda: seeded_store,
                pipeline_routes.get_slurm_gateway: lambda: gateway,
            },
        ) as client:
            response = client.post(_RETRY_PATH.format(run_id=_RUN_ID), headers={"X-User-Role": user_role})

        _assert_retired_manual_action(response)

    assert gateway.submissions == []
    assert gateway.cancelled == []
    assert _durable_snapshot(seeded_store) == before
    assert before == {
        "jobs": [(_FAILED_JOB_ID, "failed", 0, "slurm_retry_retired")],
        "event_count": 0,
        "hydro_status": "failed",
    }


def test_compute_control_retry_without_database_url_is_manual_action_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with _client_for("compute_control", monkeypatch) as client:
        response = client.post(_RETRY_PATH.format(run_id=_RUN_ID), headers={"X-User-Role": "operator"})

    body = _assert_retired_manual_action(response)
    assert "DATABASE_URL" not in json.dumps(body)


@pytest.mark.parametrize("role", _CONTROL_ROLES)
def test_retry_auth_and_rbac_precede_the_manual_action_error(
    role: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with _client_for(role, monkeypatch) as client:
        unauthenticated = client.post(_RETRY_PATH.format(run_id=_RUN_ID))
        unauthorized = client.post(_RETRY_PATH.format(run_id=_RUN_ID), headers={"X-User-Role": "viewer"})
        invalid = client.post(_RETRY_PATH.format(run_id="-bad"), headers={"X-User-Role": "operator"})

    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["code"] == "AUTH_REQUIRED"
    assert unauthorized.status_code == 403
    assert unauthorized.json()["error"]["code"] == "RBAC_FORBIDDEN"
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_RUN_ID"
    for response in (unauthenticated, unauthorized, invalid):
        body_text = response.text
        assert _MANUAL_ACTION_CODE not in body_text
        assert "suggested_action" not in body_text
        assert "recovery_runbook" not in body_text


def test_display_readonly_retry_keeps_its_display_409_first(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> None:
        raise AssertionError("display retry must not construct a write or gateway dependency")

    with _client_for(
        "display_readonly",
        monkeypatch,
        overrides={
            pipeline_routes.get_pipeline_store: forbidden,
            pipeline_routes.get_slurm_gateway: forbidden,
        },
    ) as client:
        response = client.post(_RETRY_PATH.format(run_id=_RUN_ID), headers={"X-User-Role": "operator"})

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == _MANUAL_ACTION_CODE
    assert error["details"]["display_mode"] == "display_readonly"
    assert error["details"]["run_id"] == _RUN_ID
    assert error["details"]["recovery_runbook"] == _RECOVERY_RUNBOOK


def _dependency_calls(dependant: Dependant) -> set[Callable[..., Any]]:
    calls: set[Callable[..., Any]] = set()
    for sub in dependant.dependencies:
        if sub.call is not None:
            calls.add(sub.call)
        calls |= _dependency_calls(sub)
    return calls


def test_retry_route_dependency_graph_constructs_no_store_service_or_gateway() -> None:
    app = create_app(_role_env("compute_control"))
    route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == _RETRY_PATH and "POST" in route.methods
    )
    calls = _dependency_calls(route.dependant)

    assert pipeline_routes.require_retry_control_action in calls
    assert pipeline_routes.require_retry_action in calls
    assert pipeline_routes._validated_run_id in calls
    assert pipeline_routes.get_pipeline_store not in calls
    # R4 (#3261): the SQL retry-service provider no longer exists at all.
    assert not hasattr(pipeline_routes, "get_retry_service")
    assert pipeline_routes.get_slurm_gateway not in calls


def test_retry_route_body_fails_closed_even_if_the_retired_dependency_is_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with _client_for(
        "compute_control",
        monkeypatch,
        overrides={pipeline_routes.require_retry_lane_retired: lambda: None},
    ) as client:
        response = client.post(_RETRY_PATH.format(run_id=_RUN_ID), headers={"X-User-Role": "operator"})

    _assert_retired_manual_action(response)


def test_manual_retry_submit_methods_are_retired_on_both_lanes() -> None:
    # R4 (#3261): the SQL lane's retry service class is gone entirely.
    assert not hasattr(retry_module, "RetryService")
    assert not hasattr(FileJournalRetryService, "attempt_manual_retry")
    # The surviving marker lane is untouched.
    assert callable(FileJournalRetryService.record_manual_repair)
