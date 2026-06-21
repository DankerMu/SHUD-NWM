from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from apps.api.runtime_mode import RuntimeModeError, ServiceRole, display_boundary_blockers, load_runtime_config

_ROLE_ENV_KEYS = (
    "NHMS_SERVICE_ROLE",
    "NHMS_REQUIRE_SERVICE_ROLE",
    "NHMS_AUTH_MODE",
    "AUTH_BACKEND",
    "SLURM_GATEWAY_URL",
    "SLURM_GATEWAY_BACKEND",
    "WORKSPACE_ROOT",
    "RUN_WORKSPACE_ROOT",
    "SHARED_LOG_ROOT",
    "OBJECT_STORE_ROOT",
    "NHMS_OBJECT_STORE_COPYBACK_ROOT",
    "NHMS_BASINS_ROOT",
    "NHMS_MODEL_ASSET_ROOT",
    "SLURM_GATEWAY_TEMPLATE_DIR",
    "SLURM_GATEWAY_WORKSPACE_DIR",
    "MUNGE_SOCKET",
    "MUNGE_KEY",
    "SHUD_EXECUTABLE",
    "DOCKER_HOST",
    "NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS",
)


def test_local_default_runtime_role_is_dev_monolith() -> None:
    config = load_runtime_config(_clean_env())

    assert config.service_role == ServiceRole.DEV_MONOLITH
    assert config.service_role_explicit is False
    assert config.object_store_root is None
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
        {"AUTH_BACKEND": "live"},
        {"AUTH_BACKEND": "live_idp"},
        {"AUTH_BACKEND": "oidc"},
        {"AUTH_BACKEND": "saml"},
    ],
)
def test_production_like_startup_requires_explicit_service_role(env: dict[str, str]) -> None:
    with pytest.raises(RuntimeModeError) as exc_info:
        create_app(_clean_env(env))

    assert exc_info.value.code == "SERVICE_ROLE_REQUIRED"
    assert exc_info.value.details["env_var"] == "NHMS_SERVICE_ROLE"


def test_malformed_require_service_role_fails_before_app_is_served() -> None:
    with pytest.raises(RuntimeModeError) as exc_info:
        create_app(_clean_env({"NHMS_REQUIRE_SERVICE_ROLE": "ture"}))

    assert exc_info.value.code == "SERVICE_ROLE_REQUIRE_FLAG_INVALID"
    assert exc_info.value.details["env_var"] == "NHMS_REQUIRE_SERVICE_ROLE"
    assert "accepted_truthy_values" in exc_info.value.details
    assert "accepted_falsy_values" in exc_info.value.details


@pytest.mark.parametrize(
    "env",
    [
        {"NHMS_REQUIRE_SERVICE_ROLE": "true", "NHMS_SERVICE_ROLE": "control"},
        {"NHMS_SERVICE_ROLE": "control"},
    ],
)
def test_unknown_service_role_fails_before_app_is_served(env: dict[str, str]) -> None:
    with pytest.raises(RuntimeModeError) as exc_info:
        create_app(_clean_env(env))

    assert exc_info.value.code == "SERVICE_ROLE_UNSUPPORTED"
    assert exc_info.value.details["service_role"] == "control"


def test_slurm_gateway_role_is_reserved_and_does_not_start_business_api() -> None:
    with pytest.raises(RuntimeModeError) as exc_info:
        create_app(_clean_env({"NHMS_REQUIRE_SERVICE_ROLE": "true", "NHMS_SERVICE_ROLE": "slurm_gateway"}))

    assert exc_info.value.code == "SERVICE_ROLE_RESERVED"
    assert exc_info.value.details == {"service_role": "slurm_gateway", "bounded_gateway_app": True}


def test_display_readonly_starts_with_runtime_config_and_without_slurm_routes(tmp_path: Path) -> None:
    app = create_app(_display_env(tmp_path))

    with TestClient(app) as client:
        config_response = client.get("/api/v1/runtime/config")
        slurm_response = client.get("/api/v1/slurm/health")
        openapi = client.get("/openapi.json").json()

    assert app.state.object_store_root == (tmp_path / "object-store").resolve()
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


