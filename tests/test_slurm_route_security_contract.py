"""Secrets-redaction contract for the Slurm route validation boundary.

Owns the request-validation redaction proofs for the four Slurm mutations:
secret manifest fields/keys, secret template mappings, secret job-type values,
and safe validation-error shapes must never echo the secret payload. The
manifest/resource-profile/environment contract stays in
``tests/test_slurm_route_contract.py``, which exports the shared
``_client``/``_array_task``/``_write_template``/``_write_resource_profiles``
helpers this module imports.
"""

from __future__ import annotations

import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.slurm_gateway import routes as slurm_routes
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES, SlurmGatewaySettings
from services.slurm_gateway.real_backend import RealSlurmGateway
from tests.test_slurm_route_contract import (
    SERVICE_BEARER,
    _array_task,
    _client,
    _write_resource_profiles,
    _write_template,
)


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
    monkeypatch.setenv("SLURM_GATEWAY_SERVICE_TOKEN", "route-contract-service-token-0123456789")
    monkeypatch.setattr(slurm_routes, "slurm_gateway", GatewayShouldNotBeCalled())

    with TestClient(app, headers=SERVICE_BEARER) as client:
        response = client.post("/api/v1/slurm/job-arrays", json=payload)

    assert response.status_code == 422
