from __future__ import annotations

import json
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from services.slurm_gateway import real_backend as real_backend_module
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES, SlurmGatewaySettings
from services.slurm_gateway.gateway import (
    ConfigurationError,
    ManifestValidationError,
    SlurmGatewayError,
    SlurmValidationError,
)
from services.slurm_gateway.real_backend import RealSlurmGateway
from services.slurm_gateway.resource_profiles import load_resource_profiles, resolve_model_resource_profile


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


def test_manifest_index_timestamp_collision_does_not_overwrite_first_file(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    fixed_now = datetime(2026, 5, 21, 12, 0, 0, 123456, tzinfo=UTC)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(real_backend_module, "datetime", FrozenDatetime)

    first = gateway.write_manifest_index("cycle_001", "run_shud_forecast_array", _tasks(1))
    first_content = first.read_bytes()
    second_tasks = _tasks(1)
    second_tasks[0]["run_id"] = "run_collision_second"

    second = gateway.write_manifest_index("cycle_001", "run_shud_forecast_array", second_tasks)

    assert second != first
    assert first.read_bytes() == first_content
    assert json.loads(first.read_text(encoding="utf-8"))[0]["run_id"] == "run_0"
    assert json.loads(second.read_text(encoding="utf-8"))[0]["run_id"] == "run_collision_second"


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


def test_db_free_template_exports_file_backends_without_scrubbing_database_urls(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nhms:secret@10.0.2.100:55433/nhms")
    monkeypatch.setenv("PIPELINE_DATABASE_URL", "postgresql://nhms:secret@10.0.2.100:55433/nhms")
    monkeypatch.setenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", "true")
    control_registry_manifest = tmp_path / "object-store" / "scheduler" / "registry" / "manifest-last.json"
    control_readiness_index = (
        tmp_path / "object-store" / "scheduler" / "canonical-readiness" / "index-last.json"
    )
    slurm_registry_manifest = tmp_path / "scratch-object-store" / "scheduler" / "registry" / "manifest-last.json"
    slurm_readiness_index = (
        tmp_path / "scratch-object-store" / "scheduler" / "canonical-readiness" / "index-last.json"
    )
    slurm_state_index = tmp_path / "scratch-object-store" / "scheduler" / "state-index" / "index-last.json"
    monkeypatch.setenv(
        "NHMS_SCHEDULER_REGISTRY_MANIFEST",
        str(control_registry_manifest),
    )
    monkeypatch.setenv(
        "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX",
        str(control_readiness_index),
    )
    monkeypatch.setenv("NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST", str(slurm_registry_manifest))
    monkeypatch.setenv("NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX", str(slurm_readiness_index))
    monkeypatch.setenv("NHMS_SLURM_SCHEDULER_STATE_INDEX", str(slurm_state_index))
    gateway = _production_gateway(tmp_path)

    rendered = gateway.render_template(
        "produce_forcing_array",
        {
            "run_id": "cycle_gfs_2026062118_convert_basins_qhh_shud",
            "model_id": "basins_qhh_shud",
            "cycle_id": "gfs_2026062118",
            "cycle_time": "2026-06-21T18:00:00Z",
            "source_id": "gfs",
            "stage_name": "forcing",
            "stage": "forcing",
            "job_type": "produce_forcing_array",
            "manifest_index_path": "/tmp/index.json",
            "workspace_dir": str(tmp_path / "workspace"),
            "object_store_root": str(tmp_path / "object-store"),
            "scheduler_db_free_required": True,
        },
        "/tmp/index.json",
    )

    assert "unset DATABASE_URL" not in rendered
    assert "unset PIPELINE_DATABASE_URL" not in rendered
    assert "export NHMS_CANONICAL_DB_FREE=true" in rendered
    assert "export NHMS_CANONICAL_REPOSITORY_BACKEND=file" in rendered
    assert "export NHMS_FORCING_DB_FREE=true" in rendered
    assert "export NHMS_FORCING_REPOSITORY_BACKEND=file" in rendered
    assert "export NHMS_SCHEDULER_REGISTRY_BACKEND=file" in rendered
    assert "export NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND=file" in rendered
    assert "export NHMS_SCHEDULER_STATE_INDEX_BACKEND=file" in rendered
    assert f"export NHMS_SCHEDULER_REGISTRY_MANIFEST={shlex.quote(str(slurm_registry_manifest))}" in rendered
    assert (
        f"export NHMS_SCHEDULER_CANONICAL_READINESS_INDEX={shlex.quote(str(slurm_readiness_index))}"
        in rendered
    )
    assert f"export NHMS_SCHEDULER_STATE_INDEX={shlex.quote(str(slurm_state_index))}" in rendered
    assert str(control_registry_manifest) not in rendered
    assert str(control_readiness_index) not in rendered
    assert "10.0.2.100" not in rendered
    assert "secret" not in rendered


def test_array_validation_rejects_zero_tasks(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("subprocess.run should not be called"))

    with pytest.raises(SlurmValidationError, match="Cannot submit array job with 0 tasks"):
        gateway.submit_job_array("run_shud_forecast_array", "cycle_001", "run_shud_forecast_array", [])


def test_write_manifest_index_rejects_task_count_over_limit_before_file_creation(monkeypatch, tmp_path):
    from packages.common import manifest_index as manifest_index_module

    gateway = _gateway(tmp_path)
    monkeypatch.setattr(manifest_index_module, "MAX_MANIFEST_INDEX_ENTRIES", 1)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.write_manifest_index("cycle_001", "run_shud_forecast_array", _tasks(2))

    assert exc_info.value.details["entry_limit"] == 1
    assert not (tmp_path / "workspace").exists()


def test_write_manifest_index_rejects_serialized_size_over_limit_before_file_creation(monkeypatch, tmp_path):
    from packages.common import manifest_index as manifest_index_module

    gateway = _gateway(tmp_path)
    monkeypatch.setattr(manifest_index_module, "MAX_MANIFEST_INDEX_BYTES", 32)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.write_manifest_index("cycle_001", "run_shud_forecast_array", _tasks(1))

    assert exc_info.value.details["size_limit"] == 32
    assert not (tmp_path / "workspace").exists()


def test_array_validation_rejects_zero_max_concurrent(monkeypatch, tmp_path):
    gateway = _gateway(tmp_path, _profiles(max_concurrent=0))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("subprocess.run should not be called"))

    with pytest.raises(SlurmGatewayError, match="resource profile"):
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
    assert f"export NHMS_MANIFEST_INDEX={manifest_index}" in captured["script"]
    assert record.manifest["workspace_dir"] == str(submitted_workspace.resolve())
    tasks = json.loads(manifest_index.read_text(encoding="utf-8"))
    assert {entry["workspace_dir"] for entry in tasks} == {str(submitted_workspace.resolve())}


def test_array_submission_timestamp_collision_keeps_first_manifest_index_immutable(monkeypatch, tmp_path):
    gateway = _production_gateway(tmp_path)
    fixed_now = datetime(2026, 5, 21, 12, 0, 0, 123456, tzinfo=UTC)
    captured_scripts: list[str] = []

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    def fake_run(command, **kwargs):
        del kwargs
        captured_scripts.append(Path(command[-1]).read_text(encoding="utf-8"))
        job_id = 12345 + len(captured_scripts)
        return subprocess.CompletedProcess(command, 0, stdout=f"Submitted batch job {job_id}\n", stderr="")

    monkeypatch.setattr(real_backend_module, "datetime", FrozenDatetime)
    monkeypatch.setattr(subprocess, "run", fake_run)

    first_record = gateway.submit_job_array(
        job_type="run_shud_forecast_array",
        cycle_id="cycle_001",
        stage_name="forecast",
        tasks=_tasks(1),
        manifest={
            "run_id": "cycle_001",
            "model_id": "model_001",
            "workspace_dir": str(tmp_path / "workspace" / "scheduler"),
            "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
        },
    )
    first_index = Path(first_record.manifest["manifest_index_path"])
    first_content = first_index.read_bytes()
    second_tasks = _tasks(1)
    second_tasks[0]["run_id"] = "run_collision_second"

    second_record = gateway.submit_job_array(
        job_type="run_shud_forecast_array",
        cycle_id="cycle_001",
        stage_name="forecast",
        tasks=second_tasks,
        manifest={
            "run_id": "cycle_001",
            "model_id": "model_001",
            "workspace_dir": str(tmp_path / "workspace" / "scheduler"),
            "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
        },
    )

    second_index = Path(second_record.manifest["manifest_index_path"])
    assert second_index != first_index
    assert first_index.read_bytes() == first_content
    assert json.loads(first_index.read_text(encoding="utf-8"))[0]["run_id"] == "run_0"
    assert json.loads(second_index.read_text(encoding="utf-8"))[0]["run_id"] == "run_collision_second"
    assert str(first_index) in captured_scripts[0]
    assert str(second_index) in captured_scripts[1]


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


def test_submit_job_array_rejects_task_count_over_limit_before_manifest_or_sbatch(monkeypatch, tmp_path):
    from packages.common import manifest_index as manifest_index_module

    gateway = _production_gateway(tmp_path)
    monkeypatch.setattr(manifest_index_module, "MAX_MANIFEST_INDEX_ENTRIES", 1)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for over-limit arrays")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job_array(
            job_type="run_shud_forecast_array",
            cycle_id="cycle_001",
            stage_name="forecast",
            tasks=_tasks(2),
            manifest={
                "run_id": "cycle_001",
                "model_id": "model_001",
                "workspace_dir": str(tmp_path / "workspace"),
                "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
            },
        )

    assert exc_info.value.details["entry_limit"] == 1
    assert not (tmp_path / "workspace").exists()


