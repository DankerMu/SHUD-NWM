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


SERVICE_BEARER = {"Authorization": "Bearer route-contract-service-token-0123456789"}


def _client(monkeypatch: pytest.MonkeyPatch, gateway: RealSlurmGateway) -> TestClient:
    app = FastAPI()
    app.include_router(slurm_routes.router)
    monkeypatch.setenv("SLURM_GATEWAY_SERVICE_TOKEN", "route-contract-service-token-0123456789")
    monkeypatch.setattr(slurm_routes, "slurm_gateway", gateway)
    return TestClient(app, headers=SERVICE_BEARER)


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


def _assert_no_secret_validation_echo(response_text: str, forbidden: list[str]) -> None:
    for value in forbidden:
        assert value not in response_text
    assert '"input"' not in response_text
    assert "rejected_value" not in response_text


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
            job_type_templates={"run_shud_analysis": "contract.sbatch"},
        )
    )
    captured = _capture_sbatch(monkeypatch)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/jobs",
            json={
                "run_id": "run_top",
                "model_id": "model_top",
                "job_type": "run_shud_analysis",
                "manifest": {
                    "run_id": "run_nested",
                    "model_id": "model_nested",
                    "job_type": "hindcast",
                    "extra_value": "manifest_value",
                    "metadata": {"callback_uri": "https://example.com/notify", "safe_key": "safe/value"},
                },
            },
        )

    assert response.status_code == 201
    assert response.json()["manifest"]["metadata"] == {
        "callback_uri": "https://example.com/notify",
        "safe_key": "safe/value",
    }
    assert 'echo "run=run_top model=model_top job=run_shud_analysis extra=manifest_value"' in captured["script"]
    assert "run_nested" not in captured["script"]
    assert "model_nested" not in captured["script"]
    assert "job=hindcast" not in captured["script"]


