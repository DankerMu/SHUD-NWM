from __future__ import annotations

import os
from collections.abc import Mapping

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.runtime_mode import RuntimeModeError, ServiceRole, load_runtime_config

_ROLE_ENV_KEYS = (
    "NHMS_SERVICE_ROLE",
    "NHMS_REQUIRE_SERVICE_ROLE",
    "NHMS_AUTH_MODE",
    "SLURM_GATEWAY_URL",
    "SLURM_GATEWAY_BACKEND",
    "WORKSPACE_ROOT",
    "NHMS_BASINS_ROOT",
    "SHUD_EXECUTABLE",
    "NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS",
)


def test_local_default_runtime_role_is_dev_monolith() -> None:
    config = load_runtime_config(_clean_env())

    assert config.service_role == ServiceRole.DEV_MONOLITH
    assert config.service_role_explicit is False
    assert config.control_mutations_enabled is True
    assert config.slurm_routes_enabled is True
    assert config.queue_depth_mode == "slurm_gateway"


@pytest.mark.parametrize(
    "env",
    [
        {"NHMS_REQUIRE_SERVICE_ROLE": "true"},
        {"NHMS_AUTH_MODE": "production"},
        {"NHMS_AUTH_MODE": "live"},
        {"NHMS_AUTH_MODE": "live_idp"},
    ],
)
def test_production_like_startup_requires_explicit_service_role(env: dict[str, str]) -> None:
    with pytest.raises(RuntimeModeError) as exc_info:
        create_app(_clean_env(env))

    assert exc_info.value.code == "SERVICE_ROLE_REQUIRED"
    assert exc_info.value.details["env_var"] == "NHMS_SERVICE_ROLE"


def test_unknown_service_role_fails_before_app_is_served() -> None:
    with pytest.raises(RuntimeModeError) as exc_info:
        create_app(_clean_env({"NHMS_REQUIRE_SERVICE_ROLE": "true", "NHMS_SERVICE_ROLE": "control"}))

    assert exc_info.value.code == "SERVICE_ROLE_UNSUPPORTED"
    assert exc_info.value.details["service_role"] == "control"


def test_slurm_gateway_role_is_reserved_and_does_not_start_business_api() -> None:
    with pytest.raises(RuntimeModeError) as exc_info:
        create_app(_clean_env({"NHMS_REQUIRE_SERVICE_ROLE": "true", "NHMS_SERVICE_ROLE": "slurm_gateway"}))

    assert exc_info.value.code == "SERVICE_ROLE_RESERVED"
    assert exc_info.value.details == {"service_role": "slurm_gateway", "bounded_gateway_app": False}


def test_display_readonly_starts_with_runtime_config_and_without_slurm_routes() -> None:
    app = create_app(_display_env())

    with TestClient(app) as client:
        config_response = client.get("/api/v1/runtime/config")
        slurm_response = client.get("/api/v1/slurm/health")
        openapi = client.get("/openapi.json").json()

    assert config_response.status_code == 200
    assert config_response.json()["data"] == {
        "service_role": "display_readonly",
        "control_mutations_enabled": False,
        "slurm_routes_enabled": False,
        "queue_depth_mode": "display_readonly_unavailable",
        "display_readonly": True,
    }
    assert slurm_response.status_code == 404
    assert not _route_paths(app, "/api/v1/slurm")
    assert not any(path.startswith("/api/v1/slurm") for path in openapi["paths"])


@pytest.mark.parametrize("role", ["dev_monolith", "compute_control"])
def test_dev_and_compute_roles_keep_slurm_routes_and_runtime_config(role: str) -> None:
    app = create_app(_clean_env({"NHMS_SERVICE_ROLE": role}))

    with TestClient(app) as client:
        config_response = client.get("/api/v1/runtime/config")
        slurm_response = client.get("/api/v1/slurm/health")
        openapi = client.get("/openapi.json").json()

    assert config_response.status_code == 200
    assert config_response.json()["data"] == {
        "service_role": role,
        "control_mutations_enabled": True,
        "slurm_routes_enabled": True,
        "queue_depth_mode": "slurm_gateway",
        "display_readonly": False,
    }
    assert slurm_response.status_code == 200
    assert "/api/v1/slurm/health" in _route_paths(app, "/api/v1/slurm")
    assert "/api/v1/slurm/health" in openapi["paths"]


@pytest.mark.parametrize(
    ("env_var", "value", "expected_code"),
    [
        ("SLURM_GATEWAY_URL", "http://node22.internal:8000", "DISPLAY_SLURM_GATEWAY_URL_FORBIDDEN"),
        ("SLURM_GATEWAY_BACKEND", "slurm", "DISPLAY_SLURM_BACKEND_FORBIDDEN"),
        ("WORKSPACE_ROOT", "/work/nhms", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_BASINS_ROOT", "/data/Basins", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("SHUD_EXECUTABLE", "/opt/shud/bin/shud", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS", "false", "DISPLAY_CONTROL_MUTATIONS_FORBIDDEN"),
    ],
)
def test_display_readonly_blocks_unsafe_compute_config(
    env_var: str,
    value: str,
    expected_code: str,
) -> None:
    with pytest.raises(RuntimeModeError) as exc_info:
        create_app(_display_env({env_var: value}))

    assert exc_info.value.code == "DISPLAY_BOUNDARY_CONFIG_UNSAFE"
    blockers = exc_info.value.details["blockers"]
    assert {blocker["env_var"] for blocker in blockers} == {env_var}
    assert {blocker["code"] for blocker in blockers} == {expected_code}


def _display_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    return _clean_env(
        {
            "NHMS_REQUIRE_SERVICE_ROLE": "true",
            "NHMS_SERVICE_ROLE": "display_readonly",
            **dict(extra or {}),
        }
    )


def _clean_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _ROLE_ENV_KEYS}
    env.update(extra or {})
    return env


def _route_paths(app: object, prefix: str) -> set[str]:
    return {
        str(getattr(route, "path", ""))
        for route in getattr(app, "routes", [])
        if str(getattr(route, "path", "")).startswith(prefix)
    }
