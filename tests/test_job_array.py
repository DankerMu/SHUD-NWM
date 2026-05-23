from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES, SlurmGatewaySettings
from services.slurm_gateway.gateway import ConfigurationError, SlurmValidationError
from services.slurm_gateway.real_backend import RealSlurmGateway


def _write_profiles(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "resource_profiles.yaml"
    path.write_text(body.lstrip(), encoding="utf-8")
    return path


def _profiles(max_concurrent: int = 4) -> str:
    return f"""
resource_profiles:
  default:
    partition: compute
    nodes: 1
    ntasks: 1
    cpus_per_task: 32
    memory_gb: 128
    walltime: "06:00:00"
    max_concurrent: {max_concurrent}
    shud_threads: 32
  overrides:
    yangtze_shud_v12:
      cpus_per_task: 64
      memory_gb: 256
      walltime: "12:00:00"
      shud_threads: 64
"""


def _write_template(tmp_path: Path) -> Path:
    template_dir = tmp_path / "sbatch"
    template_dir.mkdir()
    (template_dir / "array.sbatch").write_text(
        """
#!/usr/bin/env bash
#SBATCH --partition={{partition}}
#SBATCH --nodes={{nodes}}
#SBATCH --ntasks={{ntasks}}
#SBATCH --cpus-per-task={{cpus_per_task}}
#SBATCH --mem={{memory_gb}}G
#SBATCH --time={{walltime}}
export NHMS_MANIFEST_INDEX={{manifest_index_path}}
export SHUD_THREADS={{shud_threads}}
echo "{{run_id}} {{cycle_id}} {{stage_name}} {{max_concurrent}}"
""".lstrip(),
        encoding="utf-8",
    )
    return template_dir


def _gateway(tmp_path: Path, profiles: str | None = None) -> RealSlurmGateway:
    return RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir=str(_write_template(tmp_path)),
            resource_profiles_path=str(_write_profiles(tmp_path, profiles or _profiles())),
            job_type_templates={"run_shud_forecast_array": "array.sbatch"},
            workspace_dir=str(tmp_path / "workspace"),
        )
    )


def _production_gateway(tmp_path: Path) -> RealSlurmGateway:
    return RealSlurmGateway(
        SlurmGatewaySettings(
            backend="slurm",
            template_dir="infra/sbatch",
            resource_profiles_path=str(_write_profiles(tmp_path, _profiles())),
            job_type_templates=dict(DEFAULT_JOB_TYPE_TEMPLATES),
            workspace_dir=str(tmp_path / "workspace"),
        )
    )


def _tasks(count: int) -> list[dict[str, str]]:
    return [
        {
            "model_id": "model_001",
            "basin_version_id": f"basin_{index}",
            "river_network_version_id": f"river_{index}",
            "run_id": f"run_{index}",
            "source_id": "gfs",
            "cycle_time": "2026050100",
        }
        for index in range(count)
    ]


def _hindcast_tasks(tmp_path: Path) -> list[dict[str, str | int]]:
    return [
        {
            "array_task_id": 0,
            "run_id": "hindcast_era5_yangtze_shud_v12_1993",
            "model_id": "yangtze_shud_v12",
            "basin_version_id": "basin_v1",
            "river_network_version_id": "rnv_v1",
            "source_id": "ERA5",
            "year": 1993,
            "cycle_time": "1993-01-01T00:00:00Z",
            "forcing_version_id": "forc_era5_hindcast_yangtze_shud_v12_1993",
            "forcing_package_uri": "forcing/era5/1993/package",
            "object_store_root": str(tmp_path / "object-store"),
            "object_store_prefix": "hindcast/prod",
            "workspace_dir": str(tmp_path / "workspace"),
            "workspace_root": str(tmp_path / "workspace"),
        },
        {
            "array_task_id": 1,
            "run_id": "hindcast_era5_yangtze_shud_v12_1994",
            "model_id": "yangtze_shud_v12",
            "basin_version_id": "basin_v1",
            "river_network_version_id": "rnv_v1",
            "source_id": "ERA5",
            "year": 1994,
            "cycle_time": "1994-01-01T00:00:00Z",
            "forcing_version_id": "forc_era5_hindcast_yangtze_shud_v12_1994",
            "forcing_package_uri": "forcing/era5/1994/package",
            "object_store_root": str(tmp_path / "object-store"),
            "object_store_prefix": "hindcast/prod",
            "workspace_dir": str(tmp_path / "workspace"),
            "workspace_root": str(tmp_path / "workspace"),
        },
    ]


