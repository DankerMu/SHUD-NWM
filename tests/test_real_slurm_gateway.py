from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jinja2.exceptions import SecurityError

from services.slurm_gateway.config import SlurmGatewaySettings
from services.slurm_gateway.gateway import (
    ManifestValidationError,
    SlurmGatewayError,
    SlurmParseError,
    SlurmTimeoutError,
    TemplateNotFoundError,
    TemplateSecurityError,
    create_gateway,
)
from services.slurm_gateway.models import SlurmJobRecord, SlurmJobStatus, SubmitJobRequest
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


def test_submit_job_accepts_nested_model_id_and_script_manifest(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        return subprocess.CompletedProcess([], 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.submit_job(
        SubmitJobRequest(
            manifest={
                "run_id": "run_001",
                "stage": "run_shud_forecast_array",
                "model": {"model_id": "model_001"},
                "script": "#!/usr/bin/env bash\necho ok\n",
            }
        )
    )

    assert record.job_id == "12345"
    assert record.model_id == "model_001"
    assert record.manifest["model"]["model_id"] == "model_001"


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


def test_array_task_results_parse_task_lines_only(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        stdout = "\n".join(
            [
                "12345|COMPLETED|0:0",
                "12345.batch|COMPLETED|0:0",
                "12345_0|COMPLETED|0:0",
                "12345_1|FAILED|1:0",
                "12345_1.batch|FAILED|1:0",
                "12345.extern|COMPLETED|0:0",
            ]
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert gateway.get_array_task_results("12345") == [
        {"task_id": 0, "job_id": "12345_0", "state": "COMPLETED", "exit_code": 0},
        {"task_id": 1, "job_id": "12345_1", "state": "FAILED", "exit_code": 1},
    ]
    assert "--format=JobID,State,ExitCode" in calls[0]
    assert "--jobs=12345" in calls[0]


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


@pytest.mark.parametrize("operation", ["cancel_job", "get_job_status"])
def test_job_id_option_injection_rejected(monkeypatch, tmp_path, operation):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for invalid job_id")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SlurmGatewayError) as exc_info:
        getattr(gateway, operation)("--user=root")

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "INVALID_JOB_ID"


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


def test_unsupported_legacy_job_type_rejected_before_submission(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for unsupported job_type")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TemplateNotFoundError):
        gateway.submit_job(SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="legacy_unsupported"))


def test_hindcast_single_job_type_resolves_configured_template(monkeypatch, tmp_path):
    template_dir = _write_template(tmp_path, name="hindcast.sbatch")
    gateway = RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(template_dir),
            resource_profiles_path=str(_write_resource_profiles(tmp_path)),
            job_type_templates={"hindcast": "hindcast.sbatch"},
            workspace_dir=str(tmp_path / "workspace"),
        )
    )
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.submit_job(SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="hindcast"))

    assert record.job_id == "12345"
    assert record.manifest["job_type"] == "hindcast"
    assert calls[0][0] == "sbatch"


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
        gateway.submit_job(SubmitJobRequest(run_id="run_001", model_id="model_001", job_type="run_shud_forecast_array"))


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
    assert gateway.health().model_dump() == {
        "backend": "slurm",
        "version": "slurm 24.05.1",
        "status": "healthy",
        "error": None,
    }

    def failure(command, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="missing sinfo")

    monkeypatch.setattr(subprocess, "run", failure)
    response = gateway.health()
    assert response.backend == "slurm"
    assert response.status == "unhealthy"
    assert response.version == ""
    assert response.error


def test_fetch_logs_refuses_symlink(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("subprocess.run should not be called"))
    now = datetime.now(UTC)
    gateway._jobs["12345"] = SlurmJobRecord(
        job_id="12345",
        run_id="run_001",
        model_id="model_001",
        status=SlurmJobStatus.SUCCEEDED,
        submitted_at=now,
        updated_at=now,
        finished_at=now,
    )
    secret_path = tmp_path / "secret.txt"
    secret_path.write_text("secret", encoding="utf-8")
    log_dir = tmp_path / "workspace" / "run_001" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "12345.out").symlink_to(secret_path)

    response = gateway.fetch_logs("12345")

    assert response.logs == ""
    assert response.truncated is False


def test_parse_slurm_datetime_rejects_garbage(tmp_path):
    gateway = _gateway(tmp_path)

    with pytest.raises(SlurmParseError):
        gateway._parse_slurm_datetime("definitely-not-a-time")


def test_list_jobs_defaults_to_lookback_start_time(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert gateway.list_jobs(limit=100, offset=0) == []
    assert any(arg.startswith("--starttime=") for arg in calls[0])
