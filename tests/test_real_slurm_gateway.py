from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from jinja2.exceptions import SecurityError

from services.slurm_gateway.config import SlurmGatewaySettings
from services.slurm_gateway.gateway import (
    ManifestValidationError,
    SlurmTimeoutError,
    TemplateSecurityError,
    create_gateway,
)
from services.slurm_gateway.models import SlurmJobStatus, SubmitJobRequest
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
    cpus_per_task: 32
    memory_gb: 128
    walltime: "06:00:00"
    max_concurrent: 4
    shud_threads: 32
  overrides: {}
""".lstrip(),
        encoding="utf-8",
    )
    return path


def _write_template(tmp_path: Path, name: str = "run.sbatch", content: str | None = None) -> Path:
    template_dir = tmp_path / "sbatch"
    template_dir.mkdir()
    (template_dir / name).write_text(
        content
        or """
#!/usr/bin/env bash
#SBATCH --partition={{partition}}
#SBATCH --cpus-per-task={{cpus_per_task}}
echo "{{run_id}} {{model_id}} {{shud_threads}}"
""".lstrip(),
        encoding="utf-8",
    )
    return template_dir


def _gateway(tmp_path: Path, template_name: str = "run.sbatch") -> RealSlurmGateway:
    return RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(_write_template(tmp_path, template_name)),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            job_type_templates={"run_shud_forecast_array": template_name},
            workspace_dir=str(tmp_path / "workspace"),
        )
    )


def test_submit_job_parses_sbatch_stdout(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.submit_job(
        SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="run_shud_forecast_array")
    )

    assert record.job_id == "12345"
    assert record.status == SlurmJobStatus.SUBMITTED
    assert calls[0][0][0] == "sbatch"
    assert calls[0][1]["shell"] is False


@pytest.mark.parametrize(
    ("slurm_state", "expected"),
    [
        ("PENDING", SlurmJobStatus.SUBMITTED),
        ("REQUEUED", SlurmJobStatus.SUBMITTED),
        ("RUNNING", SlurmJobStatus.RUNNING),
        ("COMPLETED", SlurmJobStatus.SUCCEEDED),
        ("FAILED", SlurmJobStatus.FAILED),
        ("TIMEOUT", SlurmJobStatus.FAILED),
        ("NODE_FAIL", SlurmJobStatus.FAILED),
        ("OUT_OF_MEMORY", SlurmJobStatus.FAILED),
        ("CANCELLED", SlurmJobStatus.CANCELLED),
    ],
)
def test_sacct_state_parsing(monkeypatch, tmp_path, slurm_state, expected):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del kwargs
        stdout = (
            "12345.batch|FAILED|1:0|2026-05-08T12:00:00|2026-05-08T12:01:00\n"
            f"12345|{slurm_state}|0:0|2026-05-08T12:00:00|2026-05-08T12:05:00\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert gateway.get_job_status("12345").status == expected


def test_scancel_invocation(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.cancel_job("12345")

    assert calls == [["scancel", "12345"]]
    assert record.status == SlurmJobStatus.CANCELLED


def test_template_whitelist_rejects_path_traversal(tmp_path):
    template_dir = _write_template(tmp_path)
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(template_dir),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            job_type_templates={"bad": "../bad.sbatch"},
        )
    )

    with pytest.raises(TemplateSecurityError):
        gateway.render_template("bad", {"run_id": "run_001", "model_id": "model_001", "job_type": "bad"})


def test_manifest_injection_rejected(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for invalid manifests")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError):
        gateway.submit_job(
            SubmitJobRequest(run_id="run_001;rm", model_id="model_001", job_type="run_shud_forecast_array")
        )


def test_sandboxed_environment_restricts_template_access(tmp_path):
    template_dir = _write_template(
        tmp_path,
        content="{{ cycler.__init__.__globals__.os.system('echo unsafe') }}",
    )
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(template_dir),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            job_type_templates={"run_shud_forecast_array": "run.sbatch"},
        )
    )

    with pytest.raises(SecurityError):
        gateway.render_template(
            "run_shud_forecast_array",
            {"run_id": "run_001", "model_id": "model_001", "job_type": "run_shud_forecast_array"},
        )


def test_subprocess_timeout_handling(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del kwargs
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SlurmTimeoutError):
        gateway.submit_job(
            SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="run_shud_forecast_array")
        )


def test_factory_returns_real_gateway_for_slurm_backend(tmp_path):
    gateway = create_gateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(_write_template(tmp_path)),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
        )
    )

    assert isinstance(gateway, RealSlurmGateway)


def test_health_check_success_and_failure(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)

    def success(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(command, 0, stdout="slurm 24.05.1\n", stderr="")

    monkeypatch.setattr(subprocess, "run", success)
    assert gateway.health().model_dump() == {"backend": "slurm", "version": "slurm 24.05.1", "status": "healthy"}

    def failure(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing sinfo")

    monkeypatch.setattr(subprocess, "run", failure)
    response = gateway.health()
    assert response.backend == "slurm"
    assert response.status == "unhealthy"