def test_manifest_index_generation(tmp_path):
    gateway = _gateway(tmp_path)

    path = gateway.write_manifest_index("cycle_001", "run_shud_forecast_array", _tasks(2))

    assert path.parent == tmp_path / "workspace" / "cycle_001" / "manifests"
    assert path.name.startswith("run_shud_forecast_array_index_")
    assert path.name.endswith(".json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [entry["task_id"] for entry in data] == [0, 1]
    assert data[0]["model_id"] == "model_001"
    assert data[0]["basin_version_id"] == "basin_0"
    assert data[0]["run_id"] == "run_0"
    assert data[0]["workspace_dir"] == str(tmp_path / "workspace")


def test_manifest_index_generation_uses_versioned_paths(tmp_path):
    gateway = _gateway(tmp_path)

    first = gateway.write_manifest_index("cycle_001", "run_shud_forecast_array", _tasks(1))
    second = gateway.write_manifest_index("cycle_001", "run_shud_forecast_array", _tasks(1))

    assert first != second
    assert first.exists()
    assert second.exists()


def test_resource_profile_loading_default_and_override(tmp_path):
    gateway = _gateway(tmp_path)

    default = gateway.resolve_resource_profile("model_001")
    override = gateway.resolve_resource_profile("yangtze_shud_v12")

    assert default["cpus_per_task"] == 32
    assert default["memory_gb"] == 128
    assert override["partition"] == "compute"
    assert override["cpus_per_task"] == 64
    assert override["memory_gb"] == 256
    assert override["walltime"] == "12:00:00"
    assert override["shud_threads"] == 64


def test_resource_profile_missing_default_raises(tmp_path):
    gateway = _gateway(
        tmp_path,
        """
resource_profiles:
  overrides: {}
""",
    )

    with pytest.raises(ConfigurationError):
        gateway.resolve_resource_profile("model_001")


def test_template_rendering_includes_profile_and_manifest_variables(tmp_path):
    gateway = _gateway(tmp_path)

    rendered = gateway.render_template(
        "run_shud_forecast_array",
        {
            "run_id": "run_001",
            "model_id": "model_001",
            "cycle_id": "cycle_001",
            "stage_name": "run_shud_forecast_array",
            "job_type": "run_shud_forecast_array",
            "manifest_index_path": "/tmp/index.json",
        },
        "/tmp/index.json",
    )

    assert "#SBATCH --partition=compute" in rendered
    assert "#SBATCH --cpus-per-task=32" in rendered
    assert "#SBATCH --mem=128G" in rendered
    assert "export NHMS_MANIFEST_INDEX=/tmp/index.json" in rendered
    assert "export SHUD_THREADS=32" in rendered
    assert "run_001 cycle_001 run_shud_forecast_array 4" in rendered


def test_array_validation_rejects_zero_tasks(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("subprocess.run should not be called"))

    with pytest.raises(SlurmValidationError, match="Cannot submit array job with 0 tasks"):
        gateway.submit_job_array("run_shud_forecast_array", "cycle_001", "run_shud_forecast_array", [])


def test_array_validation_rejects_zero_max_concurrent(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path, _profiles(max_concurrent=0))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("subprocess.run should not be called"))

    with pytest.raises(SlurmValidationError, match="max_concurrent must be \u2265 1"):
        gateway.submit_job_array("run_shud_forecast_array", "cycle_001", "run_shud_forecast_array", _tasks(2))


