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
                },
            },
        )

    assert response.status_code == 201
    assert "export OBJECT_STORE_ROOT=/durable/object-store" in captured["script"]
    assert "export OBJECT_STORE_PREFIX=forecast/cycle_001" in captured["script"]


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
    monkeypatch.setattr(slurm_routes, "slurm_gateway", GatewayShouldNotBeCalled())

    raw_url = "https://user:supersecret@example.com/run?token=secret-token"
    with TestClient(app) as client:
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
    monkeypatch.setattr(slurm_routes, "slurm_gateway", GatewayShouldNotBeCalled())

    raw_key = "https://user:supersecret@example.com/selector?token=secret-token"
    with TestClient(app) as client:
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


@pytest.mark.parametrize(
    "manifest_update",
    [
        {"DATABASE_URL": "postgresql://nhms:supersecret@db.prod.example/nhms"},
        {"database_uri": "postgresql://nhms@db.prod.example/nhms"},
        {"metadata": {"callback_uri": "https://user:supersecret@example.com/notify"}},
        {"output_uri": "s3://bucket/prod?token=supersecret"},
    ],
)
def test_single_submit_route_rejects_secret_manifest_fields_before_sbatch(monkeypatch, tmp_path, manifest_update):
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
        raise AssertionError("subprocess.run must not be called for secret manifest fields")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/jobs",
            json={
                "run_id": "run_001",
                "model_id": "model_001",
                "job_type": "run_shud_analysis",
                "manifest": manifest_update,
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MANIFEST_VALIDATION_ERROR"
    assert "supersecret" not in response.text


@pytest.mark.parametrize(
    "secret_template",
    [
        "s3://bucket/template.sbatch?token=supersecret",
        "https://user:supersecret@example.com/template.sbatch",
    ],
)
def test_single_submit_route_rejects_secret_template_mapping_before_sbatch(
    monkeypatch,
    tmp_path,
    secret_template,
):
    template_dir = _write_template(tmp_path, "#!/usr/bin/env bash\n")
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(template_dir),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        )
    )

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for secret template mappings")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/jobs",
            json={
                "run_id": "run_001",
                "model_id": "model_001",
                "job_type": "download_source_cycle",
                "manifest": {
                    "slurm_job_type_templates": {
                        **DEFAULT_JOB_TYPE_TEMPLATES,
                        "download_source_cycle": secret_template,
                    }
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MANIFEST_VALIDATION_ERROR"
    assert secret_template not in response.text
    assert "supersecret" not in response.text


@pytest.mark.parametrize(
    "secret_job_type",
    [
        "s3://bucket/template.sbatch?token=supersecret",
        "https://user:supersecret@example.com/template.sbatch",
    ],
)
def test_single_submit_route_rejects_secret_top_level_job_type_before_template_lookup_or_sbatch(
    monkeypatch,
    tmp_path,
    secret_job_type,
):
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
        raise AssertionError("subprocess.run must not be called for secret job_type")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/jobs",
            json={
                "run_id": "run_001",
                "model_id": "model_001",
                "job_type": secret_job_type,
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MANIFEST_VALIDATION_ERROR"
    assert secret_job_type not in response.text
    assert "supersecret" not in response.text


@pytest.mark.parametrize(
    ("manifest_update", "secret_text"),
    [
        (
            {"https://user:supersecret@example.com/callback": "notify"},
            "https://user:supersecret@example.com/callback",
        ),
        (
            {"metadata": {"s3://bucket/path?token=supersecret": "signed"}},
            "s3://bucket/path?token=supersecret",
        ),
        ({"metadata": {"database_dsn": "postgresql://nhms@db.prod.example/nhms"}}, "database_dsn"),
    ],
)
def test_single_submit_route_rejects_secret_manifest_keys_without_raw_response(
    monkeypatch,
    tmp_path,
    manifest_update,
    secret_text,
):
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
        raise AssertionError("subprocess.run must not be called for secret manifest keys")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/jobs",
            json={
                "run_id": "run_001",
                "model_id": "model_001",
                "job_type": "run_shud_analysis",
                "manifest": manifest_update,
            },
        )

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "MANIFEST_VALIDATION_ERROR"
    assert body["error"]["details"]["findings"][0]["field"].endswith("[redacted]")
    assert secret_text not in response.text
    assert "supersecret" not in response.text


@pytest.mark.parametrize(
    "manifest_update",
    [
        {"DATABASE_URL": "postgresql://nhms:supersecret@db.prod.example/nhms"},
        {"database_dsn": "postgresql://nhms@db.prod.example/nhms"},
        {"metadata": {"callback_uri": "https://user:supersecret@example.com/notify"}},
        {"object_store_root": "s3://bucket/prod?password=supersecret"},
    ],
)
def test_array_submit_route_rejects_secret_manifest_fields_before_manifest_or_sbatch(
    monkeypatch,
    tmp_path,
    manifest_update,
):
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
        raise AssertionError("subprocess.run must not be called for secret manifest fields")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/job-arrays",
            json={
                "job_type": "run_shud_forecast_array",
                "cycle_id": "cycle_001",
                "stage_name": "forecast",
                "tasks": [_array_task()],
                "manifest": {"workspace_dir": str(tmp_path / "workspace"), **manifest_update},
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MANIFEST_VALIDATION_ERROR"
    assert "supersecret" not in response.text
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


@pytest.mark.parametrize(
    ("manifest_update", "secret_text"),
    [
        (
            {"https://user:supersecret@example.com/callback": "notify"},
            "https://user:supersecret@example.com/callback",
        ),
        (
            {"metadata": {"s3://bucket/path?token=supersecret": "signed"}},
            "s3://bucket/path?token=supersecret",
        ),
        ({"metadata": {"database_uri": "postgresql://nhms@db.prod.example/nhms"}}, "database_uri"),
    ],
)
def test_array_submit_route_rejects_secret_manifest_keys_before_manifest_or_sbatch(
    monkeypatch,
    tmp_path,
    manifest_update,
    secret_text,
):
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
        raise AssertionError("subprocess.run must not be called for secret manifest keys")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/job-arrays",
            json={
                "job_type": "run_shud_forecast_array",
                "cycle_id": "cycle_001",
                "stage_name": "forecast",
                "tasks": [_array_task()],
                "manifest": {"workspace_dir": str(tmp_path / "workspace"), **manifest_update},
            },
        )

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "MANIFEST_VALIDATION_ERROR"
    assert body["error"]["details"]["findings"][0]["field"].endswith("[redacted]")
    assert secret_text not in response.text
    assert "supersecret" not in response.text
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


@pytest.mark.parametrize(
    "secret_template",
    [
        "s3://bucket/template.sbatch?token=supersecret",
        "https://user:supersecret@example.com/template.sbatch",
    ],
)
def test_array_submit_route_rejects_secret_template_mapping_before_manifest_or_sbatch(
    monkeypatch,
    tmp_path,
    secret_template,
):
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
        raise AssertionError("subprocess.run must not be called for secret template mappings")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/job-arrays",
            json={
                "job_type": "run_shud_forecast_array",
                "cycle_id": "cycle_001",
                "stage_name": "forecast",
                "tasks": [_array_task()],
                "manifest": {
                    "workspace_dir": str(tmp_path / "workspace"),
                    "slurm_job_type_templates": {
                        **DEFAULT_JOB_TYPE_TEMPLATES,
                        "run_shud_forecast_array": secret_template,
                    },
                },
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "MANIFEST_VALIDATION_ERROR"
    assert secret_template not in response.text
    assert "supersecret" not in response.text
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


def test_array_submit_route_rejects_task_count_over_manifest_index_limit_before_manifest_or_sbatch(
    monkeypatch,
    tmp_path,
):
    from packages.common import manifest_index as manifest_index_module

    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        )
    )
    monkeypatch.setattr(manifest_index_module, "MAX_MANIFEST_INDEX_ENTRIES", 1)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for over-limit arrays")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with _client(monkeypatch, gateway) as client:
        response = client.post(
            "/api/v1/slurm/job-arrays",
            json={
                "job_type": "run_shud_forecast_array",
                "cycle_id": "cycle_001",
                "stage_name": "forecast",
                "tasks": [_array_task(), {**_array_task(), "run_id": "run_task_2", "model_id": "model_task_2"}],
                "manifest": {"workspace_dir": str(tmp_path / "workspace")},
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["details"]["entry_limit"] == 1
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


def test_array_submit_route_rejects_manifest_index_size_limit_before_manifest_or_sbatch(monkeypatch, tmp_path):
    from packages.common import manifest_index as manifest_index_module

    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            workspace_dir=str(tmp_path / "workspace"),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
        )
    )
    monkeypatch.setattr(manifest_index_module, "MAX_MANIFEST_INDEX_BYTES", 32)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for over-limit arrays")

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

    assert response.status_code == 422
    assert response.json()["error"]["details"]["size_limit"] == 32
    assert not list((tmp_path / "workspace").glob("cycle_001/manifests/*.json"))


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
