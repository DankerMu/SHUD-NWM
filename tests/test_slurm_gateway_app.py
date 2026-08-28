"""Lane 1 gateway-core tests: standalone bounded app, 4-binary health, parity."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.routing import APIRoute
from starlette.routing import Mount

from packages.common.openapi_auth_security import (
    SECURITY_SCHEME_NAMES,
    security_scheme_definitions,
)
from services.slurm_gateway.app import INTERNAL_RESET_PATH, create_gateway_app
from services.slurm_gateway.config import SlurmGatewaySettings
from services.slurm_gateway.mock_backend import MockSlurmGateway
from services.slurm_gateway.models import SLURM_HEALTH_BINARIES
from services.slurm_gateway.real_backend import RealSlurmGateway

FRAMEWORK_ROUTE_PATHS = frozenset(
    {
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)
BUSINESS_MARKERS = ("forecast", "model", "pipeline", "data-source")

SLURM_MUTATION_PATHS = {
    ("POST", "/api/v1/slurm/jobs"),
    ("POST", "/api/v1/slurm/job-arrays"),
    ("DELETE", "/api/v1/slurm/jobs/{job_id}"),
    ("POST", "/api/v1/slurm/internal/reset"),
}
SLURM_READ_PATHS = {
    ("GET", "/api/v1/slurm/health"),
    ("GET", "/api/v1/slurm/jobs"),
    ("GET", "/api/v1/slurm/jobs/{job_id}"),
    ("GET", "/api/v1/slurm/jobs/{job_id}/array-tasks"),
    ("GET", "/api/v1/slurm/jobs/{job_id}/logs"),
}


def _collect_referenced_and_defined(spec: dict[str, Any]) -> tuple[set[str], set[str]]:
    referenced: set[str] = set()
    for _path, operations in spec["paths"].items():
        for operation in operations.values():
            if not isinstance(operation, dict):
                continue
            for requirement in operation.get("security", []):
                referenced.update(requirement)
    defined = set(spec.get("components", {}).get("securitySchemes", {}))
    return referenced, defined


@dataclass(frozen=True)
class GatewayRouteInventoryEntry:
    path: str
    route_type: str
    is_api_route: bool
    is_mount: bool


def _write_resource_profiles(tmp_path: Path) -> Path:
    path = tmp_path / "resource_profiles.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "resource_profiles": {
                    "default": {
                        "partition": "compute",
                        "nodes": 1,
                        "ntasks": 1,
                        "cpus_per_task": 8,
                        "memory_gb": 16,
                        "walltime": "01:00:00",
                        "max_concurrent": 2,
                        "shud_threads": 8,
                    },
                    "overrides": {},
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _real_gateway(tmp_path: Path) -> RealSlurmGateway:
    return RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
        )
    )


def _route_inventory(app: object) -> set[GatewayRouteInventoryEntry]:
    entries: set[GatewayRouteInventoryEntry] = set()
    for route in _iter_routes(app):
        route_type = type(route).__name__
        is_api_route = isinstance(route, APIRoute)
        is_mount = isinstance(route, Mount)
        for attribute in ("path", "path_format"):
            path = getattr(route, attribute, None)
            if isinstance(path, str) and path:
                entries.add(
                    GatewayRouteInventoryEntry(
                        path=path,
                        route_type=route_type,
                        is_api_route=is_api_route,
                        is_mount=is_mount,
                    )
                )
    return entries


def _iter_routes(app_or_router: object) -> Iterable[object]:
    for route in getattr(app_or_router, "routes", []):
        yield route
        included_router = getattr(route, "original_router", None)
        if included_router is None:
            include_context = getattr(route, "include_context", None)
            included_router = getattr(include_context, "included_router", None)
        if included_router is not None:
            yield from _iter_routes(included_router)


def _route_paths(app: object) -> set[str]:
    return {entry.path for entry in _route_inventory(app)}


def _is_slurm_gateway_route_path(path: str) -> bool:
    return path == "/api/v1/slurm" or path.startswith("/api/v1/slurm/")


def _is_allowed_gateway_route_path(path: str) -> bool:
    return path == "/health" or _is_slurm_gateway_route_path(path) or path in FRAMEWORK_ROUTE_PATHS


def _is_allowed_gateway_route_entry(entry: GatewayRouteInventoryEntry) -> bool:
    if entry.is_mount:
        return False
    if entry.path in FRAMEWORK_ROUTE_PATHS:
        return True
    return entry.is_api_route and (
        entry.path == "/health" or _is_slurm_gateway_route_path(entry.path)
    )


def _forbidden_gateway_route_entries(
    entries: set[GatewayRouteInventoryEntry],
) -> list[GatewayRouteInventoryEntry]:
    return sorted(
        (entry for entry in entries if not _is_allowed_gateway_route_entry(entry)),
        key=lambda entry: (entry.path, entry.route_type),
    )


def test_health_probes_sbatch_squeue_sacct_scancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = _real_gateway(tmp_path)
    present = {"sbatch", "squeue", "sacct", "scancel"}

    def which(command: str) -> str | None:
        return f"/usr/bin/{command}" if Path(command).name in present else None

    def fake_run(command, **kwargs):
        del kwargs
        name = Path(command[0]).name
        if name in present:
            return subprocess.CompletedProcess(command, 0, stdout="slurm 24.05.1\n", stderr="")
        raise FileNotFoundError(name)

    monkeypatch.setattr("services.slurm_gateway.real_backend.shutil.which", which)
    monkeypatch.setattr(subprocess, "run", fake_run)

    healthy = gateway.health()
    assert set(healthy.binaries) == set(SLURM_HEALTH_BINARIES)
    assert healthy.healthy is True
    assert healthy.status == "healthy"
    for probe in healthy.binaries.values():
        assert probe.resolved is True
        assert probe.executable is True

    # Drop one binary -> overall unhealthy, that probe reports not executable.
    present.discard("sacct")
    unhealthy = gateway.health()
    assert unhealthy.healthy is False
    assert unhealthy.status == "unhealthy"
    assert unhealthy.binaries["sacct"].executable is False
    assert unhealthy.binaries["sbatch"].executable is True
    assert unhealthy.error


def test_mock_real_health_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = _real_gateway(tmp_path)
    mock = MockSlurmGateway(SlurmGatewaySettings(backend="mock"))

    def which(command: str) -> str:
        return f"/usr/bin/{Path(command).name}"

    def fake_run(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(command, 0, stdout="slurm 24.05.1\n", stderr="")

    monkeypatch.setattr("services.slurm_gateway.real_backend.shutil.which", which)
    monkeypatch.setattr(subprocess, "run", fake_run)

    real_payload = real.health().model_dump()
    mock_payload = mock.health().model_dump()

    assert real_payload.keys() == mock_payload.keys()
    assert set(real_payload["binaries"]) == set(mock_payload["binaries"]) == set(SLURM_HEALTH_BINARIES)
    for name in SLURM_HEALTH_BINARIES:
        assert real_payload["binaries"][name].keys() == mock_payload["binaries"][name].keys()


def test_mock_health_can_inject_unhealthy() -> None:
    mock = MockSlurmGateway(SlurmGatewaySettings(backend="mock", mock_missing_binaries=["squeue"]))
    response = mock.health()
    assert response.healthy is False
    assert response.status == "unhealthy"
    assert response.binaries["squeue"].executable is False
    assert response.binaries["sbatch"].executable is True


def test_gateway_app_exposes_only_slurm_routes() -> None:
    app = create_gateway_app(SlurmGatewaySettings(backend="mock"))
    inventory = _route_inventory(app)
    paths = {entry.path for entry in inventory}
    assert paths, "gateway app must expose at least one route"
    assert "/api/v1/slurm/health" in paths
    assert any(_is_slurm_gateway_route_path(path) for path in paths)
    for entry in inventory:
        assert _is_allowed_gateway_route_entry(entry), (
            f"unexpected route: {entry.path} ({entry.route_type})"
        )
    joined = " ".join(paths)
    for marker in BUSINESS_MARKERS:
        assert marker not in joined, f"business route leaked: {marker}"


def test_gateway_route_scope_rejects_slurm_sibling_prefixes() -> None:
    assert _is_allowed_gateway_route_path("/api/v1/slurm")
    assert _is_allowed_gateway_route_path("/api/v1/slurm/jobs")
    assert not _is_allowed_gateway_route_path("/api/v1/slurmish")
    assert not _is_allowed_gateway_route_path("/api/v1/slurm-admin")


def test_gateway_route_inventory_includes_mounts() -> None:
    app = create_gateway_app(SlurmGatewaySettings(backend="mock"))
    app.mount("/frontend", _NoopAsgiApp(), name="frontend")
    inventory = _route_inventory(app)
    paths = {entry.path for entry in inventory}

    assert "/frontend" in paths
    assert "/frontend" in {entry.path for entry in _forbidden_gateway_route_entries(inventory)}


def test_gateway_route_inventory_rejects_same_namespace_mounts() -> None:
    app = create_gateway_app(SlurmGatewaySettings(backend="mock"))
    app.mount("/api/v1/slurm/extra", _NoopAsgiApp(), name="same-namespace")
    inventory = _route_inventory(app)

    forbidden_mounts = [
        entry
        for entry in _forbidden_gateway_route_entries(inventory)
        if entry.path == "/api/v1/slurm/extra"
    ]
    assert forbidden_mounts
    assert all(entry.is_mount for entry in forbidden_mounts)


class _NoopAsgiApp:
    async def __call__(self, scope: object, receive: object, send: object) -> None:
        del scope, receive, send


def test_internal_reset_disabled_by_default() -> None:
    app = create_gateway_app(SlurmGatewaySettings(backend="mock"))
    assert INTERNAL_RESET_PATH not in _route_paths(app)

    enabled = create_gateway_app(SlurmGatewaySettings(backend="mock", allow_internal_reset=True))
    assert INTERNAL_RESET_PATH in _route_paths(enabled)


def test_service_role_slurm_gateway_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    # The standalone bounded gateway app builds regardless of NHMS_SERVICE_ROLE,
    # including the formerly-reserved slurm_gateway role.
    monkeypatch.setenv("NHMS_SERVICE_ROLE", "slurm_gateway")
    monkeypatch.setenv("NHMS_REQUIRE_SERVICE_ROLE", "true")
    app = create_gateway_app(SlurmGatewaySettings(backend="mock"))
    assert "/api/v1/slurm/health" in _route_paths(app)


def test_standalone_enabled_schema_defines_every_referenced_scheme() -> None:
    # Every operation-level security requirement must be defined in
    # components.securitySchemes; a referenced-but-undefined scheme is a
    # dangling OpenAPI ref (issue #1684 regression). The standalone app
    # publishes the full six-scheme set from the shared owner.
    app = create_gateway_app(SlurmGatewaySettings(backend="mock", allow_internal_reset=True))
    spec = app.openapi()
    referenced, defined = _collect_referenced_and_defined(spec)

    assert referenced <= defined
    assert defined == set(SECURITY_SCHEME_NAMES)


def test_standalone_enabled_schema_exact_six_schemes_from_shared_owner() -> None:
    app = create_gateway_app(SlurmGatewaySettings(backend="mock", allow_internal_reset=True))
    schemes = app.openapi()["components"]["securitySchemes"]

    assert set(schemes) == set(SECURITY_SCHEME_NAMES)
    assert schemes == security_scheme_definitions()


def test_standalone_enabled_schema_mutations_have_exact_bearer_first_lists() -> None:
    from packages.common.openapi_auth_security import slurm_mutation_security_alternatives

    app = create_gateway_app(SlurmGatewaySettings(backend="mock", allow_internal_reset=True))
    spec = app.openapi()

    expected = slurm_mutation_security_alternatives()
    for method, path in sorted(SLURM_MUTATION_PATHS):
        operation = spec["paths"][path][method.lower()]
        assert operation["security"] == expected, (method, path)


def test_standalone_enabled_schema_read_routes_have_no_security() -> None:
    app = create_gateway_app(SlurmGatewaySettings(backend="mock", allow_internal_reset=True))
    spec = app.openapi()

    for method, path in sorted(SLURM_READ_PATHS):
        operation = spec["paths"][path][method.lower()]
        assert "security" not in operation, (method, path)


def test_standalone_enabled_schema_embeds_no_credential_values() -> None:
    app = create_gateway_app(SlurmGatewaySettings(backend="mock", allow_internal_reset=True))
    serialized = json.dumps(app.openapi())

    for credential_fragment in ("slurm-gateway-service-token", "proof-token", "Bearer secret", "dev-test:"):
        assert credential_fragment not in serialized


def test_standalone_disabled_schema_omits_reset_and_keeps_no_dangling_refs() -> None:
    # With reset disabled the route is absent (404) and the document must still
    # define every scheme its remaining operations reference.
    app = create_gateway_app(SlurmGatewaySettings(backend="mock"))
    spec = app.openapi()

    assert "post" not in spec["paths"].get("/api/v1/slurm/internal/reset", {})
    referenced, defined = _collect_referenced_and_defined(spec)
    assert referenced <= defined
    assert defined == set(SECURITY_SCHEME_NAMES)


def test_standalone_disabled_schema_publishes_all_six_schemes() -> None:
    # Intentional behavior: the disabled-reset document still publishes all six
    # scheme definitions (harmless, runbook-clean, and future-proof against
    # flipping the flag without regenerating the scheme set).
    app = create_gateway_app(SlurmGatewaySettings(backend="mock"))
    schemes = app.openapi()["components"]["securitySchemes"]

    assert set(schemes) == set(SECURITY_SCHEME_NAMES)
    assert schemes == security_scheme_definitions()
