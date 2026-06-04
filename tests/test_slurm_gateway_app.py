"""Lane 1 gateway-core tests: standalone bounded app, 4-binary health, parity."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi.routing import APIRoute

from services.slurm_gateway.app import INTERNAL_RESET_PATH, create_gateway_app
from services.slurm_gateway.config import SlurmGatewaySettings
from services.slurm_gateway.mock_backend import MockSlurmGateway
from services.slurm_gateway.models import SLURM_HEALTH_BINARIES
from services.slurm_gateway.real_backend import RealSlurmGateway

ALLOWED_PREFIXES = ("/health", "/api/v1/slurm")
BUSINESS_MARKERS = ("forecast", "model", "pipeline", "hindcast", "flood", "data-source")


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


def _route_paths(app) -> set[str]:
    return {route.path for route in app.routes if isinstance(route, APIRoute)}


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
    paths = _route_paths(app)
    assert paths, "gateway app must expose at least one route"
    assert "/api/v1/slurm/health" in paths
    assert any(path.startswith("/api/v1/slurm") for path in paths)
    for path in paths:
        assert path.startswith(ALLOWED_PREFIXES), f"unexpected route: {path}"
    joined = " ".join(paths)
    for marker in BUSINESS_MARKERS:
        assert marker not in joined, f"business route leaked: {marker}"


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
