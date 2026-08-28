"""Focused auth matrix for the four registered Slurm gateway mutations.

Covers issue #1684: every registered Slurm mutation has exactly one canonical
policy decision before any gateway side effect; the scheduler service bearer
authenticates only Slurm mutations; disabled standalone reset stays 404;
release-blocked identities stay 503; and denied requests never construct or
call the gateway.

The dedicated suite runs against the standalone bounded app
(``services.slurm_gateway.app.create_gateway_app``) and the shared router
mounted in a bare FastAPI app, so both deployment arms are covered.

The client-side token contract and pre-acceptance disposition proofs live in
``tests/test_slurm_gateway_auth_client.py``; the deployment-side loopback
bind guard and display-forbidden parity live in
``tests/test_slurm_gateway_auth_deployment.py``. Both import the shared
helpers/constants from this module.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from packages.common.auth_policy import AUTH_REQUIRED, RBAC_FORBIDDEN, RELEASE_BLOCKED
from services.slurm_gateway.app import INTERNAL_RESET_PATH, create_gateway_app
from services.slurm_gateway.config import SlurmGatewaySettings, get_settings
from services.slurm_gateway.models import ResetRequest
from services.slurm_gateway.routes import create_slurm_router, slurm_gateway

SERVICE_TOKEN = "slurm-gateway-service-token-0123456789abcdef"
SECOND_TOKEN = "slurm-gateway-service-token-abcdef0123456789"
SERVICE_BEARER = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
WRONG_BEARER = {"Authorization": "Bearer wrong-token-value-abcdef"}
OPERATOR_IDENTITY = {"X-User-ID": "ops-user", "X-User-Role": "operator"}
SYSADMIN_IDENTITY = {"X-User-ID": "admin-user", "X-User-Role": "sys_admin"}
VIEWER_IDENTITY = {"X-User-ID": "viewer-user", "X-User-Role": "viewer"}
MODEL_ADMIN_IDENTITY = {"X-User-ID": "model-admin", "X-User-Role": "model_admin"}

SUBMIT_BODY = {"run_id": "run_001", "model_id": "model_001"}

ALL_MUTATION_CALLS = (
    ("POST", "/api/v1/slurm/jobs", SUBMIT_BODY),
    (
        "POST",
        "/api/v1/slurm/job-arrays",
        {
            "job_type": "run_shud_forecast_array",
            "cycle_id": "gfs_2026050100",
            "stage_name": "forecast",
            "tasks": [{"run_id": "run_0", "model_id": "model_001", "basin_version_id": "basin_0"}],
            "manifest": {"run_id": "run_0", "model_id": "model_001", "basin_version_id": "basin_0"},
        },
    ),
    ("DELETE", "/api/v1/slurm/jobs/mock_4040", None),
    ("POST", "/api/v1/slurm/internal/reset", None),
)


@pytest.fixture(autouse=True)
def reset_mock_gateway():
    slurm_gateway.reset(ResetRequest(restore_defaults=True))
    yield
    slurm_gateway.reset(ResetRequest(restore_defaults=True))


def _standalone_app(*, allow_internal_reset: bool = True) -> FastAPI:
    settings = SlurmGatewaySettings(backend="mock", allow_internal_reset=allow_internal_reset)
    app = create_gateway_app(settings)
    # The reset handler resolves settings through the cached get_settings()
    # dependency, so pin it to the same settings used to build the app.
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def _env(monkeypatch, **values: str | None) -> None:
    monkeypatch.delenv("SLURM_GATEWAY_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_DEV_ROLE_HEADER", raising=False)
    monkeypatch.delenv("NHMS_DEV_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("AUTH_BACKEND", raising=False)
    monkeypatch.delenv("NHMS_AUTH_MODE", raising=False)
    monkeypatch.delenv("NHMS_TRUSTED_LIVE_PROOF_MODE", raising=False)
    monkeypatch.delenv("NHMS_INTERNAL_LIVE_PROOF_TOKEN", raising=False)
    for key, value in values.items():
        if value is not None:
            monkeypatch.setenv(key, value)


def _client_for(
    monkeypatch,
    app: FastAPI,
    *,
    service_token: str | None = SERVICE_TOKEN,
    allow_dev_role_header: bool = True,
    additional_env: dict[str, str] | None = None,
) -> TestClient:
    env_values: dict[str, str | None] = {"SLURM_GATEWAY_SERVICE_TOKEN": service_token}
    if allow_dev_role_header:
        env_values["ALLOW_DEV_ROLE_HEADER"] = "true"
    env_values.update(additional_env or {})
    _env(monkeypatch, **env_values)
    return TestClient(app)


def _denied_call(client: TestClient, method: str, path: str, body: dict | None) -> tuple[int, str]:
    response = client.request(method, path, json=body)
    payload = response.json()
    return response.status_code, payload["error"]["code"]


# ---------------------------------------------------------------------------
# 4.1 - missing/wrong bearer -> 401 AUTH_REQUIRED with zero side effect
# ---------------------------------------------------------------------------


def test_submit_without_credential_401_and_no_gateway(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app, service_token=None)
    status, code = _denied_call(client, "POST", "/api/v1/slurm/jobs", SUBMIT_BODY)
    assert status == 401
    assert code == AUTH_REQUIRED
    # No gateway side effect: job list cannot contain a submitted job.
    assert client.get("/api/v1/slurm/jobs").json() == []


def test_submit_wrong_bearer_401_and_no_gateway(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    response = client.post("/api/v1/slurm/jobs", json=SUBMIT_BODY, headers=WRONG_BEARER)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == AUTH_REQUIRED
    assert client.get("/api/v1/slurm/jobs").json() == []


def test_array_submit_without_credential_401_and_no_gateway(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app, service_token=None)
    response = client.post(
        "/api/v1/slurm/job-arrays",
        json={
            "job_type": "run_shud_forecast_array",
            "cycle_id": "gfs_2026050100",
            "stage_name": "forecast",
            "tasks": [{"run_id": "run_0", "model_id": "model_001"}],
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == AUTH_REQUIRED
    assert client.get("/api/v1/slurm/jobs").json() == []


def test_cancel_without_credential_401_and_no_gateway(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app, service_token=None)
    response = client.delete("/api/v1/slurm/jobs/mock_4040")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == AUTH_REQUIRED


def test_cancel_wrong_bearer_401_and_no_gateway(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    response = client.delete("/api/v1/slurm/jobs/mock_4040", headers=WRONG_BEARER)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == AUTH_REQUIRED


def test_enabled_reset_without_credential_401_and_no_registry_mutation(monkeypatch) -> None:
    app = _standalone_app()
    # Seed one job with a properly-token client, then deny reset with a
    # tokenless client against the SAME app to prove registry is untouched.
    seeded_client = _client_for(monkeypatch, app, service_token=SERVICE_TOKEN)
    seeded = seeded_client.post("/api/v1/slurm/jobs", json=SUBMIT_BODY, headers=SERVICE_BEARER)
    assert seeded.status_code == 201
    _env(monkeypatch, SLURM_GATEWAY_SERVICE_TOKEN=None)
    status, code = _denied_call(seeded_client, "POST", "/api/v1/slurm/internal/reset", None)
    assert status == 401
    assert code == AUTH_REQUIRED
    assert len(seeded_client.get("/api/v1/slurm/jobs").json()) == 1


def test_enabled_reset_wrong_bearer_401(monkeypatch) -> None:
    # Wrong bearer yields no context at all on the identity fallback path, so
    # the decision is AUTH_REQUIRED (401), not RBAC.
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    response = client.post("/api/v1/slurm/internal/reset", headers=WRONG_BEARER)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == AUTH_REQUIRED


# ---------------------------------------------------------------------------
# 4.1/4.2 - viewer identity -> 403 RBAC_FORBIDDEN with zero side effect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/v1/slurm/jobs", SUBMIT_BODY),
        ("POST", "/api/v1/slurm/job-arrays", {"job_type": "run_shud_forecast_array", "cycle_id": "c", "tasks": []}),
        ("DELETE", "/api/v1/slurm/jobs/mock_4040", None),
        ("POST", "/api/v1/slurm/internal/reset", None),
    ],
)
def test_viewer_identity_403_rbac_forbidden_no_side_effect(monkeypatch, method, path, body) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    response = client.request(method, path, json=body, headers=VIEWER_IDENTITY)
    assert response.status_code == 403, (method, path)
    assert response.json()["error"]["code"] == RBAC_FORBIDDEN
    assert client.get("/api/v1/slurm/jobs").json() == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/v1/slurm/jobs", SUBMIT_BODY),
        ("POST", "/api/v1/slurm/job-arrays", {"job_type": "run_shud_forecast_array", "cycle_id": "c", "tasks": []}),
        ("DELETE", "/api/v1/slurm/jobs/mock_4040", None),
    ],
)
def test_viewer_has_no_scheduler_bearer_and_gets_403(monkeypatch, method, path, body) -> None:
    # Explicit no-bearer + viewer role must deny, never fall through to gateway.
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    response = client.request(method, path, json=body, headers={**VIEWER_IDENTITY, "Authorization": ""})
    assert response.status_code == 403, (method, path)
    assert response.json()["error"]["code"] == RBAC_FORBIDDEN


# ---------------------------------------------------------------------------
# 4.2 - scheduler bearer + enabled reset -> 403; release-blocked -> 503
# ---------------------------------------------------------------------------


def test_scheduler_bearer_enabled_reset_403_no_registry_mutation(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    seeded = client.post("/api/v1/slurm/jobs", json=SUBMIT_BODY, headers=SERVICE_BEARER)
    assert seeded.status_code == 201
    response = client.post("/api/v1/slurm/internal/reset", headers=SERVICE_BEARER)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == RBAC_FORBIDDEN
    # Registry unchanged.
    assert len(client.get("/api/v1/slurm/jobs").json()) == 1


def test_release_blocked_live_mode_503_on_submit(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app, service_token=None)
    _env(monkeypatch, AUTH_BACKEND="oidc")
    response = client.post(
        "/api/v1/slurm/jobs",
        json=SUBMIT_BODY,
        headers={"X-Live-User-ID": "live-ops", "X-Live-User-Roles": "operator"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == RELEASE_BLOCKED
    assert client.get("/api/v1/slurm/jobs").json() == []


def test_release_blocked_live_mode_503_on_reset(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app, service_token=None)
    _env(monkeypatch, AUTH_BACKEND="oidc")
    response = client.post("/api/v1/slurm/internal/reset")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == RELEASE_BLOCKED


# ---------------------------------------------------------------------------
# 4.3 - disabled standalone reset: route absent, 404 for every credential
# ---------------------------------------------------------------------------


def test_disabled_standalone_reset_route_absent(monkeypatch) -> None:
    app = _standalone_app(allow_internal_reset=False)
    _client_for(monkeypatch, app)
    assert INTERNAL_RESET_PATH not in {route.path for route in app.routes}


@pytest.mark.parametrize(
    ("headers", "token"),
    [
        (None, None),
        (WRONG_BEARER, None),
        (SERVICE_BEARER, SERVICE_TOKEN),
        (SYSADMIN_IDENTITY, None),
    ],
)
def test_disabled_standalone_reset_404_for_every_credential(monkeypatch, headers, token) -> None:
    app = _standalone_app(allow_internal_reset=False)
    client = _client_for(monkeypatch, app, service_token=token)
    response = client.post("/api/v1/slurm/internal/reset", headers=headers)
    # Route-absent 404: the standalone app has no error envelope registered for
    # framework 404s, so the body is FastAPI's default; the invariant is that it
    # is NEVER 401/403 and never a registered operation.
    assert response.status_code == 404
    assert "error" not in response.json()


# ---------------------------------------------------------------------------
# 4.4 - valid bearer + invalid body -> existing validation; no side effect
# ---------------------------------------------------------------------------


def test_valid_bearer_invalid_submit_body_reaches_validation_only(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    response = client.post("/api/v1/slurm/jobs", json={"model_id": "model_001"}, headers=SERVICE_BEARER)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_MANIFEST"
    assert client.get("/api/v1/slurm/jobs").json() == []


def test_valid_bearer_invalid_array_body_reaches_validation_only(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    response = client.post(
        "/api/v1/slurm/job-arrays",
        json={"cycle_id": "cycle_001", "tasks": []},
        headers=SERVICE_BEARER,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert client.get("/api/v1/slurm/jobs").json() == []


def test_valid_bearer_valid_submit_reaches_gateway(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    response = client.post("/api/v1/slurm/jobs", json=SUBMIT_BODY, headers=SERVICE_BEARER)
    assert response.status_code == 201
    data = response.json()
    assert data["job_id"] == "mock_1001"
    assert data["status"] == "submitted"


def test_valid_dev_operator_identity_submit_reaches_gateway(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app, service_token=None)
    _env(monkeypatch, ALLOW_DEV_ROLE_HEADER="true")
    response = client.post("/api/v1/slurm/jobs", json=SUBMIT_BODY, headers=OPERATOR_IDENTITY)
    assert response.status_code == 201
    assert response.json()["job_id"] == "mock_1001"


def test_valid_sysadmin_identity_enabled_reset_reaches_gateway(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app, service_token=None)
    _env(monkeypatch, ALLOW_DEV_ROLE_HEADER="true")
    seeded = client.post("/api/v1/slurm/jobs", json=SUBMIT_BODY, headers=OPERATOR_IDENTITY)
    assert seeded.status_code == 201
    response = client.post("/api/v1/slurm/internal/reset", headers=SYSADMIN_IDENTITY)
    assert response.status_code == 200
    assert response.json()["cleared"] == 1


def test_model_admin_identity_submit_allowed_cancel_allowed(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app, service_token=None)
    _env(monkeypatch, ALLOW_DEV_ROLE_HEADER="true")
    seeded = client.post("/api/v1/slurm/jobs", json=SUBMIT_BODY, headers=MODEL_ADMIN_IDENTITY)
    assert seeded.status_code == 201
    cancelled = client.delete(f"/api/v1/slurm/jobs/{seeded.json()['job_id']}", headers=MODEL_ADMIN_IDENTITY)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Shared-router mount parity (the full app mounts the same router)
# ---------------------------------------------------------------------------


def test_bare_router_mount_enforces_submit(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(create_slurm_router())
    client = _client_for(monkeypatch, app)
    response = client.post("/api/v1/slurm/jobs", json=SUBMIT_BODY, headers=WRONG_BEARER)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == AUTH_REQUIRED


def test_health_read_stays_anonymous_and_token_free(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    response = client.get("/api/v1/slurm/health")
    assert response.status_code == 200
    assert response.json()["healthy"] is True
    # Reads still work without credentials; the bearer is not required.
    no_token = _standalone_app()
    client_no_token = _client_for(monkeypatch, no_token, service_token=None)
    assert client_no_token.get("/api/v1/slurm/health").status_code == 200


def test_denied_invalid_body_never_reaches_validation(monkeypatch) -> None:
    # A denied request with an invalid mutation body must be 401/403 before the
    # body is parsed: auth resolves ahead of request validation.
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    response = client.post("/api/v1/slurm/jobs", json={"model_id": "model_001"}, headers=WRONG_BEARER)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == AUTH_REQUIRED

    response = client.post("/api/v1/slurm/job-arrays", json={})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == AUTH_REQUIRED


def test_denied_request_never_constructs_or_calls_gateway(monkeypatch) -> None:
    """F1: real zero-construction/no-call proof on the LIVE lazy path.

    The route handlers reach the gateway exclusively through the module lazy
    singleton (``routes.slurm_gateway`` -> ``LazySlurmGateway.__getattr__`` ->
    ``_get()`` -> ``create_gateway``). Replacing the singleton with a mock
    would delete the lazy construction path and make ``constructed==0`` vacuous
    (it would be true even if auth were removed). This proof keeps the lazy
    singleton in place, resets its instance so any access MUST construct, and
    patches only ``create_gateway`` (the construction seam) with a counting
    factory that returns a recording gateway. Four wrong-bearer mutations must
    never trigger construction or a gateway method call.
    """
    import services.slurm_gateway.routes as routes_module

    real_singleton = routes_module.slurm_gateway
    assert isinstance(real_singleton, routes_module.LazySlurmGateway)
    original_create_gateway = routes_module.create_gateway
    constructed = {"count": 0}
    calls: list[tuple[str, list[Any]]] = []

    class RecordingGateway:
        def __getattr__(self, name: str):
            def recorder(*args, **kwargs):
                calls.append((name, [*args, kwargs]))
                raise AssertionError(f"gateway method {name} must not be called on a denied request")

            return recorder

    def counting_create_gateway(*args, **kwargs):
        constructed["count"] += 1
        # The denial must happen BEFORE any construction/method access, so a
        # counting factory that returns a recording gateway makes any accidental
        # construction or call visible (and fails loudly on calls).
        return RecordingGateway()

    # Keep the lazy singleton; patch only the construction seam.
    monkeypatch.setattr(routes_module, "create_gateway", counting_create_gateway)
    real_singleton.reset_instance()
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    try:
        for method, path, body in ALL_MUTATION_CALLS:
            response = client.request(method, path, json=body, headers=WRONG_BEARER)
            assert response.status_code in {401, 403}, (method, path)
        assert constructed["count"] == 0
        assert calls == []

        # Discrimination: invoking a gateway method through the same fresh lazy
        # singleton MUST construct (count 1) and record the call. This proves
        # the counter bites — the zero above is because denial short-circuits
        # before lazy construction, not because the counter cannot observe
        # construction or calls. The recorder raises on invocation, which is
        # exactly what we assert here.
        with pytest.raises(AssertionError, match="gateway method health must not be called"):
            real_singleton.health()
        assert constructed["count"] == 1
        assert [name for name, _ in calls] == ["health"]
    finally:
        # Restore the real factory and reset the lazy instance so the autouse
        # reset_mock_gateway teardown constructs the real mock gateway, never
        # the recording mock (whose method calls fail loudly).
        routes_module.create_gateway = original_create_gateway
        real_singleton.reset_instance()


def test_service_token_value_never_appears_in_denied_response(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    for method, path, body in ALL_MUTATION_CALLS:
        response = client.request(method, path, json=body, headers=WRONG_BEARER)
        assert SERVICE_TOKEN not in response.text
        assert WRONG_BEARER["Authorization"].removeprefix("Bearer ") not in response.text


def test_denied_request_records_redacted_policy_evidence(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    response = client.post("/api/v1/slurm/jobs", json=SUBMIT_BODY)
    details = response.json()["error"]["details"]
    assert details["policy_decision"]["action_id"] == "slurm.submit_job"
    assert details["policy_decision"]["reason_code"] == AUTH_REQUIRED
    assert details["audit_record"]["reason_code"] == AUTH_REQUIRED
    assert details["audit_record"]["no_mutation_expected"] is True
    assert SERVICE_TOKEN not in response.text


def test_service_bearer_rejected_by_original_business_mutation(monkeypatch) -> None:
    # The scheduler bearer must NOT authenticate POST /api/v1/runs/{run_id}/retry.
    # The full compute/dev API mounts the business routes; the bearer only ever
    # reaches the Slurm mutation dependency, so the retry guard returns 401
    # AUTH_REQUIRED before any gateway/business side effect.
    from apps.api.main import create_app

    app = create_app(
        {"NHMS_SERVICE_ROLE": "compute_control", "NHMS_REQUIRE_SERVICE_ROLE": "true"}
    )
    client = _client_for(
        monkeypatch,
        app,
        service_token=SERVICE_TOKEN,
        allow_dev_role_header=False,
        additional_env={"NHMS_SERVICE_ROLE": "compute_control", "NHMS_REQUIRE_SERVICE_ROLE": "true"},
    )
    response = client.post(
        "/api/v1/runs/run_001/retry",
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == AUTH_REQUIRED


# ---------------------------------------------------------------------------
# F3 - request-id parity for standalone denial audit
# ---------------------------------------------------------------------------


def test_denied_standalone_request_id_parity_caller_provided(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app, service_token=None)
    response = client.post(
        "/api/v1/slurm/jobs",
        json=SUBMIT_BODY,
        headers={"X-Request-ID": "req-caller-1684"},
    )
    body = response.json()
    assert response.headers.get("X-Request-ID") == "req-caller-1684"
    assert body["request_id"] == "req-caller-1684"
    assert body["error"]["details"]["audit_record"]["request_id"] == "req-caller-1684"
    assert body["error"]["details"]["policy_decision"]["reason_code"] == AUTH_REQUIRED


def test_denied_standalone_request_id_parity_generated(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app, service_token=None)
    response = client.post("/api/v1/slurm/jobs", json=SUBMIT_BODY)
    body = response.json()
    header_id = response.headers.get("X-Request-ID")
    assert header_id and header_id.startswith("req_")
    assert body["request_id"] == header_id
    assert body["error"]["details"]["audit_record"]["request_id"] == header_id


def test_denied_standalone_403_503_request_id_parity(monkeypatch) -> None:
    app = _standalone_app()
    client = _client_for(monkeypatch, app)
    # 403 RBAC via viewer identity.
    response = client.post(
        "/api/v1/slurm/jobs",
        json=SUBMIT_BODY,
        headers={"X-Request-ID": "req-403-1684", **VIEWER_IDENTITY},
    )
    body = response.json()
    assert response.status_code == 403
    assert response.headers.get("X-Request-ID") == "req-403-1684"
    assert body["request_id"] == "req-403-1684"
    assert body["error"]["details"]["audit_record"]["request_id"] == "req-403-1684"

    # 503 release-blocked via live backend without proof.
    tokenless = _client_for(monkeypatch, app, service_token=None)
    _env(monkeypatch, AUTH_BACKEND="oidc")
    response = tokenless.post(
        "/api/v1/slurm/jobs",
        json=SUBMIT_BODY,
        headers={"X-Request-ID": "req-503-1684"},
    )
    body = response.json()
    assert response.status_code == 503
    assert response.headers.get("X-Request-ID") == "req-503-1684"
    assert body["request_id"] == "req-503-1684"
    assert body["error"]["details"]["audit_record"]["request_id"] == "req-503-1684"


def test_denied_full_app_request_id_parity(monkeypatch) -> None:
    from apps.api.main import create_app

    app = create_app(
        {"NHMS_SERVICE_ROLE": "compute_control", "NHMS_REQUIRE_SERVICE_ROLE": "true"}
    )
    client = _client_for(
        monkeypatch,
        app,
        service_token=None,
        allow_dev_role_header=False,
        additional_env={"NHMS_SERVICE_ROLE": "compute_control", "NHMS_REQUIRE_SERVICE_ROLE": "true"},
    )
    response = client.post(
        "/api/v1/slurm/jobs",
        json=SUBMIT_BODY,
        headers={"X-Request-ID": "req-full-1684"},
    )
    body = response.json()
    assert response.status_code == 401
    assert response.headers.get("X-Request-ID") == "req-full-1684"
    assert body["request_id"] == "req-full-1684"
    assert body["error"]["details"]["audit_record"]["request_id"] == "req-full-1684"