def test_submit_job_array_rejects_serialized_size_over_limit_before_manifest_or_sbatch(monkeypatch, tmp_path):
    from packages.common import manifest_index as manifest_index_module

    gateway = _production_gateway(tmp_path)
    monkeypatch.setattr(manifest_index_module, "MAX_MANIFEST_INDEX_BYTES", 32)

    def fake_run(command, **kwargs):
        del command, kwargs
        raise AssertionError("subprocess.run must not be called for over-limit arrays")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ManifestValidationError) as exc_info:
        gateway.submit_job_array(
            job_type="run_shud_forecast_array",
            cycle_id="cycle_001",
            stage_name="forecast",
            tasks=_tasks(1),
            manifest={
                "run_id": "cycle_001",
                "model_id": "model_001",
                "workspace_dir": str(tmp_path / "workspace"),
                "slurm_job_type_templates": dict(DEFAULT_JOB_TYPE_TEMPLATES),
            },
        )

    assert exc_info.value.details["size_limit"] == 32
    assert not (tmp_path / "workspace").exists()


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
    assert "export NHMS_MANIFEST_INDEX=" in captured["script"]
    assert str(manifest_index) in captured["script"]


# ---------------------------------------------------------------------------
# #3344: job_type_overrides -- a third resolution layer after the model override.
# Expected values below are restated from the #3344 fixture (design and spec
# scenarios), not computed from the resolver.
# ---------------------------------------------------------------------------

