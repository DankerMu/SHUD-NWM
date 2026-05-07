from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from workers.shud_runtime.runtime import SHUDRuntime, SHUDRuntimeConfig, SHUDRuntimeError


class FakeHydroRunRepository:
    def __init__(self) -> None:
        self.statuses: list[str] = []
        self.failures: list[tuple[str, str]] = []
        self.created: list[dict[str, Any]] = []
        self.success_fields: dict[str, Any] = {}

    def create_run(self, manifest: dict[str, Any], run_manifest_uri: str) -> dict[str, Any]:
        self.created.append({"run_id": manifest["run_id"], "run_manifest_uri": run_manifest_uri})
        self.statuses.append("created")
        return {}

    def update_status(self, _run_id: str, status: str, **fields: Any) -> dict[str, Any]:
        self.statuses.append(status)
        if status == "succeeded":
            self.success_fields = dict(fields)
        return {}

    def mark_failed(self, _run_id: str, error_code: str, error_message: str, **_fields: Any) -> dict[str, Any]:
        self.statuses.append("failed")
        self.failures.append((error_code, error_message))
        return {}


def _write_package(object_root: Path) -> None:
    package = object_root / "models" / "demo_model" / "package"
    package.mkdir(parents=True)
    (package / "demo.mesh").write_text("mesh\n", encoding="utf-8")
    (package / "demo.para").write_text(
        "START_TIME = {{START_TIME}}\n"
        "END_TIME = {{END_TIME}}\n"
        "OUTPUT_DIR = {{OUTPUT_DIR}}\n"
        "MODEL_OUTPUT_INTERVAL = {{MODEL_OUTPUT_INTERVAL}}\n"
        "old_ic_file = demo.cfg.ic\n",
        encoding="utf-8",
    )
    (package / "demo.calib").write_text("calib\n", encoding="utf-8")


def _write_forcing(object_root: Path) -> None:
    forcing = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    forcing.mkdir(parents=True)
    (forcing / "forcing.tsd.forc").write_text("forcing\n", encoding="utf-8")


def _manifest(mock_executable: Path) -> dict[str, Any]:
    return {
        "run_id": "fcst_gfs_2026050100_demo_model",
        "run_type": "forecast",
        "scenario_id": "forecast_gfs_deterministic",
        "source_id": "GFS",
        "cycle_time": "2026-05-01T00:00:00Z",
        "start_time": "2026-05-01T00:00:00Z",
        "end_time": "2026-05-04T00:00:00Z",
        "model": {
            "model_id": "demo_model",
            "basin_version_id": "basin_v01",
            "model_package_uri": "s3://nhms/models/demo_model/package/",
            "project_name": "demo",
            "segment_count": 2,
        },
        "initial_state": {"state_id": None, "ic_file_uri": None},
        "forcing": {
            "forcing_version_id": "forc_gfs_2026050100_demo_model",
            "forcing_uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/",
        },
        "runtime": {
            "executable": str(mock_executable),
            "output_interval_minutes": 1440,
        },
        "outputs": {
            "output_uri": "s3://nhms/runs/fcst_gfs_2026050100_demo_model/output/",
            "log_uri": "s3://nhms/runs/fcst_gfs_2026050100_demo_model/logs/",
        },
    }


def _runtime(tmp_path: Path, repository: FakeHydroRunRepository) -> SHUDRuntime:
    config = SHUDRuntimeConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=tmp_path / "object-store",
        object_store_prefix="s3://nhms",
        executable="shud_omp",
        output_interval_minutes=1440,
        timeout_seconds=30,
    )
    return SHUDRuntime(
        config=config,
        repository=repository,
        object_store=LocalObjectStore(config.object_store_root, config.object_store_prefix),
    )


def test_runtime_executes_mock_shud_and_updates_statuses(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest(Path("workers/shud_runtime/mock_shud_omp.py").resolve())

    result = runtime.execute(manifest)

    cfg_path = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input" / "demo.cfg.para"
    output_path = tmp_path / "workspace" / "runs" / manifest["run_id"] / "output" / "demo.rivqdown"
    log_path = tmp_path / "workspace" / "runs" / manifest["run_id"] / "logs" / "shud_stdout.log"

    assert result.status == "succeeded"
    assert repository.statuses == ["created", "staged", "running", "succeeded"]
    assert repository.success_fields["output_uri"] == "s3://nhms/runs/fcst_gfs_2026050100_demo_model/output/"
    assert output_path.exists()
    assert log_path.exists()
    cfg = cfg_path.read_text(encoding="utf-8")
    assert "START_TIME = 2026-05-01T00:00:00Z" in cfg
    assert "END_TIME = 2026-05-04T00:00:00Z" in cfg
    assert "MODEL_OUTPUT_INTERVAL = 1440" in cfg
    assert "INIT_MODE = cold-start" in cfg
    assert ".cfg.ic" not in cfg


def test_output_verification_rejects_wrong_row_count(tmp_path: Path) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest(Path("workers/shud_runtime/mock_shud_omp.py").resolve())
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.rivqdown").write_text("time,seg1,seg2\n2026-05-01T00:00:00Z,1,2\n", encoding="utf-8")

    with pytest.raises(SHUDRuntimeError, match="expected 3 data rows"):
        runtime.verify_output(manifest, output_dir)


def test_workspace_failure_marks_run_failed(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_package(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest(Path("workers/shud_runtime/mock_shud_omp.py").resolve())

    with pytest.raises(SHUDRuntimeError, match="Object storage artifact not found"):
        runtime.execute(manifest)

    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "ARTIFACT_NOT_FOUND"