@pytest.mark.parametrize("role", ["dev_monolith", "compute_control"])
@pytest.mark.parametrize("backend", ["slurm", "mock", ""])
def test_dev_and_compute_roles_allow_slurm_gateway_backend_env(role: str, backend: str) -> None:
    config = load_runtime_config(_clean_env({"NHMS_SERVICE_ROLE": role, "SLURM_GATEWAY_BACKEND": backend}))

    assert config.service_role == ServiceRole(role)
    assert config.slurm_routes_enabled is True


@pytest.mark.parametrize("role", ["dev_monolith", "compute_control"])
@pytest.mark.parametrize("url", ["http://node22.internal:8000", ""])
def test_dev_and_compute_roles_allow_slurm_gateway_url_env(role: str, url: str) -> None:
    config = load_runtime_config(_clean_env({"NHMS_SERVICE_ROLE": role, "SLURM_GATEWAY_URL": url}))

    assert config.service_role == ServiceRole(role)
    assert config.slurm_routes_enabled is True


def test_display_route_inventory_preserves_non_slurm_business_routes(tmp_path: Path) -> None:
    display_app = create_app(_display_env(tmp_path))
    compute_app = create_app(_clean_env({"NHMS_SERVICE_ROLE": "compute_control"}))
    display_routes = _route_keys(display_app)
    compute_routes = _route_keys(compute_app)
    slurm_routes = {route for route in compute_routes if route[1].startswith("/api/v1/slurm/")}
    expected_business_read_routes = {
        ("GET", "/api/v1/models"),
        ("GET", "/api/v1/runs"),
        ("GET", "/api/v1/mvp/qhh/latest-product"),
        ("GET", "/api/v1/jobs"),
        ("GET", "/api/v1/pipeline/status"),
        ("GET", "/api/v1/pipeline/stages"),
        ("GET", "/api/v1/queue/depth"),
        ("GET", "/api/v1/data-sources"),
    }

    assert slurm_routes
    assert compute_routes - display_routes == slurm_routes
    assert not display_routes - compute_routes
    assert expected_business_read_routes <= display_routes
    assert expected_business_read_routes <= compute_routes