_JT_DEFAULT = {
    "partition": "compute",
    "nodes": 1,
    "ntasks": 1,
    "cpus_per_task": 4,
    "memory_gb": 8,
    "walltime": "14:00:00",
    "max_concurrent": 32,
    "shud_threads": 4,
}
_JT_MODEL_OVERRIDE = {
    "cpus_per_task": 12,
    "memory_gb": 16,
    "walltime": "20:00:00",
    "max_concurrent": 6,
    "shud_threads": 12,
}
_JT_FORCING_ENTRY = {"produce_forcing_array": {"cpus_per_task": 1, "shud_threads": 1}}
_JT_HLJ_RESOLVED = {
    "partition": "compute",
    "nodes": 1,
    "ntasks": 1,
    "cpus_per_task": 12,
    "memory_gb": 16,
    "walltime": "20:00:00",
    "max_concurrent": 6,
    "shud_threads": 12,
}


def _jt_profiles(job_type_overrides: object = None) -> dict[str, object]:
    profiles: dict[str, object] = {
        "default": dict(_JT_DEFAULT),
        "overrides": {"hlj_dg": dict(_JT_MODEL_OVERRIDE)},
    }
    if job_type_overrides is not None:
        profiles["job_type_overrides"] = job_type_overrides
    return profiles


def test_job_type_resolution_default_only():
    assert resolve_model_resource_profile(_jt_profiles(), "model_001") == _JT_DEFAULT
    assert resolve_model_resource_profile(_jt_profiles(), "model_001", job_type="produce_forcing_array") == _JT_DEFAULT


