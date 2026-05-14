from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.slurm_gateway import routes as slurm_routes
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES, SlurmGatewaySettings
from services.slurm_gateway.real_backend import RealSlurmGateway


def _write_resource_profiles(tmp_path: Path) -> Path:
    path = tmp_path / "resource_profiles.yaml"
    path.write_text(
        """
resource_profiles:
  default:
    partition: compute
    nodes: 1
    ntasks: 1
    cpus_per_task: 8
    memory_gb: 32
    walltime: "01:00:00"
    max_concurrent: 2
    shud_threads: 8
  overrides: {}
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _write_template(tmp_path: Path, content: str) -> Path:
    template_dir = tmp_path / "sbatch"
    template_dir.mkdir()
    (template_dir / "contract.sbatch").write_text(content, encoding="utf-8")
    return template_dir


def _client(monkeypatch: pytest.MonkeyPatch, gateway: RealSlurmGateway) -> TestClient:
    app = FastAPI()
    app.include_router(slurm_routes.router)
    monkeypatch.setattr(slurm_routes, "slurm_gateway", gateway)
    return TestClient(app)


def _array_task() -> dict[str, str]:
    return {
        "model_id": "model_task",
        "basin_version_id": "basin_001",
        "river_network_version_id": "river_001",
        "run_id": "run_task",
        "source_id": "GFS",
        "cycle_time": "2026051200",
    }


def _capture_sbatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        captured["script"] = Path(command[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return captured


def test_single_job_manifest_survives_route_boundary(monkeypatch, tmp_path):
    template_dir = _write_template(
        tmp_path,
        """
#!/usr/bin/env bash
#SBATCH --partition={{partition}}
echo "run={{run_id}} model={{model_id}} job={{job_type}} extra={{extra_value}}"
""".lstrip(),
    )
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(template_dir),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
            job_type_templates={"run_shud_forecast_array": "contract.sbatch"},
        )
    )
    captured = _capture_sbatch(monkeypatch)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/jobs",
            json={
                "run_id": "run_top",
                "model_id": "model_top",
                "job_type": "run_shud_forecast_array",
                "manifest": {
                    "run_id": "run_nested",
                    "model_id": "model_nested",
                    "job_type": "hindcast",
                    "extra_value": "manifest_value",
                },
            },
        )

    assert response.status_code == 201
    assert 'echo "run=run_top model=model_top job=run_shud_forecast_array extra=manifest_value"' in captured["script"]
    assert "run_nested" not in captured["script"]
    assert "model_nested" not in captured["script"]
    assert "job=hindcast" not in captured["script"]


def test_array_job_manifest_survives_route_boundary(monkeypatch, tmp_path):
    template_dir = _write_template(
        tmp_path,
        """
#!/usr/bin/env bash
#SBATCH --partition={{partition}}
echo "job={{job_type}} cycle={{cycle_id}} stage={{stage_name}} tasks={{tasks | length}} root={{object_store_root}}"
""".lstrip(),
    )
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(template_dir),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
            job_type_templates={"run_shud_forecast_array": "contract.sbatch"},
        )
    )
    captured = _capture_sbatch(monkeypatch)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/job-arrays",
            json={
                "job_type": "run_shud_forecast_array",
                "cycle_id": "cycle_top",
                "stage_name": "stage_top",
                "tasks": [_array_task()],
                "manifest": {
                    "job_type": "hindcast",
                    "cycle_id": "cycle_nested",
                    "stage_name": "stage_nested",
                    "object_store_root": "/objects/nhms",
                },
            },
        )

    assert response.status_code == 201
    assert 'echo "job=run_shud_forecast_array cycle=cycle_top stage=stage_top tasks=1 root=/objects/nhms"' in captured[
        "script"
    ]
    assert "hindcast" not in captured["script"]
    assert "cycle_nested" not in captured["script"]
    assert "stage_nested" not in captured["script"]


def test_object_store_roots_exported_to_template(monkeypatch, tmp_path):
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        )
    )
    captured = _capture_sbatch(monkeypatch)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/job-arrays",
            json={
                "job_type": "run_shud_forecast_array",
                "cycle_id": "cycle_001",
                "stage_name": "run_shud_forecast_array",
                "tasks": [_array_task()],
                "manifest": {
                    "object_store_root": "/durable/object-store",
                    "object_store_prefix": "forecast/cycle_001",
                },
            },
        )

    assert response.status_code == 201
    assert 'export OBJECT_STORE_ROOT="/durable/object-store"' in captured["script"]
    assert 'export OBJECT_STORE_PREFIX="forecast/cycle_001"' in captured["script"]


def test_single_submit_missing_job_type_returns_validation_error_without_sbatch(monkeypatch, tmp_path):
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(_write_template(tmp_path, "#!/usr/bin/env bash\n")),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
            job_type_templates={"run_shud_forecast_array": "contract.sbatch"},
        )
    )

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called when job_type is missing")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post("/api/v1/slurm/jobs", json={"run_id": "run_001", "model_id": "model_001"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MANIFEST_VALIDATION_ERROR"


@pytest.mark.parametrize("payload", [{"cycle_id": "cycle_001", "tasks": []}, {"job_type": "run_shud_forecast_array"}])
def test_array_submit_required_fields_validated_before_gateway(monkeypatch, payload):
    class GatewayShouldNotBeCalled:
        def submit_job_array(self, request):
            del request
            raise AssertionError("gateway must not be called when request validation fails")

    app = FastAPI()
    app.include_router(slurm_routes.router)
    monkeypatch.setattr(slurm_routes, "slurm_gateway", GatewayShouldNotBeCalled())

    with TestClient(app) as client:
        response = client.post("/api/v1/slurm/job-arrays", json=payload)

    assert response.status_code == 422
