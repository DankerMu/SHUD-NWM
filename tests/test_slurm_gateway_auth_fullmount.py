"""Full compute/dev mount auth matrix for the four registered Slurm mutations.

Partitioned out of ``tests/test_slurm_gateway_auth.py`` for the repo
1,000-line limit (#1684 EVID-02). This module owns the FULL app
(``apps.api.main.create_app`` / compute-control role) denial matrix:

- every mutation shape x anonymous / wrong bearer / viewer / release-blocked;
- the non-vacuous F1 zero-construction/no-call proof on the full mount;
- scheduler-bearer reset 403 + registry unchanged;
- scheduler bearer authorizes Slurm mutation while the SAME bearer cannot
  authenticate a business mutation (production OIDC/release-block settings);
- full-app disabled-reset historical 403 preserved (registered route, refusal
  before auth).

Helpers/constants are imported from ``tests/test_slurm_gateway_auth.py``, which
stays the single home for the shared fixtures (no circular import: this module
is never imported by that suite).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from apps.api.main import create_app
from packages.common.auth_policy import AUTH_REQUIRED, RBAC_FORBIDDEN, RELEASE_BLOCKED
from services.slurm_gateway.config import SlurmGatewaySettings, get_settings
from services.slurm_gateway.models import ResetRequest
from services.slurm_gateway.routes import slurm_gateway
from tests.test_slurm_gateway_auth import (
    ALL_MUTATION_CALLS,
    NON_ASCII_AUTH_BYTES,
    SERVICE_BEARER,
    SERVICE_TOKEN,
    SUBMIT_BODY,
    VIEWER_IDENTITY,
    WRONG_BEARER,
    _client_for,
)

_FULL_COMPUTE_ENV: dict[str, str] = {
    "NHMS_SERVICE_ROLE": "compute_control",
    "NHMS_REQUIRE_SERVICE_ROLE": "true",
}


@pytest.fixture(autouse=True)
def reset_mock_gateway():
    """Reset the shared mock gateway registry around each test.

    The full app and the standalone suite share the module-level
    ``routes.slurm_gateway`` lazy singleton, so a seeded job from one test
    would otherwise leak into the next (409-duplicate or exact-count drift).
    Mirrors the autouse fixture in tests/test_slurm_gateway_auth.py.
    """
    slurm_gateway.reset(ResetRequest(restore_defaults=True))
    yield
    slurm_gateway.reset(ResetRequest(restore_defaults=True))


def _full_compute_app(*, allow_internal_reset: bool = False) -> FastAPI:
    app = create_app(_FULL_COMPUTE_ENV)
    # The reset handler resolves settings through the cached get_settings()
    # dependency, so pin it to the same settings the full app would use at
    # runtime. Disabled default preserves the historical full-app behavior:
    # the route IS registered and returns 403 SLURM_INTERNAL_RESET_DISABLED
    # before any auth (the standalone app instead omits the route -> 404).
    settings = SlurmGatewaySettings(backend="mock", allow_internal_reset=allow_internal_reset)
    app.dependency_overrides[get_settings] = lambda: settings
    return app


def _full_compute_client(
    monkeypatch,
    app: FastAPI,
    *,
    allow_dev_role_header: bool = False,
    **env_values: str | None,
):
    client_env = dict(_FULL_COMPUTE_ENV)
    client_env.update(env_values)
    # The app was built from the same env; `_client_for` mirrors those values
    # into the process env so the route dependency and reset handler see them.
    return _client_for(monkeypatch, app, allow_dev_role_header=allow_dev_role_header, additional_env=client_env)


def test_full_app_non_ascii_header_401_auth_required_not_500(monkeypatch) -> None:
    """Raw non-ASCII Authorization bytes on the FULL compute/dev mount -> 401."""
    app = _full_compute_app()
    client = _full_compute_client(monkeypatch, app, SLURM_GATEWAY_SERVICE_TOKEN=SERVICE_TOKEN)
    response = client.post(
        "/api/v1/slurm/jobs",
        json=SUBMIT_BODY,
        headers={"Authorization": NON_ASCII_AUTH_BYTES},
    )
    assert response.status_code == 401, response.text
    body = response.json()
    assert body["error"]["code"] == AUTH_REQUIRED
    assert body["error"]["details"]["policy_decision"]["reason_code"] == AUTH_REQUIRED
    assert body["error"]["details"]["audit_record"]["reason_code"] == AUTH_REQUIRED
    assert body["error"]["details"]["audit_record"]["no_mutation_expected"] is True
    assert body["request_id"] == response.headers.get("X-Request-ID")


def test_denied_full_app_request_id_parity(monkeypatch) -> None:
    app = _full_compute_app()
    client = _full_compute_client(monkeypatch, app, SLURM_GATEWAY_SERVICE_TOKEN=None)
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


@pytest.mark.parametrize(
    ("method", "path", "body"),
    ALL_MUTATION_CALLS,
)
def test_full_app_anonymous_denial_all_four_mutations(monkeypatch, method, path, body) -> None:
    """No credential on the full compute/dev mount -> 401 before validation.

    The reset leg uses an ENABLED reset: the full app always registers the
    route, and its disabled historical behavior (403 SLURM_INTERNAL_RESET_DISABLED
    before auth) is preserved and asserted separately below; this matrix proves
    the auth boundary for all four registered mutation shapes.
    """
    app = _full_compute_app(allow_internal_reset=True)
    client = _full_compute_client(monkeypatch, app, SLURM_GATEWAY_SERVICE_TOKEN=None)
    response = client.request(method, path, json=body)
    assert response.status_code == 401, (method, path, response.text)
    assert response.json()["error"]["code"] == AUTH_REQUIRED, (method, path)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    ALL_MUTATION_CALLS,
)
def test_full_app_wrong_bearer_denial_all_four_mutations(monkeypatch, method, path, body) -> None:
    """Wrong bearer on the full compute/dev mount -> 401 before validation."""
    app = _full_compute_app(allow_internal_reset=True)
    client = _full_compute_client(monkeypatch, app, SLURM_GATEWAY_SERVICE_TOKEN=SERVICE_TOKEN)
    response = client.request(method, path, json=body, headers=WRONG_BEARER)
    assert response.status_code == 401, (method, path, response.text)
    assert response.json()["error"]["code"] == AUTH_REQUIRED, (method, path)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    ALL_MUTATION_CALLS,
)
def test_full_app_viewer_denial_all_four_mutations(monkeypatch, method, path, body) -> None:
    """Viewer role on the full compute/dev mount -> 403, no side effect."""
    app = _full_compute_app(allow_internal_reset=True)
    client = _full_compute_client(
        monkeypatch, app, SLURM_GATEWAY_SERVICE_TOKEN=None, allow_dev_role_header=True
    )
    response = client.request(method, path, json=body, headers=VIEWER_IDENTITY)
    assert response.status_code == 403, (method, path, response.text)
    assert response.json()["error"]["code"] == RBAC_FORBIDDEN, (method, path)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    ALL_MUTATION_CALLS,
)
def test_full_app_release_blocked_denial_all_four_mutations(monkeypatch, method, path, body) -> None:
    """Live OIDC without a trusted proof on the full compute/dev mount -> 503."""
    app = _full_compute_app(allow_internal_reset=True)
    client = _full_compute_client(monkeypatch, app, SLURM_GATEWAY_SERVICE_TOKEN=None, AUTH_BACKEND="oidc")
    response = client.request(
        method, path, json=body, headers={"X-Live-User-ID": "live-ops", "X-Live-User-Roles": "operator"}
    )
    assert response.status_code == 503, (method, path, response.text)
    assert response.json()["error"]["code"] == RELEASE_BLOCKED, (method, path)


def test_full_app_disabled_reset_historical_403_before_auth(monkeypatch) -> None:
    """Full-app disabled reset keeps its historical behavior: registered route,
    403 SLURM_INTERNAL_RESET_DISABLED before any auth, never 401/503."""
    app = _full_compute_app(allow_internal_reset=False)
    client = _full_compute_client(monkeypatch, app, SLURM_GATEWAY_SERVICE_TOKEN=None)
    response = client.post("/api/v1/slurm/internal/reset")
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "SLURM_INTERNAL_RESET_DISABLED"
    # Wrong bearer reaches the same disabled refusal: no auth decision is made.
    client2 = _full_compute_client(monkeypatch, app, SLURM_GATEWAY_SERVICE_TOKEN=SERVICE_TOKEN)
    response = client2.post("/api/v1/slurm/internal/reset", headers=WRONG_BEARER)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "SLURM_INTERNAL_RESET_DISABLED"


def test_full_app_denied_requests_never_construct_or_call_gateway(monkeypatch) -> None:
    """Full-app F1: denial short-circuits BEFORE lazy gateway construction.

    Same non-vacuous proof as the standalone arm, against the same shared
    lazy singleton the full compute/dev app reaches: the counting factory must
    observe ZERO constructions on four denied mutations with the reset ENABLED
    (so every leg actually passes the auth dependency), then prove it can
    observe one (discrimination) through the same fresh lazy instance.
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
        return RecordingGateway()

    monkeypatch.setattr(routes_module, "create_gateway", counting_create_gateway)
    real_singleton.reset_instance()
    app = _full_compute_app(allow_internal_reset=True)
    client = _full_compute_client(monkeypatch, app, SLURM_GATEWAY_SERVICE_TOKEN=SERVICE_TOKEN)
    try:
        # Four wrong-bearer mutations: exact 401 AUTH_REQUIRED, zero construction,
        # zero calls. (The reset leg uses an ENABLED reset so it reaches the auth
        # dependency instead of the pre-auth disabled refusal.)
        for method, path, body in ALL_MUTATION_CALLS:
            response = client.request(method, path, json=body, headers=WRONG_BEARER)
            assert response.status_code == 401, (method, path, response.text)
            assert response.json()["error"]["code"] == AUTH_REQUIRED, (method, path)
        assert constructed["count"] == 0
        assert calls == []

        # Discrimination: the counting factory can construct through the same
        # lazy singleton, proving the zero above is not a counter that cannot see.
        with pytest.raises(AssertionError, match="gateway method health must not be called"):
            real_singleton.health()
        assert constructed["count"] == 1
        assert [name for name, _ in calls] == ["health"]
    finally:
        routes_module.create_gateway = original_create_gateway
        real_singleton.reset_instance()