def test_job_type_resolution_model_override_only():
    assert resolve_model_resource_profile(_jt_profiles(), "hlj_dg") == _JT_HLJ_RESOLVED


def test_job_type_resolution_job_type_override_only():
    resolved = resolve_model_resource_profile(
        _jt_profiles(_JT_FORCING_ENTRY), "model_001", job_type="produce_forcing_array"
    )

    assert resolved == {**_JT_DEFAULT, "cpus_per_task": 1, "shud_threads": 1}


def test_job_type_resolution_stacks_job_type_over_model_override():
    profiles = _jt_profiles(_JT_FORCING_ENTRY)

    forcing = resolve_model_resource_profile(profiles, "hlj_dg", job_type="produce_forcing_array")
    forecast = resolve_model_resource_profile(profiles, "hlj_dg", job_type="run_shud_forecast_array")

    assert forcing == {
        "partition": "compute",
        "nodes": 1,
        "ntasks": 1,
        "cpus_per_task": 1,
        "memory_gb": 16,
        "walltime": "20:00:00",
        "max_concurrent": 6,
        "shud_threads": 1,
    }
    assert forecast == _JT_HLJ_RESOLVED


def test_job_type_resolution_without_entry_or_without_job_type_is_default_plus_model():
    profiles = _jt_profiles(_JT_FORCING_ENTRY)

    assert resolve_model_resource_profile(profiles, "hlj_dg", job_type="run_shud_forecast_array") == _JT_HLJ_RESOLVED
    assert resolve_model_resource_profile(profiles, "hlj_dg", job_type="analysis_produce_forcing") == _JT_HLJ_RESOLVED
    assert resolve_model_resource_profile(profiles, "hlj_dg", job_type=None) == _JT_HLJ_RESOLVED
    assert resolve_model_resource_profile(profiles, "hlj_dg") == _JT_HLJ_RESOLVED
    assert resolve_model_resource_profile(profiles, "model_001", job_type="") == _JT_DEFAULT


def test_job_type_resolution_deployment_overlay_is_applied_last():
    # In-memory only: the loader would refuse `partition` in an entry.
    profiles = _jt_profiles(
        {"produce_forcing_array": {"partition": "other", "cpus_per_task": 1, "shud_threads": 1}}
    )

    without_overlay = resolve_model_resource_profile(profiles, "hlj_dg", job_type="produce_forcing_array")
    with_overlay = resolve_model_resource_profile(
        profiles,
        "hlj_dg",
        job_type="produce_forcing_array",
        partition_override="CPU",
        exclude_nodes="w2",
    )

    assert without_overlay["partition"] == "other"
    assert with_overlay["partition"] == "CPU"
    assert with_overlay["exclude_nodes"] == "w2"
    assert with_overlay["cpus_per_task"] == 1


@pytest.mark.parametrize(
    "job_type_overrides",
    [
        {"produce_forcing_array": "cpus_per_task=1"},
        {"produce_forcing_array": ["cpus_per_task", 1]},
        {"produce_forcing_array": None},
        ["produce_forcing_array"],
    ],
)
def test_job_type_resolution_ignores_non_mapping_in_memory_entries(job_type_overrides):
    profiles = _jt_profiles(job_type_overrides)

    assert resolve_model_resource_profile(profiles, "hlj_dg", job_type="produce_forcing_array") == _JT_HLJ_RESOLVED


@pytest.mark.parametrize(
    ("entry_key", "job_type"),
    [
        ("save_state_snapshot_array", "save_state_snapshot"),
        ("convert_canonical", "analysis_convert_canonical"),
    ],
)
def test_job_type_resolution_matches_entry_key_exactly(entry_key, job_type):
    profiles = _jt_profiles({entry_key: {"cpus_per_task": 1, "shud_threads": 1}})

    assert resolve_model_resource_profile(profiles, "hlj_dg", job_type=job_type) == _JT_HLJ_RESOLVED
    assert resolve_model_resource_profile(profiles, "model_001", job_type=job_type) == _JT_DEFAULT
    assert resolve_model_resource_profile(profiles, "hlj_dg", job_type=entry_key)["cpus_per_task"] == 1