@pytest.mark.parametrize(
    ("env_var", "value", "expected_code"),
    [
        ("SLURM_GATEWAY_URL", "http://node22.internal:8000", "DISPLAY_SLURM_GATEWAY_URL_FORBIDDEN"),
        ("SLURM_GATEWAY_URL", "", "DISPLAY_SLURM_GATEWAY_URL_FORBIDDEN"),
        ("SLURM_GATEWAY_BACKEND", "slurm", "DISPLAY_SLURM_BACKEND_FORBIDDEN"),
        ("SLURM_GATEWAY_BACKEND", "mock", "DISPLAY_SLURM_BACKEND_FORBIDDEN"),
        ("SLURM_GATEWAY_BACKEND", "", "DISPLAY_SLURM_BACKEND_FORBIDDEN"),
        ("WORKSPACE_ROOT", "/work/nhms", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("WORKSPACE_ROOT", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("RUN_WORKSPACE_ROOT", "/work/nhms/runs", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("RUN_WORKSPACE_ROOT", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("SHARED_LOG_ROOT", "/work/nhms/logs", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("SHARED_LOG_ROOT", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_OBJECT_STORE_COPYBACK_ROOT", "/ghdc/data/nwm/object-store", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_OBJECT_STORE_COPYBACK_ROOT", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_SCHEDULER_LOCK_ROOT", "/work/nhms/scheduler/locks", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_SCHEDULER_LOCK_ROOT", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_SCHEDULER_EVIDENCE_ROOT", "/work/nhms/scheduler/evidence", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_SCHEDULER_EVIDENCE_ROOT", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_SCHEDULER_RUNTIME_ROOT", "/work/nhms/runtime", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_SCHEDULER_RUNTIME_ROOT", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_SCHEDULER_TEMP_ROOT", "/work/nhms/tmp", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_SCHEDULER_TEMP_ROOT", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_BASINS_ROOT", "/data/Basins", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_BASINS_ROOT", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_MODEL_ASSET_ROOT", "/data/model-assets", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_MODEL_ASSET_ROOT", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("SLURM_GATEWAY_TEMPLATE_DIR", "/app/infra/sbatch", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("SLURM_GATEWAY_TEMPLATE_DIR", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("SLURM_GATEWAY_WORKSPACE_DIR", "/work/nhms/slurm", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("SLURM_GATEWAY_WORKSPACE_DIR", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("MUNGE_SOCKET", "/run/munge/munge.socket.2", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("MUNGE_SOCKET", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("MUNGE_KEY", "/etc/munge/munge.key", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("MUNGE_KEY", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("SHUD_EXECUTABLE", "/opt/shud/bin/shud", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("SHUD_EXECUTABLE", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("DOCKER_HOST", "unix:///var/run/docker.sock", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("DOCKER_HOST", "", "DISPLAY_COMPUTE_PATH_FORBIDDEN"),
        ("NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS", "false", "DISPLAY_CONTROL_MUTATIONS_FORBIDDEN"),
    ],
)
def test_display_readonly_blocks_unsafe_compute_config(
    tmp_path: Path,
    env_var: str,
    value: str,
    expected_code: str,
) -> None:
    with pytest.raises(RuntimeModeError) as exc_info:
        create_app(_display_env(tmp_path, {env_var: value}))

    assert exc_info.value.code == "DISPLAY_BOUNDARY_CONFIG_UNSAFE"
    blockers = exc_info.value.details["blockers"]
    assert {blocker["env_var"] for blocker in blockers} == {env_var}
    assert {blocker["code"] for blocker in blockers} == {expected_code}


def test_display_readonly_requires_object_store_root() -> None:
    with pytest.raises(RuntimeModeError) as exc_info:
        create_app(_clean_env({"NHMS_REQUIRE_SERVICE_ROLE": "true", "NHMS_SERVICE_ROLE": "display_readonly"}))

    assert exc_info.value.code == "OBJECT_STORE_ROOT_REQUIRED"
    assert "OBJECT_STORE_ROOT env var is required" in exc_info.value.message
    assert exc_info.value.details["env_var"] == "OBJECT_STORE_ROOT"


@pytest.mark.parametrize("role", ["display_readonly", "compute_control", "dev_monolith"])
def test_configured_object_store_root_must_be_readable_directory(tmp_path: Path, role: str) -> None:
    missing_root = tmp_path / "missing-object-store"

    with pytest.raises(RuntimeModeError) as exc_info:
        create_app(
            _clean_env(
                {
                    "NHMS_SERVICE_ROLE": role,
                    "OBJECT_STORE_ROOT": str(missing_root),
                    **({"NHMS_REQUIRE_SERVICE_ROLE": "true"} if role == "display_readonly" else {}),
                }
            )
        )

    assert exc_info.value.code == "OBJECT_STORE_ROOT_UNREADABLE"
    assert "is not a readable directory" in exc_info.value.message
    assert exc_info.value.details["path"] == str(missing_root.resolve())


def test_display_readonly_allows_readable_object_store_root_without_boundary_blocker(tmp_path: Path) -> None:
    config = load_runtime_config(_display_env(tmp_path))
    blockers = display_boundary_blockers(_display_env(tmp_path))

    assert config.object_store_root == (tmp_path / "object-store").resolve()
    assert not any(blocker.env_var == "OBJECT_STORE_ROOT" for blocker in blockers)


def _display_env(tmp_path: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    object_store_root = tmp_path / "object-store"
    object_store_root.mkdir(exist_ok=True)
    return _clean_env(
        {
            "NHMS_REQUIRE_SERVICE_ROLE": "true",
            "NHMS_SERVICE_ROLE": "display_readonly",
            "OBJECT_STORE_ROOT": str(object_store_root),
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
        for route in _iter_routes(app)
        if str(getattr(route, "path", "")).startswith(prefix)
    }


def _route_keys(app: object) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for route in _iter_routes(app):
        path = str(getattr(route, "path", ""))
        methods = getattr(route, "methods", None)
        if methods is None:
            continue
        for method in methods:
            routes.add((str(method).upper(), path))
    return routes


def _iter_routes(app_or_router: object) -> Iterable[object]:
    for route in getattr(app_or_router, "routes", []):
        yield route
        included_router = getattr(route, "original_router", None)
        if included_router is None:
            include_context = getattr(route, "include_context", None)
            included_router = getattr(include_context, "included_router", None)
        if included_router is not None:
            yield from _iter_routes(included_router)
