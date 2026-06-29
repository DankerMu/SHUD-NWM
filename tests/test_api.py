import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.types import Message, Scope

from apps.api.main import _ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES, app
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
        ("PUT", "/api/v1/models/inactive_model/active", {"active_flag": "yes"}, "body.active"),
        ("PUT", "/api/v1/models/inactive_model/active", {}, "body.active"),
        ("POST", "/api/v1/models/inactive_model/lifecycle", {"operation": "explode"}, "body.operation"),
        ("POST", "/api/v1/models/inactive_model/lifecycle", {}, "body.operation"),
        ("POST", "/api/v1/models/inactive_model/preflight", {}, "body.operation"),
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
@pytest.mark.parametrize(
    ("method", "path", "body", "expected_field"),
    [
        ("PUT", "/api/v1/models/inactive_model/active", b'{"active": ', "body.active"),
        ("POST", "/api/v1/models/inactive_model/lifecycle", b'{"operation": ', "body.operation"),
        ("POST", "/api/v1/models/inactive_model/preflight", b'{"operation": ', "body.operation"),
    ],
)
async def test_protected_mutation_pre_body_malformed_json_rejects_before_store_call(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: bytes,
    expected_field: str,
) -> None:
    _force_dev_header_auth(monkeypatch)
    request_id = f"req-api-pre-body-malformed-{expected_field.replace('.', '-')}"
    store = _RecordingModelStore()

    response = await _request_with_model_store(
        method,
        path,
        store,
        headers={
            "X-Request-ID": request_id,
            "X-User-Role": "model_admin",
            "Content-Type": "application/json",
        },
        content=body,
    )

    assert response.status_code == 422
    _assert_pre_body_validation_error(response, request_id=request_id, expected_field=expected_field)
    assert store.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body", "expected_field"),
    [
        (
            "PUT",
            "/api/v1/models/inactive_model/active",
            b'{"active": true, "padding":"' + (b"x" * _ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES) + b'"}',
            "body.active",
        ),
        (
            "POST",
            "/api/v1/models/inactive_model/lifecycle",
            b'{"operation": "activate", "padding":"' + (b"x" * _ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES) + b'"}',
            "body.operation",
        ),
        (
            "POST",
            "/api/v1/models/inactive_model/preflight",
            b'{"operation": "activate", "padding":"' + (b"x" * _ACTIVE_TOGGLE_PRE_BODY_MAX_BYTES) + b'"}',
            "body.operation",
        ),
    ],
    ids=["active-content-length", "lifecycle-content-length", "preflight-content-length"],
)
async def test_protected_mutation_oversized_body_with_content_length_stops_before_receive_and_store_call(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: bytes,
    expected_field: str,
) -> None:
    _force_dev_header_auth(monkeypatch)
    request_id = f"req-api-pre-body-content-length-{expected_field.replace('.', '-')}"
    store = _RecordingModelStore()

    status, headers, payload, reads = await _asgi_request_with_model_store(
        method,
        path,
        store,
        headers={
            "X-Request-ID": request_id,
            "X-User-Role": "model_admin",
            "Content-Type": "application/json",
            "Content-Length": str(len(body)),
        },
        body_chunks=[body],
    )

    assert status == 422
    _assert_pre_body_validation_payload(headers, payload, request_id=request_id, expected_field=expected_field)
    assert reads == 0
    assert store.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body", "expected_field"),
    [
        (
            "PUT",
            "/api/v1/models/inactive_model/active",
            b'{"active": true, "padding":"' + (b"x" * 200_000) + b'"}',
            "body.active",
        ),
        (
            "POST",
            "/api/v1/models/inactive_model/lifecycle",
            b'{"operation": "activate", "padding":"' + (b"x" * 200_000) + b'"}',
            "body.operation",
        ),
        (
            "POST",
            "/api/v1/models/inactive_model/preflight",
            b'{"operation": "activate", "padding":"' + (b"x" * 200_000) + b'"}',
            "body.operation",
        ),
    ],
    ids=["active-stream", "lifecycle-stream", "preflight-stream"],
)
async def test_protected_mutation_oversized_stream_without_content_length_stops_before_store_call(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    body: bytes,
    expected_field: str,
) -> None:
    _force_dev_header_auth(monkeypatch)
    request_id = f"req-api-pre-body-read-cap-{expected_field.replace('.', '-')}"
    store = _RecordingModelStore()

    status, headers, payload, reads = await _asgi_request_with_model_store(
        method,
        path,
        store,
        headers={
            "X-Request-ID": request_id,
            "X-User-Role": "model_admin",
            "Content-Type": "application/json",
        },
        body_chunks=[body[:8192], body[8192:]],
    )

    assert status == 422
    _assert_pre_body_validation_payload(headers, payload, request_id=request_id, expected_field=expected_field)
    assert reads == 1
    assert store.calls == []