def test_single_job_rejects_array_capable_job_type_before_sbatch(monkeypatch, tmp_path):
    template_dir = _write_template(
        tmp_path,
        """
#!/usr/bin/env bash
#SBATCH --partition={{partition}}
echo "job={{job_type}}"
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

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for array-capable single submit")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/jobs",
            json={"run_id": "run_001", "model_id": "model_001", "job_type": "run_shud_forecast_array"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


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
                    "published_artifact_root": "/published",
                    "published_artifact_uri_prefix": "published://",
                },
            },
        )

    assert response.status_code == 201
    assert "export OBJECT_STORE_ROOT=/durable/object-store" in captured["script"]
    assert "export OBJECT_STORE_PREFIX=forecast/cycle_001" in captured["script"]
    assert "export NHMS_PUBLISHED_ARTIFACT_ROOT=/published" in captured["script"]
    assert "export NHMS_PUBLISHED_ARTIFACT_URI_PREFIX=published://" in captured["script"]


def test_retired_download_source_cycle_route_is_not_in_default_templates(monkeypatch, tmp_path):
    workspace_root = tmp_path / "workspace"
    object_store_root = tmp_path / "object-store"
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(workspace_root),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        )
    )
    captured = _capture_sbatch(monkeypatch)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/jobs",
            json={
                "run_id": "cycle_ifs_2026053106",
                "model_id": "model_a",
                "job_type": "download_source_cycle",
                "manifest": {
                    "run_id": "cycle_ifs_2026053106",
                    "model_id": "model_a",
                    "cycle_id": "ifs_2026053106",
                    "job_type": "download_source_cycle",
                    "stage": "download",
                    "source_id": "ifs",
                    "cycle_time": "2026053106",
                    "workspace_dir": str(workspace_root),
                    "object_store_root": str(object_store_root),
                    "object_store_prefix": "s3://nhms-prod",
                    "pipeline_job_id": "cycle_ifs_2026053106_retry_active",
                    "retry_count": 2,
                    "manual_retry_marker": True,
                },
            },
        )

    assert response.status_code == 404
    assert "download_source_cycle" not in DEFAULT_JOB_TYPE_TEMPLATES
    assert "script" not in captured


def test_route_object_store_prefix_quote_breakout_is_shell_quoted(monkeypatch, tmp_path):
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
                    "object_store_prefix": 'prod" PYTHONPATH=/tmp/evil #',
                },
            },
        )

    assert response.status_code == 201
    assert 'export OBJECT_STORE_PREFIX="prod" PYTHONPATH=/tmp/evil #' not in captured["script"]
    assert 'export OBJECT_STORE_PREFIX=\'prod" PYTHONPATH=/tmp/evil #\'' in captured["script"]


@pytest.mark.parametrize(
    "resource_profiles",
    [
        """
resource_profiles:
  default:
    partition: compute --account=vip
    nodes: 1
    ntasks: 1
    cpus_per_task: 8
    memory_gb: 32
    walltime: "01:00:00"
    max_concurrent: 2
    shud_threads: 8
  overrides: {}
""",
        """
resource_profiles:
  default:
    partition: compute
    account: "friends --qos=high"
    nodes: 1
    ntasks: 1
    cpus_per_task: 8
    memory_gb: 32
    walltime: "01:00:00"
    max_concurrent: 2
    shud_threads: 8
  overrides: {}
""",
    ],
)
def test_array_submit_route_rejects_resource_profile_injection_before_manifest_or_sbatch(
    monkeypatch,
    tmp_path,
    resource_profiles,
):
    profiles_path = tmp_path / "resource_profiles.yaml"
    profiles_path.write_text(resource_profiles.lstrip(), encoding="utf-8")
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(profiles_path),
            workspace_dir=str(tmp_path / "workspace"),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        )
    )

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for invalid resource profiles")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/job-arrays",
            json={
                "job_type": "run_shud_forecast_array",
                "cycle_id": "cycle_001",
                "stage_name": "forecast",
                "tasks": [_array_task()],
                "manifest": {"workspace_dir": str(tmp_path / "workspace")},
            },
        )

    response_text = response.text
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "CONFIGURATION_ERROR"
    assert "--account=vip" not in response_text
    assert "--qos=high" not in response_text
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


@pytest.mark.parametrize(
    "resource_profiles",
    [
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
    run_id: profile_run
  overrides: {}
""",
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
    manifest_index_path: /tmp/profile-index.json
  overrides: {}
""",
    ],
)
def test_array_submit_route_rejects_resource_profile_context_collision_before_manifest_or_sbatch(
    monkeypatch,
    tmp_path,
    resource_profiles,
):
    profiles_path = tmp_path / "resource_profiles.yaml"
    profiles_path.write_text(resource_profiles.lstrip(), encoding="utf-8")
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(profiles_path),
            workspace_dir=str(tmp_path / "workspace"),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        )
    )

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for invalid resource profiles")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/job-arrays",
            json={
                "job_type": "run_shud_forecast_array",
                "cycle_id": "cycle_001",
                "stage_name": "forecast",
                "tasks": [_array_task()],
                "manifest": {"workspace_dir": str(tmp_path / "workspace")},
            },
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "CONFIGURATION_ERROR"
    assert "profile_run" not in response.text
    assert "/tmp/profile-index.json" not in response.text
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


def test_single_submit_route_rejects_resource_profile_context_collision_before_sbatch(monkeypatch, tmp_path):
    profiles_path = tmp_path / "resource_profiles.yaml"
    profiles_path.write_text(
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
    workspace_dir: /tmp/profile-workspace
  overrides: {}
""".lstrip(),
        encoding="utf-8",
    )
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(_write_template(tmp_path, "#!/usr/bin/env bash\n")),
            resource_profiles_path=str(profiles_path),
            workspace_dir=str(tmp_path / "workspace"),
            job_type_templates={"run_shud_analysis": "contract.sbatch"},
        )
    )

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for invalid resource profiles")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/jobs",
            json={
                "run_id": "manifest_run",
                "model_id": "model_001",
                "job_type": "run_shud_analysis",
                "manifest": {"workspace_dir": str(tmp_path / "workspace")},
            },
        )

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "CONFIGURATION_ERROR"
    assert "/tmp/profile-workspace" not in response.text


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


def test_malformed_single_submit_request_redacts_prehandler_validation_input(monkeypatch):
    class GatewayShouldNotBeCalled:
        def submit_job(self, request):
            del request
            raise AssertionError("gateway must not be called when request validation fails")

    app = FastAPI()
    app.include_router(slurm_routes.router)
    monkeypatch.setenv("SLURM_GATEWAY_SERVICE_TOKEN", "route-contract-service-token-0123456789")
    monkeypatch.setattr(slurm_routes, "slurm_gateway", GatewayShouldNotBeCalled())

    raw_url = "https://user:supersecret@example.com/run?token=secret-token"
    with TestClient(app, headers=SERVICE_BEARER) as client:
        response = client.post(
            "/api/v1/slurm/jobs",
            json={
                "run_id": "run_001",
                "model_id": "model_001",
                "job_type": {"selector": raw_url, "api_key": "secret-value"},
            },
        )

    body = response.json()
    assert response.status_code == 422
    assert body["status"] == "error"
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "validation_errors" in body["error"]["details"]
    _assert_no_secret_validation_echo(
        response.text,
        [
            raw_url,
            "user:supersecret",
            "token=secret-token",
            "secret-value",
            "api_key",
        ],
    )