_JT_LOADER_PREFIX = """
resource_profiles:
  default:
    partition: compute
    nodes: 1
    ntasks: 1
    cpus_per_task: 4
    memory_gb: 8
    walltime: "14:00:00"
    max_concurrent: 32
    shud_threads: 4
  overrides:
    hlj_dg:
      cpus_per_task: 12
      shud_threads: 12
"""


@pytest.mark.parametrize("tail", ["", "  job_type_overrides:\n", "  job_type_overrides: null\n"])
def test_job_type_overrides_absent_or_null_loads_as_empty_mapping(tmp_path, tail):
    profiles = load_resource_profiles(_write_profiles(tmp_path, _JT_LOADER_PREFIX + tail))

    assert profiles["job_type_overrides"] == {}
    assert resolve_model_resource_profile(profiles, "hlj_dg", job_type="produce_forcing_array")["cpus_per_task"] == 12


def test_job_type_overrides_valid_entry_loads(tmp_path):
    tail = (
        "  job_type_overrides:\n"
        "    produce_forcing_array:\n"
        "      cpus_per_task: 1\n"
        "      shud_threads: 1\n"
        "    parse_output_array:\n"
        "      memory_gb: 2\n"
    )

    profiles = load_resource_profiles(_write_profiles(tmp_path, _JT_LOADER_PREFIX + tail))

    assert profiles["job_type_overrides"] == {
        "produce_forcing_array": {"cpus_per_task": 1, "shud_threads": 1},
        "parse_output_array": {"memory_gb": 2},
    }


# (yaml tail, key names the message must name, a value token the error must not leak)
_JT_REFUSED_FILES = {
    "not_a_mapping": ("  job_type_overrides: [produce_forcing_array]\n", ["job_type_overrides"], None),
    "entry_not_a_mapping": (
        "  job_type_overrides:\n    produce_forcing_array: cores_one_xyz\n",
        ["produce_forcing_array"],
        "cores_one_xyz",
    ),
    "max_concurrent": (
        "  job_type_overrides:\n    produce_forcing_array:\n"
        "      cpus_per_task: 1\n      shud_threads: 1\n      max_concurrent: 8731\n",
        ["produce_forcing_array", "max_concurrent"],
        "8731",
    ),
    "walltime": (
        '  job_type_overrides:\n    produce_forcing_array:\n      walltime: "93:17:41"\n',
        ["produce_forcing_array", "walltime"],
        "93:17:41",
    ),
    "partition": (
        "  job_type_overrides:\n    produce_forcing_array:\n      partition: zzpart_secretish\n",
        ["produce_forcing_array", "partition"],
        "zzpart_secretish",
    ),
    "account": (
        "  job_type_overrides:\n    produce_forcing_array:\n      account: acct_secretish\n",
        ["produce_forcing_array", "account"],
        "acct_secretish",
    ),
    "exclude_nodes": (
        "  job_type_overrides:\n    produce_forcing_array:\n      exclude_nodes: nodeq77\n",
        ["produce_forcing_array", "exclude_nodes"],
        "nodeq77",
    ),
    "cpus_without_threads": (
        "  job_type_overrides:\n    produce_forcing_array:\n      cpus_per_task: 1\n",
        ["produce_forcing_array", "cpus_per_task", "shud_threads"],
        None,
    ),
    "threads_without_cpus": (
        "  job_type_overrides:\n    produce_forcing_array:\n      shud_threads: 1\n      memory_gb: 2\n",
        ["produce_forcing_array", "cpus_per_task", "shud_threads"],
        None,
    ),
    "zero_cpus": (
        "  job_type_overrides:\n    produce_forcing_array:\n      cpus_per_task: 0\n      shud_threads: 1\n",
        ["produce_forcing_array", "cpus_per_task"],
        None,
    ),
    "negative_threads": (
        "  job_type_overrides:\n    produce_forcing_array:\n      cpus_per_task: 1\n      shud_threads: -37\n",
        ["produce_forcing_array", "shud_threads"],
        "-37",
    ),
    "zero_memory": (
        "  job_type_overrides:\n    produce_forcing_array:\n      memory_gb: 0\n",
        ["produce_forcing_array", "memory_gb"],
        None,
    ),
    "float_cpus": (
        "  job_type_overrides:\n    produce_forcing_array:\n      cpus_per_task: 1.5\n      shud_threads: 1\n",
        ["produce_forcing_array", "cpus_per_task"],
        "1.5",
    ),
    "bool_threads": (
        "  job_type_overrides:\n    produce_forcing_array:\n      cpus_per_task: 1\n      shud_threads: true\n",
        ["produce_forcing_array", "shud_threads"],
        None,
    ),
    "string_memory": (
        "  job_type_overrides:\n    produce_forcing_array:\n      memory_gb: abcxyz\n",
        ["produce_forcing_array", "memory_gb"],
        "abcxyz",
    ),
    "unsafe_job_type_name": (
        '  job_type_overrides:\n    "forcing;rm -rf":\n      max_concurrent: 2\n',
        ["[redacted]", "max_concurrent"],
        "forcing;rm -rf",
    ),
    # The spec's literal scenario: no shud_threads either, and the closed key set
    # (not the cores/threads pair) is what must be reported.
    "spec_max_concurrent_without_threads": (
        "  job_type_overrides:\n    produce_forcing_array:\n      cpus_per_task: 1\n      max_concurrent: 8\n",
        ["produce_forcing_array", "max_concurrent"],
        None,
    ),
    "over_limit_cpus": (
        "  job_type_overrides:\n    produce_forcing_array:\n      cpus_per_task: 9999\n      shud_threads: 1\n",
        ["produce_forcing_array", "cpus_per_task"],
        "9999",
    ),
}