def test_full_app_scheduler_bearer_reset_403_registry_unchanged(monkeypatch) -> None:
    """Scheduler bearer + enabled reset on the FULL mount -> 403, registry intact."""
    app = _full_compute_app(allow_internal_reset=True)
    client = _full_compute_client(monkeypatch, app, SLURM_GATEWAY_SERVICE_TOKEN=SERVICE_TOKEN)
    seeded = client.post("/api/v1/slurm/jobs", json=SUBMIT_BODY, headers=SERVICE_BEARER)
    assert seeded.status_code == 201, seeded.text
    response = client.post("/api/v1/slurm/internal/reset", headers=SERVICE_BEARER)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == RBAC_FORBIDDEN
    # Registry unchanged: the seeded job is still listed.
    assert len(client.get("/api/v1/slurm/jobs").json()) == 1


def test_full_app_scheduler_bearer_allows_mutation_without_business_access(monkeypatch) -> None:
    """Production OIDC/release-block mode: scheduler bearer still authorizes a
    Slurm mutation, while the SAME bearer cannot authenticate a business retry.

    The route-scoped service identity is tested first by the Slurm mutation
    dependency, so a valid scheduler bearer reaches the gateway even when the
    live identity modes are release-blocked. The business retry middleware never
    accepts this credential, so the same bearer gets 401 there.
    """
    app = create_app(
        {
            "NHMS_SERVICE_ROLE": "compute_control",
            "NHMS_REQUIRE_SERVICE_ROLE": "true",
            "AUTH_BACKEND": "oidc",
            "NHMS_AUTH_MODE": "live_idp",
        }
    )
    client = _full_compute_client(
        monkeypatch,
        app,
        SLURM_GATEWAY_SERVICE_TOKEN=SERVICE_TOKEN,
        AUTH_BACKEND="oidc",
        NHMS_AUTH_MODE="live_idp",
    )
    # Slurm mutation: valid scheduler bearer -> 201 (auth passed, gateway ran).
    response = client.post("/api/v1/slurm/jobs", json=SUBMIT_BODY, headers=SERVICE_BEARER)
    assert response.status_code == 201, response.text
    # Same bearer on an original business mutation -> NOT authenticated by it.
    # In release-blocked live mode the canonical rejection is 503
    # RELEASE_BLOCKED with the release-blocked identity (never the scheduler
    # actor), proving the scheduler bearer is not accepted there.
    response = client.post("/api/v1/runs/run_001/retry", headers=SERVICE_BEARER)
    body = response.json()
    assert response.status_code in {401, 403, 503}, response.text
    assert body["error"]["code"] in {AUTH_REQUIRED, RBAC_FORBIDDEN, RELEASE_BLOCKED}
    audit_actor = body["error"]["details"]["policy_decision"]["actor_id"]
    assert audit_actor != "slurm-scheduler", (
        f"scheduler bearer authenticated a business mutation: {audit_actor}"
    )