def test_malformed_array_submit_request_redacts_prehandler_validation_key_and_input(monkeypatch):
    class GatewayShouldNotBeCalled:
        def submit_job_array(self, request):
            del request
            raise AssertionError("gateway must not be called when request validation fails")

    app = FastAPI()
    app.include_router(slurm_routes.router)
    monkeypatch.setenv("SLURM_GATEWAY_SERVICE_TOKEN", "route-contract-service-token-0123456789")
    monkeypatch.setattr(slurm_routes, "slurm_gateway", GatewayShouldNotBeCalled())

    raw_key = "https://user:supersecret@example.com/selector?token=secret-token"
    with TestClient(app, headers=SERVICE_BEARER) as client:
        response = client.post(
            "/api/v1/slurm/job-arrays",
            json={
                raw_key: "secret-value",
                "cycle_id": "cycle_001",
                "stage_name": "forecast",
                "tasks": [],
            },
        )

    body = response.json()
    assert response.status_code == 422
    assert body["status"] == "error"
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["validation_errors"][0]["field"] == "body.job_type"
    _assert_no_secret_validation_echo(
        response.text,
        [
            raw_key,
            "user:supersecret",
            "token=secret-token",
            "secret-value",
        ],
    )


def test_slurm_query_validation_error_uses_safe_shape(monkeypatch):
    class GatewayShouldNotBeCalled:
        def list_jobs(self, *, limit: int, offset: int):
            del limit, offset
            raise AssertionError("gateway must not be called when query validation fails")

    app = FastAPI()
    app.include_router(slurm_routes.router)
    monkeypatch.setattr(slurm_routes, "slurm_gateway", GatewayShouldNotBeCalled())

    with TestClient(app) as client:
        response = client.get("/api/v1/slurm/jobs?limit=0&offset=-1")

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert {detail["field"] for detail in body["error"]["details"]["validation_errors"]} == {
        "query.limit",
        "query.offset",
    }
    assert '"input"' not in response.text
    assert "rejected_value" not in response.text


def test_slurm_path_validation_error_uses_safe_shape(monkeypatch):
    class GatewayShouldNotBeCalled:
        def get_job_status(self, job_id: str):
            del job_id
            raise AssertionError("gateway must not be called when path validation fails")

    app = FastAPI()
    app.include_router(slurm_routes.router)
    monkeypatch.setattr(slurm_routes, "slurm_gateway", GatewayShouldNotBeCalled())

    with TestClient(app) as client:
        response = client.get("/api/v1/slurm/jobs/not-a-number")

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"]["validation_errors"][0]["field"] == "path.job_id"
    assert "not-a-number" not in response.text
    assert '"input"' not in response.text
    assert "rejected_value" not in response.text


@pytest.mark.parametrize(
    "slurm_env",
    [
        {"NHMS_MANIFEST_INDEX": "/tmp/evil.json"},
        {"WORKSPACE_ROOT": "/tmp/evil-workspace"},
        {"SHUD_THREADS": "1"},
        {"SLURM_ARRAY_TASK_ID": "99"},
    ],
)
def test_single_submit_route_rejects_reserved_slurm_env_before_sbatch(monkeypatch, tmp_path, slurm_env):
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(_write_template(tmp_path, "#!/usr/bin/env bash\n")),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
            job_type_templates={"run_shud_analysis": "contract.sbatch"},
        )
    )

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for reserved slurm_env")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/jobs",
            json={
                "run_id": "run_001",
                "model_id": "model_001",
                "job_type": "run_shud_analysis",
                "manifest": {"slurm_env": slurm_env},
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MANIFEST_VALIDATION_ERROR"


@pytest.mark.parametrize(
    "slurm_env",
    [
        {"NHMS_MANIFEST_INDEX": "/tmp/evil.json"},
        {"OBJECT_STORE_ROOT": "/tmp/evil-objects"},
        {"OMP_NUM_THREADS": "1"},
        {"SLURM_ARRAY_TASK_ID": "99"},
    ],
)
def test_array_submit_route_rejects_reserved_slurm_env_before_manifest_or_sbatch(monkeypatch, tmp_path, slurm_env):
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        )
    )

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for reserved slurm_env")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/job-arrays",
            json={
                "job_type": "run_shud_forecast_array",
                "cycle_id": "cycle_001",
                "stage_name": "forecast",
                "tasks": [_array_task()],
                "manifest": {"workspace_dir": str(tmp_path / "workspace"), "slurm_env": slurm_env},
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MANIFEST_VALIDATION_ERROR"
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))