@pytest.mark.asyncio
async def test_protected_mutation_pre_body_release_blocked_stops_before_store_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLOW_DEV_ROLE_HEADER", raising=False)
    monkeypatch.setenv("AUTH_BACKEND", "oidc")
    monkeypatch.setenv("NHMS_TRUSTED_LIVE_PROOF_MODE", "test_internal")
    monkeypatch.setenv("NHMS_INTERNAL_LIVE_PROOF_TOKEN", "proof-token")
    request_id = "req-api-pre-body-release-blocked"
    store = _RecordingModelStore()

    response = await _request_with_model_store(
        "PUT",
        "/api/v1/models/inactive_model/active",
        store,
        headers={"X-Request-ID": request_id, "X-User-Role": "model_admin"},
        json={"active": True},
    )

    assert response.status_code == 503
    _assert_pre_body_policy_error(
        response,
        request_id=request_id,
        code="RELEASE_BLOCKED",
        action_id="models.activate",
        decision="release_blocked",
        target={"type": "model_instance", "id": "inactive_model"},
        extra_detail_keys={"removal_criteria"},
    )
    details = response.json()["error"]["details"]
    assert details["policy_decision"]["execution_mode"] == "release_blocked"
    assert details["removal_criteria"] == "Configure and prove live backend identity-provider role mapping."
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


async def _asgi_request_with_model_store(
    method: str,
    path: str,
    store: "_RecordingModelStore",
    *,
    headers: dict[str, str],
    body_chunks: list[bytes],
) -> tuple[int, dict[bytes, bytes], dict[str, Any], int]:
    reads = 0
    response_messages: list[Message] = []

    async def receive() -> Message:
        nonlocal reads
        if reads < len(body_chunks):
            chunk = body_chunks[reads]
            reads += 1
            return {"type": "http.request", "body": chunk, "more_body": reads < len(body_chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        response_messages.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")]
        + [(key.lower().encode(), value.encode()) for key, value in headers.items()],
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
    return start["status"], dict(start["headers"]), json.loads(body_bytes), reads


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
    extra_detail_keys: set[str] | None = None,
) -> None:
    body = response.json()
    assert response.headers["X-Request-ID"] == request_id
    assert body["request_id"] == request_id
    assert body["status"] == "error"
    assert body["error"]["code"] == code
    details = body["error"]["details"]
    assert set(details) == {"policy_decision", "audit_record", *(extra_detail_keys or set())}
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
    _assert_pre_body_validation_payload(
        response.headers,
        response.json(),
        request_id=request_id,
        expected_field=expected_field,
    )


def _assert_pre_body_validation_payload(
    headers: Any,
    body: dict[str, Any],
    *,
    request_id: str,
    expected_field: str,
) -> None:
    assert _header_request_id(headers) == request_id
    assert body["request_id"] == request_id
    assert body["status"] == "error"
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["message"] == "Request validation failed."
    assert body["error"]["details"][0]["field"] == expected_field
    assert body["error"]["details"][0]["rejected_value"] is None
    assert body["error"]["details"][0]["request_id"] == request_id


def _header_request_id(headers: Any) -> str:
    if isinstance(headers, dict) and b"x-request-id" in headers:
        return headers[b"x-request-id"].decode()
    return headers["X-Request-ID"]


class _RecordingModelStore:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        def _record_call(*_args: Any, **_kwargs: Any) -> None:
            self.calls.append(name)
            raise AssertionError(f"model store should not be called on protected mutation pre-body path: {name}")

        return _record_call