@pytest.mark.parametrize("case", sorted(_JT_REFUSED_FILES))
def test_job_type_overrides_malformed_file_is_refused_at_load(tmp_path, case):
    tail, named_keys, leaked_value = _JT_REFUSED_FILES[case]
    path = _write_profiles(tmp_path, _JT_LOADER_PREFIX + tail)

    with pytest.raises(ConfigurationError) as exc_info:
        load_resource_profiles(path)

    message = str(exc_info.value)
    for key in named_keys:
        assert key in message, (case, key, message)
    if leaked_value is not None:
        assert leaked_value not in message
        assert leaked_value not in repr(exc_info.value.details)


# The repository's shipped profile file: the one entry it carries and what every
# job type resolves to for every model the file knows plus one it does not.
_SHIPPED_PROFILES_PATH = Path(__file__).resolve().parents[1] / "config" / "resource_profiles.yaml"
_HLJ_DIRECT_GRID_MODELS = ("dg_3b091cccc163690de93cddd9c4c32472", "dg_8a34ed2ba8f8dd22f2716405569628a9")


def test_shipped_profiles_give_forcing_one_core_and_one_thread_and_leave_other_job_types_alone():
    raw = yaml.safe_load(_SHIPPED_PROFILES_PATH.read_text(encoding="utf-8"))["resource_profiles"]
    profiles = load_resource_profiles(_SHIPPED_PROFILES_PATH)

    assert raw["job_type_overrides"] == {"produce_forcing_array": {"cpus_per_task": 1, "shud_threads": 1}}
    assert (raw["default"]["cpus_per_task"], raw["default"]["shud_threads"]) == (4, 4)
    models = [*raw["overrides"], "unlisted_model_3344"]
    assert set(_HLJ_DIRECT_GRID_MODELS) <= set(models)
    assert "produce_forcing_array" in DEFAULT_JOB_TYPE_TEMPLATES

    for model_id in models:
        default_plus_model = {**raw["default"], **raw["overrides"].get(model_id, {})}
        for job_type in DEFAULT_JOB_TYPE_TEMPLATES:
            resolved = resolve_model_resource_profile(profiles, model_id, job_type=job_type)
            if job_type == "produce_forcing_array":
                expected = {**default_plus_model, "cpus_per_task": 1, "shud_threads": 1}
            else:
                expected = default_plus_model
            assert resolved == expected, (model_id, job_type)

    for model_id in _HLJ_DIRECT_GRID_MODELS:
        forecast = resolve_model_resource_profile(profiles, model_id, job_type="run_shud_forecast_array")
        forcing = resolve_model_resource_profile(profiles, model_id, job_type="produce_forcing_array")
        assert (forecast["cpus_per_task"], forecast["shud_threads"]) == (12, 12)
        assert (forcing["cpus_per_task"], forcing["shud_threads"]) == (1, 1)
        assert forcing["memory_gb"] == forecast["memory_gb"] == 8
        assert forcing["walltime"] == forecast["walltime"] == "14:00:00"