def test_max_concurrent_is_clamped(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path, _profiles(max_concurrent=20))
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.submit_job_array("run_shud_forecast_array", "cycle_001", "run_shud_forecast_array", _tasks(5))

    assert "--array=0-4%5" in calls[0]
    assert record.manifest["max_concurrent"] == 5


def test_single_basin_falls_back_to_non_array(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    gateway.submit_job_array("run_shud_forecast_array", "cycle_001", "run_shud_forecast_array", _tasks(1))

    assert "--array=0-0%1" in calls[0]


def test_array_sbatch_command_construction(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    gateway.submit_job_array("run_shud_forecast_array", "cycle_001", "run_shud_forecast_array", _tasks(3))

    assert calls[0][0] == "sbatch"
    assert calls[0][1] == "--array=0-2%3"
    assert calls[0][2].endswith(".sbatch")


def test_array_submission_binds_manifest_index_under_submitted_workspace(monkeypatch, tmp_path):
    gateway = _production_gateway(tmp_path)
    submitted_workspace = tmp_path / "workspace" / "scheduler"
    captured: dict[str, str] = {}

    def fake_run(command, **kwargs):
        del kwargs
        captured["script"] = Path(command[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.submit_job_array(
        job_type="run_shud_forecast_array",
        cycle_id="cycle_001",
        stage_name="forecast",
        tasks=_tasks(2),
        manifest={
            "run_id": "cycle_001",
            "model_id": "model_001",
            "workspace_dir": str(submitted_workspace),
            "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
        },
    )

    manifest_index = Path(record.manifest["manifest_index_path"])
    assert manifest_index.is_relative_to(submitted_workspace)
    assert str(manifest_index) in captured["script"]
    assert f'export NHMS_MANIFEST_INDEX="{manifest_index}"' in captured["script"]
    assert record.manifest["workspace_dir"] == str(submitted_workspace.resolve())
    tasks = json.loads(manifest_index.read_text(encoding="utf-8"))
    assert {entry["workspace_dir"] for entry in tasks} == {str(submitted_workspace.resolve())}


def test_array_submission_rejects_sibling_workspace_before_sbatch(monkeypatch, tmp_path):
    gateway = _production_gateway(tmp_path)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called when submitted workspace is outside gateway root")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SlurmValidationError):
        gateway.submit_job_array(
            job_type="run_shud_forecast_array",
            cycle_id="cycle_001",
            stage_name="forecast",
            tasks=_tasks(1),
            manifest={
                "run_id": "cycle_001",
                "model_id": "model_001",
                "workspace_dir": str(tmp_path / "sibling-workspace"),
                "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
            },
        )


def test_hindcast_production_array_submission_writes_required_manifest_fields(monkeypatch, tmp_path):
    gateway = _production_gateway(tmp_path)
    captured: dict[str, str] = {}
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        calls.append(command)
        captured["script"] = Path(command[-1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="Submitted batch job 12345\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    record = gateway.submit_job_array(
        {
            "job_type": "hindcast",
            "cycle_id": "hindcast_yangtze_shud_v12_1993_1994",
            "stage_name": "hindcast",
            "manifest": {
                "run_id": "hindcast_era5_yangtze_shud_v12",
                "model_id": "yangtze_shud_v12",
                "basin_version_id": "basin_v1",
                "river_network_version_id": "rnv_v1",
                "source_id": "ERA5",
                "years": [1993, 1994],
                "object_store_root": str(tmp_path / "object-store"),
                "object_store_prefix": "hindcast/prod",
                "workspace_dir": str(tmp_path / "workspace"),
                "workspace_root": str(tmp_path / "workspace"),
                "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
            },
            "tasks": _hindcast_tasks(tmp_path),
        }
    )

    manifest_index = Path(record.manifest["manifest_index_path"])
    tasks = json.loads(manifest_index.read_text(encoding="utf-8"))
    assert "--array=0-1%2" in calls[0]
    assert tasks[0]["river_network_version_id"] == "rnv_v1"
    assert tasks[1]["river_network_version_id"] == "rnv_v1"
    assert 'export NHMS_MANIFEST_INDEX="' in captured["script"]
    assert str(manifest_index) in captured["script"]
