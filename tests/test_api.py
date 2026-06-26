import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Scope

from apps.api.main import app
from apps.api.routes.models import get_model_registry_store


@pytest.mark.asyncio
async def test_health():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "nhms-api"
    assert data["version"] == "0.1.0"


def test_api_errors_import_does_not_construct_slurm_gateway(monkeypatch):
    monkeypatch.setenv("SLURM_GATEWAY_BACKEND", "invalid")
    import apps.api.errors as errors

    assert errors.ApiError(
        status_code=400,
        code="BAD_REQUEST",
        message="bad request",
    ).code == "BAD_REQUEST"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "method",
        "path",
        "json_body",
        "headers",
        "expected_status",
        "expected_code",
        "expected_action",
        "expected_decision",
        "expected_target",
    ),
    [
        (
            "POST",
            "/api/v1/models",
            {
                "model_id": "candidate_model",
                "basin_version_id": "basin_v01",
                "river_network_version_id": "basin_rivnet_v01",
                "mesh_version_id": "basin_mesh_v01",
                "calibration_version_id": "basin_cal_v01",
                "shud_code_version": "2.0",
                "model_package_uri": "s3://nhms/models/candidate_model/package/",
            },
            {},
            401,
            "AUTH_REQUIRED",
            "models.switch_version",
            "deny",
            {"type": "model_registry", "id": "models"},
        ),
        (
            "PUT",
            "/api/v1/models/inactive_model/active",
            {"active": True},
            {"X-User-Role": "viewer"},
            403,
            "RBAC_FORBIDDEN",
            "models.activate",
            "deny",
            {"type": "model_instance", "id": "inactive_model"},
        ),
    ],
)
async def test_protected_mutation_pre_body_auth_denial_preserves_error_shape_and_store_boundary(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    json_body: dict[str, Any],
    headers: dict[str, str],
    expected_status: int,
    expected_code: str,
    expected_action: str,
    expected_decision: str,
    expected_target: dict[str, str],
) -> None:
    _force_dev_header_auth(monkeypatch)
    request_id = f"req-api-pre-body-{expected_code.lower()}"
    store = _RecordingModelStore()

    response = await _request_with_model_store(
        method,
        path,
        store,
        headers={**headers, "X-Request-ID": request_id},
        json=json_body,
    )

    assert response.status_code == expected_status
    _assert_pre_body_policy_error(
        response,
        request_id=request_id,
        code=expected_code,
        action_id=expected_action,
        decision=expected_decision,
        target=expected_target,
    )
    assert store.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "json_body", "expected_field"),
    [
        ("PUT", "/api/v1/models/inactive_model/active", {"active": "yes"}, "body.active"),
        ("POST", "/api/v1/models/inactive_model/lifecycle", {"operation": "explode"}, "body.operation"),
    ],
)
async def test_protected_mutation_pre_body_validation_rejects_before_store_call(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    json_body: dict[str, Any],
    expected_field: str,
) -> None:
    _force_dev_header_auth(monkeypatch)
    request_id = f"req-api-pre-body-validation-{expected_field.replace('.', '-')}"
    store = _RecordingModelStore()

    response = await _request_with_model_store(
        method,
        path,
        store,
        headers={"X-Request-ID": request_id, "X-User-Role": "model_admin"},
        json=json_body,
    )

    assert response.status_code == 422
    _assert_pre_body_validation_error(response, request_id=request_id, expected_field=expected_field)
    assert store.calls == []


@pytest.mark.asyncio
async def test_protected_mutation_active_toggle_oversize_without_content_length_stops_before_store_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_dev_header_auth(monkeypatch)
    request_id = "req-api-active-toggle-read-cap"
    body = b'{"active": true, "padding":"' + (b"x" * 200_000) + b'"}'
    chunks = [body[:8192], body[8192:]]
    reads = 0
    response_messages: list[Message] = []
    store = _RecordingModelStore()

    async def receive() -> Message:
        nonlocal reads
        if reads < len(chunks):
            chunk = chunks[reads]
            reads += 1
            return {"type": "http.request", "body": chunk, "more_body": reads < len(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        response_messages.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": "/api/v1/models/inactive_model/active",
        "raw_path": b"/api/v1/models/inactive_model/active",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"x-request-id", request_id.encode()),
            (b"x-user-role", b"model_admin"),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }

    app.dependency_overrides[get_model_registry_store] = lambda: store
    try:
        await app(scope, receive, send)
    finally:
        app.dependency_overrides.pop(get_model_registry_store, None)

    start = next(message for message in response_messages if message["type"] == "http.response.start")
    body_bytes = b"".join(
        message.get("body", b"") for message in response_messages if message["type"] == "http.response.body"
    )
    response_headers = dict(start["headers"])
    payload = json.loads(body_bytes)
    assert start["status"] == 422
    assert response_headers[b"x-request-id"] == request_id.encode()
    assert payload["request_id"] == request_id
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "VALIDATION_ERROR"
    assert payload["error"]["details"][0]["field"] == "body.active"
    assert payload["error"]["details"][0]["request_id"] == request_id
    assert reads == 1
    assert store.calls == []


async def _request_with_model_store(
    method: str,
    path: str,
    store: "_RecordingModelStore",
    **kwargs: Any,
) -> Any:
    app.dependency_overrides[get_model_registry_store] = lambda: store
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)
    finally:
        app.dependency_overrides.pop(get_model_registry_store, None)


def _force_dev_header_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTH_BACKEND", raising=False)
    monkeypatch.delenv("NHMS_AUTH_MODE", raising=False)
    monkeypatch.delenv("NHMS_INTERNAL_LIVE_PROOF_TOKEN", raising=False)
    monkeypatch.delenv("NHMS_TRUSTED_LIVE_PROOF_MODE", raising=False)
    monkeypatch.setenv("ALLOW_DEV_ROLE_HEADER", "true")


def _assert_pre_body_policy_error(
    response: Any,
    *,
    request_id: str,
    code: str,
    action_id: str,
    decision: str,
    target: dict[str, str],
) -> None:
    body = response.json()
    assert response.headers["X-Request-ID"] == request_id
    assert body["request_id"] == request_id
    assert body["status"] == "error"
    assert body["error"]["code"] == code
    details = body["error"]["details"]
    assert set(details) == {"policy_decision", "audit_record"}
    policy_decision = details["policy_decision"]
    audit = details["audit_record"]
    assert policy_decision["action_id"] == action_id
    assert policy_decision["decision"] == decision
    assert policy_decision["target_type"] == target["type"]
    assert policy_decision["target_id"] == target["id"]
    assert policy_decision["no_mutation_expected"] is True
    assert audit["request_id"] == request_id
    assert audit["action_id"] == action_id
    assert audit["decision"] == decision
    assert audit["target"] == target


def _assert_pre_body_validation_error(response: Any, *, request_id: str, expected_field: str) -> None:
    body = response.json()
    assert response.headers["X-Request-ID"] == request_id
    assert body["request_id"] == request_id
    assert body["status"] == "error"
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "Request validation failed."
    assert body["error"]["details"][0]["field"] == expected_field
    assert body["error"]["details"][0]["rejected_value"] is None
    assert body["error"]["details"][0]["request_id"] == request_id


class _RecordingModelStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def _record_call(*_args: Any, **_kwargs: Any) -> None:
            self.calls.append(name)
            raise AssertionError(f"model store should not be called on protected mutation pre-body path: {name}")

        return _record_call
