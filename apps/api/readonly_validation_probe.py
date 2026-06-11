from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi.testclient import TestClient

from apps.api import main as api_main
from apps.api.routes import pipeline as pipeline_routes
from packages.common.redaction import redact_text
from services.production_closure.readonly_db_validation import (
    STATUS_FAIL,
    STATUS_PASS,
    RouteHttpResponse,
    RouteRequester,
    _bounded_database_url,
    _display_app_env,
    _display_validation_env,
    _operator_headers,
    _response_body,
    _temporary_env,
)


@contextmanager
def display_route_requester(database_url: str) -> Iterator[RouteRequester]:
    with _temporary_env(_display_validation_env(database_url=database_url)):
        app = api_main.create_app(_display_app_env())
        with TestClient(app) as client:

            def requester(method: str, path: str) -> RouteHttpResponse:
                response = client.request(method, path, headers=_operator_headers())
                return RouteHttpResponse(
                    status_code=response.status_code,
                    body=_response_body(response),
                    text=response.text,
                )

            yield requester


def run_manual_action_probes(run_id: str, *, database_url: str | None = None) -> list[dict[str, Any]]:
    def forbidden_dependency() -> None:
        raise AssertionError("display_readonly manual action probe reached a write or gateway dependency")

    results: list[dict[str, Any]] = []
    validation_env = _display_validation_env()
    if database_url:
        validation_env["DATABASE_URL"] = _bounded_database_url(database_url)
    with _temporary_env(validation_env):
        app = api_main.create_app(_display_app_env())
        app.dependency_overrides[pipeline_routes.get_pipeline_store] = forbidden_dependency
        app.dependency_overrides[pipeline_routes.get_retry_service] = forbidden_dependency
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = forbidden_dependency
        with TestClient(app) as client:
            for action in ("retry", "cancel"):
                path = f"/api/v1/runs/{run_id}/{action}"
                try:
                    response = client.post(path, headers=_operator_headers())
                    body = _response_body(response)
                    error = body.get("error") if isinstance(body, dict) else {}
                    passed = response.status_code == 409 and error.get("code") == "CONTROL_PLANE_MANUAL_ACTION_REQUIRED"
                    results.append(
                        {
                            "name": f"display_{action}_manual_action",
                            "method": "POST",
                            "path": path,
                            "status": STATUS_PASS if passed else STATUS_FAIL,
                            "http_status": response.status_code,
                            "expected_error_code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
                            "observed_error_code": error.get("code"),
                            "write_dependency_constructed": False,
                            "write_executed": False,
                            "database_url_configured": bool(database_url),
                        }
                    )
                except AssertionError as error:
                    results.append(
                        {
                            "name": f"display_{action}_manual_action",
                            "method": "POST",
                            "path": path,
                            "status": STATUS_FAIL,
                            "expected_error_code": "CONTROL_PLANE_MANUAL_ACTION_REQUIRED",
                            "write_dependency_constructed": True,
                            "database_url_configured": bool(database_url),
                            "reason": redact_text(str(error)),
                        }
                    )
    return results
