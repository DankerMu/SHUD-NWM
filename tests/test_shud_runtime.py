from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from packages.common import safe_fs
from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes
from packages.common.safe_fs import SafeFilesystemError
from workers.shud_runtime import runtime as runtime_module
from workers.shud_runtime.runtime import (
    DbFreeHydroRunRepository,
    SHUDRuntime,
    SHUDRuntimeConfig,
    SHUDRuntimeError,
    _state_checkpoint_poll_seconds,
    _StateCheckpointTracker,
    _validate_direct_grid_station_filename_target,
)


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


def _write_basins_package(object_root: Path) -> None:
    package = object_root / "models" / "basins_basin_a_shud" / "vbasins-test" / "package"
    package.mkdir(parents=True)
    (package / "alias-a.sp.mesh").write_text("mesh\n", encoding="utf-8")
    (package / "alias-a.cfg.para").write_text(
        "START_TIME = {{START_TIME}}\n"
        "END_TIME = {{END_TIME}}\n"
        "OUTPUT_DIR = {{OUTPUT_DIR}}\n"
        "MODEL_OUTPUT_INTERVAL = {{MODEL_OUTPUT_INTERVAL}}\n"
        "SEGMENT_COUNT = {{SEGMENT_COUNT}}\n"
        "old_ic_file = alias-a.cfg.ic\n",
        encoding="utf-8",
    )
    (package / "alias-a.cfg.calib").write_text("calib\n", encoding="utf-8")
    (package / "alias-a.sp.riv").write_text("2 1\n", encoding="utf-8")
    (package / "alias-a.sp.rivseg").write_text("2 4\n", encoding="utf-8")
    (package / "alias-a.sp.att").write_text(
        "2\n"
        "ID\tA\tB\tC\tFORC\n"
        "1\t0\t0\t0\t2\n"
        "2\t0\t0\t0\t3\n",
        encoding="utf-8",
    )


def _write_forcing(object_root: Path) -> None:
    forcing = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    forcing.mkdir(parents=True)
    (forcing / "forcing.tsd.forc").write_text("forcing\n", encoding="utf-8")


def _write_standard_shud_forcing(
    object_root: Path,
    *,
    units: dict[str, str] | None = None,
    lineage: dict[str, Any] | None = None,
    station_ids: tuple[int, ...] = (1,),
) -> dict[str, str]:
    forcing = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    shud_dir = forcing / "shud"
    shud_dir.mkdir(parents=True)
    station_lines = []
    csv_files: dict[str, str] = {}
    for station_id in station_ids:
        filename = "forcing.csv" if station_id == 1 else f"forcing_{station_id:03d}.csv"
        station_lines.append(f"{station_id}\t100\t30\t{station_id}\t1\t1\t{filename}")
        csv_files[filename] = (
            "2\t6\t20260501\t20260501\n"
            "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
            f"0\t{station_id}\t2\t3\t4\t5\n"
        )
    tsd_content = (
        f"{len(station_ids)} 20260501\n"
        "/data\n"
        "ID\tLon\tLat\tX\tY\tZ\tFilename\n"
        + "\n".join(station_lines)
        + "\n"
    )
    (shud_dir / "qhh.tsd.forc").write_text(tsd_content, encoding="utf-8")
    for filename, content in csv_files.items():
        (shud_dir / filename).write_text(content, encoding="utf-8")
    manifest_payload: dict[str, Any] = {
        "station_count": len(station_ids),
        "files": [
            {
                "role": "shud_forcing",
                "relative_path": "shud/qhh.tsd.forc",
                "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/qhh.tsd.forc",
                "checksum": sha256_bytes(tsd_content.encode("utf-8")),
            },
        ],
    }
    for filename, content in csv_files.items():
        manifest_payload["files"].append(
            {
                "role": "shud_forcing_csv",
                "relative_path": f"shud/{filename}",
                "uri": f"s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/{filename}",
                "checksum": sha256_bytes(content.encode("utf-8")),
            }
        )
    if units is not None:
        manifest_payload["units"] = units
    if lineage is not None:
        manifest_payload["lineage"] = lineage
    manifest_content = json_bytes(manifest_payload)
    (forcing / "forcing_package.json").write_bytes(manifest_content)
    return {
        "manifest_uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/forcing_package.json",
        "manifest_checksum": sha256_bytes(manifest_content),
        "tsd_checksum": sha256_bytes(tsd_content.encode("utf-8")),
        "csv_checksum": sha256_bytes(csv_files["forcing.csv"].encode("utf-8"))
        if "forcing.csv" in csv_files
        else sha256_bytes(next(iter(csv_files.values())).encode("utf-8")),
    }


def _manifest() -> dict[str, Any]:
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
            "command": ["/does/not/exist"],
            "executable": "/also/not/trusted",
            "output_interval_minutes": 1440,
        },
        "outputs": {
            "output_uri": "s3://nhms/runs/fcst_gfs_2026050100_demo_model/output/",
            "log_uri": "s3://nhms/runs/fcst_gfs_2026050100_demo_model/logs/",
        },
    }


def json_bytes(payload: dict[str, Any]) -> bytes:
    import json

    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _runtime(
    tmp_path: Path,
    repository: FakeHydroRunRepository,
    shud_executable: Path | None = None,
    timeout_seconds: int = 30,
) -> SHUDRuntime:
    config = SHUDRuntimeConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=tmp_path / "object-store",
        object_store_prefix="s3://nhms",
        shud_executable=str(shud_executable or Path("tests/mock_shud_omp.py").resolve()),
        output_interval_minutes=1440,
        timeout_seconds=timeout_seconds,
    )
    return SHUDRuntime(
        config=config,
        repository=repository,
        object_store=LocalObjectStore(config.object_store_root, config.object_store_prefix),
    )


def _shud_project_manifest_with_forcing_checksums(checksums: dict[str, str]) -> dict[str, Any]:
    manifest = _manifest()
    manifest["model"] = {
        "model_id": "basins_basin_a_shud",
        "basin_version_id": "basins_basin_a_vbasins",
        "model_package_uri": "s3://nhms/models/basins_basin_a_shud/vbasins-test/package/",
        "project_name": "alias-a",
        "segment_count": 2,
    }
    manifest["runtime"]["command_style"] = "shud_project"
    manifest["forcing"] = {
        **manifest["forcing"],
        "package_manifest_uri": checksums["manifest_uri"],
        "package_manifest_checksum": checksums["manifest_checksum"],
        "files": [
            {
                "role": "shud_forcing",
                "relative_path": "shud/qhh.tsd.forc",
                "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/qhh.tsd.forc",
                "checksum": checksums["tsd_checksum"],
            },
            {
                "role": "shud_forcing_csv",
                "relative_path": "shud/forcing.csv",
                "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/forcing.csv",
                "checksum": checksums["csv_checksum"],
            },
        ],
    }
    return manifest


def _drop_runtime_forcing_files(manifest: dict[str, Any]) -> dict[str, Any]:
    manifest["forcing"].pop("files", None)
    manifest["forcing"].pop("file_checksums", None)
    return manifest


def test_runtime_executes_mock_shud_and_updates_statuses(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()

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
    assert "INIT_MODE = 1" in cfg
    assert ".cfg.ic" not in cfg


def test_shud_project_warm_start_ic_materializes_in_project_input_dir(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    ic_content = b"2 1 29626560.000000\n1 0.1\n2 0.2\n1 0.0\n"
    state_path = object_root / "states" / "gfs" / "basins_basin_a_shud" / "2026050100" / "state.cfg.ic"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(ic_content)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["initial_state"] = {
        "state_id": "state_gfs_basins_basin_a_shud_2026050100",
        "ic_file_uri": "s3://nhms/states/gfs/basins_basin_a_shud/2026050100/state.cfg.ic",
        "checksum": sha256_bytes(ic_content),
        "valid_time": "2026-05-01T00:00:00Z",
        "quality": "fresh",
    }
    manifest["runtime"]["init_mode"] = 3
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"

    runtime.prepare_workspace(manifest, input_dir)

    assert (input_dir / "alias-a" / "alias-a.cfg.ic").is_file()
    assert not (input_dir / "alias-a" / "alias-a" / "alias-a.cfg.ic").exists()
    assert not (input_dir / "alias-a" / "state.cfg.ic").exists()


def test_shud_project_warm_start_accepts_prefixed_ic_checksum(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    ic_content = b"2 1 29626560.000000\n1 0.1\n2 0.2\n1 0.0\n"
    state_path = object_root / "states" / "gfs" / "basins_basin_a_shud" / "2026050100" / "state.cfg.ic"
    state_path.parent.mkdir(parents=True)
    state_path.write_bytes(ic_content)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["initial_state"] = {
        "state_id": "state_gfs_basins_basin_a_shud_2026050100",
        "ic_file_uri": "s3://nhms/states/gfs/basins_basin_a_shud/2026050100/state.cfg.ic",
        "checksum": f"sha256:{sha256_bytes(ic_content)}",
        "valid_time": "2026-05-01T00:00:00Z",
        "quality": "fresh",
    }
    manifest["runtime"]["init_mode"] = 3
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"

    runtime.prepare_workspace(manifest, input_dir)

    assert (input_dir / "alias-a" / "alias-a.cfg.ic").is_file()
    assert repository.failures == []


def test_forecast_checkpoint_cadence_does_not_shorten_shud_long_run(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["end_time"] = "2026-05-08T00:00:00Z"
    manifest["forecast_horizon_hours"] = 168
    manifest["runtime"]["state_checkpoint_hours"] = [6, 12]
    manifest["runtime"]["update_ic_step_minutes"] = 360

    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    output_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "output"
    runtime.prepare_workspace(manifest, input_dir)
    cfg_path = runtime.generate_cfg_para(
        manifest,
        input_dir,
        output_dir,
    )

    cfg = cfg_path.read_text(encoding="utf-8")
    assert "START\t0.0" in cfg
    assert "END\t7.0" in cfg
    assert "Update_IC_STEP\t360" in cfg
    assert "START_TIME\t2026-05-01T00:00:00Z" in cfg
    assert "END_TIME\t2026-05-08T00:00:00Z" in cfg


def test_shud_project_runtime_applies_validated_solver_parameter_overrides(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    runtime = _runtime(tmp_path, FakeHydroRunRepository())
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["runtime"]["solver_parameters"] = {"MAX_SOLVER_STEP": 2}
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    output_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "output"

    runtime.prepare_workspace(manifest, input_dir)
    cfg_path = runtime.generate_cfg_para(manifest, input_dir, output_dir)

    assert "MAX_SOLVER_STEP\t2" in cfg_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("solver_parameters", "error_code"),
    [
        ({"SHELL_COMMAND": "unsafe"}, "SOLVER_PARAMETER_UNSUPPORTED"),
        ({"MAX_SOLVER_STEP": 0}, "SOLVER_PARAMETER_INVALID"),
        ({"MAX_SOLVER_STEP": True}, "SOLVER_PARAMETER_INVALID"),
    ],
)
def test_shud_project_runtime_rejects_invalid_solver_parameter_overrides(
    tmp_path: Path,
    solver_parameters: dict[str, Any],
    error_code: str,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    runtime = _runtime(tmp_path, FakeHydroRunRepository())
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["runtime"]["solver_parameters"] = solver_parameters
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    output_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "output"
    runtime.prepare_workspace(manifest, input_dir)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.generate_cfg_para(manifest, input_dir, output_dir)

    assert exc_info.value.error_code == error_code


def test_state_checkpoint_tracker_captures_t6_t12_from_long_run_update(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["end_time"] = "2026-05-08T00:00:00Z"
    manifest["forecast_horizon_hours"] = 168
    manifest["runtime"]["state_checkpoint_hours"] = [6, 12]
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    update_file = output_dir / "demo.cfg.ic.update"
    tracker = _StateCheckpointTracker(manifest, output_dir)

    update_file.write_text("2 2 29626920.000000\n1 0.1\n2 0.2\n1 0\n2 0\n", encoding="utf-8")
    tracker.capture_available()
    update_file.write_text("2 2 29627280.000000\n1 0.3\n2 0.4\n1 0\n2 0\n", encoding="utf-8")
    tracker.capture_available()
    tracker.write_manifest()

    checkpoint_dir = output_dir / "state_checkpoints"
    f006 = checkpoint_dir / "demo.f006.cfg.ic.update"
    f012 = checkpoint_dir / "demo.f012.cfg.ic.update"
    payload = json.loads((checkpoint_dir / "state_checkpoints.json").read_text(encoding="utf-8"))

    assert f006.read_text(encoding="utf-8").startswith("2 2 29626920.000000")
    assert f012.read_text(encoding="utf-8").startswith("2 2 29627280.000000")
    assert [item["lead_hours"] for item in payload["checkpoints"]] == [6, 12]
    assert [item["valid_time"] for item in payload["checkpoints"]] == [
        "2026-05-01T06:00:00Z",
        "2026-05-01T12:00:00Z",
    ]


def test_state_checkpoint_tracker_accepts_shud_relative_minutes(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["end_time"] = "2026-05-08T00:00:00Z"
    manifest["forecast_horizon_hours"] = 168
    manifest["runtime"]["state_checkpoint_hours"] = [6, 12]
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    update_file = output_dir / "demo.cfg.ic.update"
    tracker = _StateCheckpointTracker(manifest, output_dir)

    update_file.write_text("2 2 360.000000\n1 0.1\n2 0.2\n1 0\n2 0\n", encoding="utf-8")
    tracker.capture_available()
    update_file.write_text("2 2 720.000000\n1 0.3\n2 0.4\n1 0\n2 0\n", encoding="utf-8")
    tracker.capture_available()
    tracker.write_manifest()

    checkpoint_dir = output_dir / "state_checkpoints"
    assert (checkpoint_dir / "demo.f006.cfg.ic.update").read_text(encoding="utf-8").startswith("2 2 360.000000")
    assert (checkpoint_dir / "demo.f012.cfg.ic.update").read_text(encoding="utf-8").startswith("2 2 720.000000")


def test_state_checkpoint_tracker_retries_header_matching_partial_native_write(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["end_time"] = "2026-05-08T00:00:00Z"
    manifest["forecast_horizon_hours"] = 168
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    update_file = output_dir / "demo.cfg.ic.update"
    tracker = _StateCheckpointTracker(manifest, output_dir)

    update_file.write_text(
        "2\t6\t720.000000\n"
        "Index\tCanopy\tSnow\tSurface\tUnsat\tGW\n"
        "1\t0.0\t0.0\t0.0\t0.0\t0.0\n",
        encoding="utf-8",
    )
    tracker.capture_available()

    checkpoint = output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update"
    assert checkpoint.exists() is False
    assert tracker.missing_hours() == [12]

    update_file.write_text(
        "2\t6\t720.000000\n"
        "Index\tCanopy\tSnow\tSurface\tUnsat\tGW\n"
        "1\t0.0\t0.0\t0.0\t0.0\t0.0\n"
        "2\t0.0\t0.0\t0.0\t0.0\t0.0\n"
        "Index\tRiver_Stage\n"
        "1\t0.0\n",
        encoding="utf-8",
    )
    tracker.capture_available()

    assert checkpoint.is_file() is False
    assert tracker.missing_hours() == [12]

    update_file.write_text(update_file.read_text(encoding="utf-8") + "2\t0.0\n", encoding="utf-8")
    tracker.capture_available()

    assert checkpoint.is_file()
    assert tracker.missing_hours() == []


def test_state_checkpoint_tracker_captures_native_lake_checkpoint(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["end_time"] = "2026-05-08T00:00:00Z"
    manifest["forecast_horizon_hours"] = 168
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    update_file = output_dir / "demo.cfg.ic.update"
    tracker = _StateCheckpointTracker(manifest, output_dir)

    update_file.write_text(
        "2\t6\t720.000000\n"
        "Index\tCanopy\tSnow\tSurface\tUnsat\tGW\n"
        "1\t0.0\t0.0\t0.0\t0.0\t0.0\n"
        "2\t0.0\t0.0\t0.0\t0.0\t0.0\n"
        "Index\tRiver_Stage\n"
        "1\t0.1\n"
        "2\t0.2\n"
        "1\t2\n"
        "Index\tLakeStage\n"
        "1\t0.3\n",
        encoding="utf-8",
    )

    tracker.capture_available()
    tracker.write_manifest()

    checkpoint = output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update"
    assert checkpoint.is_file()
    assert tracker.missing_hours() == []


def test_runtime_manifest_path_missing_raises_stable_manifest_error(tmp_path: Path) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest_path = tmp_path / "workspace" / "runs" / "missing_run" / "input" / "manifest.json"

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute_manifest_path(manifest_path)

    assert exc_info.value.error_code == "RUNTIME_MANIFEST_MISSING"
    assert "missing_run" in exc_info.value.message
    assert repository.statuses == []


def test_runtime_manifest_path_symlink_is_not_followed(tmp_path: Path) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest_path = tmp_path / "workspace" / "runs" / "run_001" / "input" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    target = tmp_path / "outside_manifest.json"
    target.write_text("{}", encoding="utf-8")
    try:
        manifest_path.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is not supported: {exc}")

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute_manifest_path(manifest_path)

    assert exc_info.value.error_code == "WORKSPACE_PATH_UNSAFE"
    assert "symlink" in exc_info.value.message
    assert repository.statuses == []


def test_basins_package_stages_and_generates_cfg_without_live_solver(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    _write_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    manifest["model"] = {
        "model_id": "basins_basin_a_shud",
        "basin_version_id": "basins_basin_a_vbasins",
        "model_package_uri": "s3://nhms/models/basins_basin_a_shud/vbasins-test/package/",
        "project_name": "alias-a",
        "segment_count": 2,
    }

    workspace = tmp_path / "workspace" / "runs" / manifest["run_id"]
    input_dir = workspace / "input"
    output_dir = workspace / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)
    cfg_path = runtime.generate_cfg_para(manifest, input_dir, output_dir)

    assert (input_dir / "alias-a.sp.mesh").read_text(encoding="utf-8") == "mesh\n"
    assert (input_dir / "alias-a.cfg.calib").read_text(encoding="utf-8") == "calib\n"
    assert (input_dir / "forcing.tsd.forc").read_text(encoding="utf-8") == "forcing\n"
    assert cfg_path == input_dir / "alias-a.cfg.para"
    cfg = cfg_path.read_text(encoding="utf-8")
    assert "START_TIME = 2026-05-01T00:00:00Z" in cfg
    assert "END_TIME = 2026-05-04T00:00:00Z" in cfg
    assert "SEGMENT_COUNT = 2" in cfg
    assert ".cfg.ic" not in cfg
    assert repository.statuses == []


def test_runtime_staging_rejects_forcing_file_checksum_mismatch(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["forcing"]["files"][0]["checksum"] = "stale-file-checksum"
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "FORCING_FILE_CHECKSUM_MISMATCH"


def test_runtime_staging_accepts_manifest_carried_forcing_checksums(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    assert (input_dir / "alias-a" / "alias-a.tsd.forc").exists()
    assert (input_dir / "alias-a" / "forcing.csv").exists()


def test_runtime_direct_grid_uses_package_manifest_file_checksums_without_runtime_files(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(2, 3),
    )
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    model_input_dir = input_dir / "alias-a"
    assert (model_input_dir / "alias-a.tsd.forc").exists()
    assert (model_input_dir / "forcing_002.csv").exists()
    assert (model_input_dir / "forcing_003.csv").exists()


def test_runtime_direct_grid_requires_verified_package_manifest_before_forcing_staging(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(2, 3),
    )
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["forcing"].pop("package_manifest_uri")
    manifest["forcing"].pop("package_manifest_checksum")
    manifest["forcing"]["forcing_mapping_mode"] = "direct_grid"

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    model_input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input" / "alias-a"
    assert exc_info.value.error_code == "FORCING_PACKAGE_MANIFEST_REQUIRED"
    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "FORCING_PACKAGE_MANIFEST_REQUIRED"
    assert not (model_input_dir / "shud" / "qhh.tsd.forc").exists()
    assert not (model_input_dir / "alias-a.tsd.forc").exists()


def test_runtime_direct_grid_package_manifest_ignores_stale_outer_forcing_files(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(2, 3),
    )
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["forcing"]["files"][0]["checksum"] = "stale-outer-tsd-checksum"
    manifest["forcing"]["files"][0]["uri"] = (
        "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/stale/qhh.tsd.forc"
    )
    manifest["forcing"]["files"][1]["checksum"] = "stale-outer-csv-checksum"
    manifest["forcing"]["files"][1]["uri"] = (
        "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/stale/forcing.csv"
    )
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    model_input_dir = input_dir / "alias-a"
    assert (model_input_dir / "alias-a.tsd.forc").exists()
    assert (model_input_dir / "forcing_002.csv").exists()
    assert (model_input_dir / "forcing_003.csv").exists()


def test_runtime_direct_grid_stages_only_package_manifest_allowlist_and_ignores_sidecar(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(2, 3),
    )
    forcing_sidecar = (
        object_root
        / "forcing"
        / "gfs"
        / "2026050100"
        / "basin_v01"
        / "demo_model"
        / "alias-a.sp.att"
    )
    forcing_sidecar.write_text(
        "1\n"
        "ID\tA\tB\tC\tFORC\n"
        "1\t0\t0\t0\t1\n",
        encoding="utf-8",
    )
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    model_input_dir = input_dir / "alias-a"
    staged_sp_att = (model_input_dir / "alias-a.sp.att").read_text(encoding="utf-8")
    assert "1\t0\t0\t0\t2" in staged_sp_att
    assert "2\t0\t0\t0\t3" in staged_sp_att
    assert not staged_sp_att.startswith("1\n")


def test_runtime_direct_grid_oversized_manifest_tsd_uses_bounded_object_path_before_checksum(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(2, 3),
    )
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    tsd_path = forcing_dir / "shud" / "qhh.tsd.forc"
    oversized_tsd = (
        b"1 20260501\n/data\nID Lon Lat X Y Z Filename\n"
        + b"1 100 30 1 1 1 forcing.csv\n" * 400_000
    )
    tsd_path.write_bytes(oversized_tsd)
    package_manifest_path = forcing_dir / "forcing_package.json"
    package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
    package_manifest["files"][0]["checksum"] = sha256_bytes(oversized_tsd)
    manifest_content = json_bytes(package_manifest)
    package_manifest_path.write_bytes(manifest_content)
    checksums["manifest_checksum"] = sha256_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    config = SHUDRuntimeConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        shud_executable=str(Path("tests/mock_shud_omp.py").resolve()),
        output_interval_minutes=1440,
        timeout_seconds=30,
    )
    tracking_store = _ChecksumTrackingObjectStore(
        LocalObjectStore(config.object_store_root, config.object_store_prefix)
    )
    runtime = SHUDRuntime(config=config, repository=repository, object_store=tracking_store)
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    tsd_uri = "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/qhh.tsd.forc"
    assert exc_info.value.error_code == "DIRECT_GRID_TSD_FORC_TOO_LARGE"
    assert tsd_uri not in tracking_store.checksum_calls
    assert tracking_store.read_bytes_limited_calls[-1] == (tsd_uri, 8 * 1024 * 1024)
    assert not (input_dir / "alias-a" / "shud" / "qhh.tsd.forc").exists()


def test_runtime_direct_grid_manifest_station_csv_uses_limited_checksum_not_full_checksum(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(2, 3),
    )
    repository = FakeHydroRunRepository()
    config = SHUDRuntimeConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        shud_executable=str(Path("tests/mock_shud_omp.py").resolve()),
        output_interval_minutes=1440,
        timeout_seconds=30,
    )
    tracking_store = _ChecksumTrackingObjectStore(
        LocalObjectStore(config.object_store_root, config.object_store_prefix)
    )
    runtime = SHUDRuntime(config=config, repository=repository, object_store=tracking_store)
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    csv_uri = "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/forcing_002.csv"
    assert csv_uri not in tracking_store.checksum_calls
    assert (csv_uri, 8 * 1024 * 1024) in tracking_store.checksum_limited_calls


def test_runtime_neutral_package_manifest_with_outer_direct_grid_fails_before_forcing_staging(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["forcing"]["forcing_mapping_mode"] = "direct_grid"
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "FORCING_PACKAGE_MAPPING_MODE_MISSING"
    model_input_dir = input_dir / "alias-a"
    assert not (model_input_dir / "shud" / "qhh.tsd.forc").exists()
    assert not (model_input_dir / "alias-a.tsd.forc").exists()


def test_runtime_direct_grid_accepts_producer_manifest_top_level_files_without_relative_path(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(2, 3),
    )
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    tsd_forc = forcing_dir / "forcing.tsd.forc"
    csv_debug = forcing_dir / "forcing_debug.csv"
    tsd_forc.write_text("forcing\n", encoding="utf-8")
    csv_debug.write_text(
        "valid_time,variable,value\n"
        "2026-05-01T00:00:00Z,PRCP,1\n",
        encoding="utf-8",
    )
    package_manifest_path = forcing_dir / "forcing_package.json"
    package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
    package_manifest["files"] = [
        {
            "role": "tsd_forc",
            "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/forcing.tsd.forc",
            "checksum": sha256_bytes(tsd_forc.read_bytes()),
        },
        {
            "role": "csv_debug",
            "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/forcing_debug.csv",
            "checksum": sha256_bytes(csv_debug.read_bytes()),
        },
        *package_manifest["files"],
    ]
    manifest_content = json_bytes(package_manifest)
    package_manifest_path.write_bytes(manifest_content)
    checksums["manifest_checksum"] = sha256_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    model_input_dir = input_dir / "alias-a"
    assert not (model_input_dir / "forcing.tsd.forc").exists()
    assert not (model_input_dir / "forcing_debug.csv").exists()
    assert (model_input_dir / "alias-a.tsd.forc").exists()
    assert (model_input_dir / "forcing_002.csv").exists()
    assert (model_input_dir / "forcing_003.csv").exists()


def test_runtime_direct_grid_oversized_package_manifest_uses_bounded_read_before_checksum(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(2, 3),
    )
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    package_manifest_path = forcing_dir / "forcing_package.json"
    oversized_manifest = (
        b'{"lineage":{"forcing_mapping_mode":"direct_grid"},"files":['
        + b'{"relative_path":"shud/qhh.tsd.forc","uri":"s3://nhms/example","checksum":"0"},' * 250_000
        + b"{}]}"
    )
    package_manifest_path.write_bytes(oversized_manifest)
    checksums["manifest_checksum"] = sha256_bytes(oversized_manifest)
    repository = FakeHydroRunRepository()
    config = SHUDRuntimeConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        shud_executable=str(Path("tests/mock_shud_omp.py").resolve()),
        output_interval_minutes=1440,
        timeout_seconds=30,
    )
    tracking_store = _ChecksumTrackingObjectStore(
        LocalObjectStore(config.object_store_root, config.object_store_prefix)
    )
    runtime = SHUDRuntime(config=config, repository=repository, object_store=tracking_store)
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "FORCING_PACKAGE_MANIFEST_READ_FAILED"
    assert checksums["manifest_uri"] not in tracking_store.checksum_calls
    assert tracking_store.read_bytes_limited_calls[-1] == (checksums["manifest_uri"], 16 * 1024 * 1024)
    assert not (input_dir / "alias-a" / "shud" / "qhh.tsd.forc").exists()


def test_runtime_direct_grid_package_manifest_tsd_checksum_mismatch_fails_before_staged_status(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(2, 3),
    )
    tsd_path = (
        object_root
        / "forcing"
        / "gfs"
        / "2026050100"
        / "basin_v01"
        / "demo_model"
        / "shud"
        / "qhh.tsd.forc"
    )
    tsd_path.write_text(tsd_path.read_text(encoding="utf-8") + "# stale mutation\n", encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    assert exc_info.value.error_code == "FORCING_FILE_CHECKSUM_MISMATCH"
    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "FORCING_FILE_CHECKSUM_MISMATCH"


def test_runtime_direct_grid_package_manifest_station_csv_checksum_mismatch_fails_before_staged_status(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(2, 3),
    )
    csv_path = (
        object_root
        / "forcing"
        / "gfs"
        / "2026050100"
        / "basin_v01"
        / "demo_model"
        / "shud"
        / "forcing_002.csv"
    )
    csv_path.write_text(csv_path.read_text(encoding="utf-8") + "1\t99\t2\t3\t4\t5\n", encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    assert exc_info.value.error_code == "FORCING_FILE_CHECKSUM_MISMATCH"
    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "FORCING_FILE_CHECKSUM_MISMATCH"


@pytest.mark.parametrize(
    ("header", "expected_missing", "expected_extra"),
    [
        ("Time_Day\tPrecip\tTemp\tRH\tWind\n", "RN", None),
        ("Time_Day\tPrecip\tTemp\tRH\tWind\tRN\tPress\n", None, "Press"),
    ],
)
def test_runtime_direct_grid_station_csv_header_contract_fails_before_staged_status(
    tmp_path: Path,
    header: str,
    expected_missing: str | None,
    expected_extra: str | None,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(2, 3),
    )
    csv_path = (
        object_root
        / "forcing"
        / "gfs"
        / "2026050100"
        / "basin_v01"
        / "demo_model"
        / "shud"
        / "forcing_002.csv"
    )
    csv_content = "2\t6\t20260501\t20260501\n" + header + "0\t2\t2\t3\t4\t5\n"
    csv_path.write_text(csv_content, encoding="utf-8")
    package_manifest_path = (
        object_root
        / "forcing"
        / "gfs"
        / "2026050100"
        / "basin_v01"
        / "demo_model"
        / "forcing_package.json"
    )
    package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
    for file_entry in package_manifest["files"]:
        if file_entry["relative_path"] == "shud/forcing_002.csv":
            file_entry["checksum"] = sha256_bytes(csv_content.encode("utf-8"))
    manifest_content = json_bytes(package_manifest)
    package_manifest_path.write_bytes(manifest_content)
    checksums["manifest_checksum"] = sha256_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    assert exc_info.value.error_code == "SHUD_FORCING_CSV_HEADER_INVALID"
    if expected_missing is not None:
        assert expected_missing in exc_info.value.message
    if expected_extra is not None:
        assert expected_extra in exc_info.value.message
    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "SHUD_FORCING_CSV_HEADER_INVALID"


def test_runtime_direct_grid_standard_package_stages_multi_station_without_sp_att_rewrite(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    (
        object_root
        / "models"
        / "basins_basin_a_shud"
        / "vbasins-test"
        / "package"
        / "alias-a.sp.att"
    ).write_text(
        "2\n"
        "ID\tA\tB\tC\tFORC\n"
        "1\t0\t0\t0\t1\n"
        "2\t0\t0\t0\t2\n",
        encoding="utf-8",
    )
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid", "spatial_mapping_method": "direct_grid"},
        station_ids=(1, 2),
    )
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    model_input_dir = input_dir / "alias-a"
    sp_att = (model_input_dir / "alias-a.sp.att").read_text(encoding="utf-8")
    tsd_forc = (model_input_dir / "alias-a.tsd.forc").read_text(encoding="utf-8")
    assert "\t2\n" in sp_att
    assert "1\t0\t0\t0\t1" in sp_att
    assert "1\t100\t30\t1\t1\t1\tforcing.csv" in tsd_forc
    assert "2\t100\t30\t2\t1\t1\tforcing_002.csv" in tsd_forc
    assert (model_input_dir / "forcing.csv").exists()
    assert (model_input_dir / "forcing_002.csv").exists()


def test_runtime_direct_grid_missing_standard_forcing_fails_without_sp_att_rewrite(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    _write_forcing(object_root)
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    (forcing_dir / "forcing_debug.csv").write_text(
        "valid_time,variable,value\n"
        "2026-05-01T00:00:00Z,PRCP,1\n"
        "2026-05-01T00:00:00Z,TEMP,2\n",
        encoding="utf-8",
    )
    package_manifest = {
        "lineage": {"forcing_mapping_mode": "direct_grid"},
        "files": [],
    }
    manifest_content = json_bytes(package_manifest)
    (forcing_dir / "forcing_package.json").write_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(
        {
            "manifest_uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/forcing_package.json",
            "manifest_checksum": sha256_bytes(manifest_content),
            "tsd_checksum": "",
            "csv_checksum": "",
        }
    )
    manifest["forcing"]["files"] = []
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "DIRECT_GRID_STANDARD_SHUD_FORCING_MISSING"
    assert "\t2\n" in (input_dir / "alias-a" / "alias-a.sp.att").read_text(encoding="utf-8")
    assert "\t1\n" not in (input_dir / "alias-a" / "alias-a.sp.att").read_text(encoding="utf-8")


def test_runtime_direct_grid_package_manifest_overrides_stale_outer_idw(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    _write_forcing(object_root)
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    (forcing_dir / "forcing_debug.csv").write_text(
        "valid_time,variable,value\n"
        "2026-05-01T00:00:00Z,PRCP,1\n"
        "2026-05-01T00:00:00Z,TEMP,2\n",
        encoding="utf-8",
    )
    package_manifest = {"lineage": {"forcing_mapping_mode": "direct_grid"}, "files": []}
    manifest_content = json_bytes(package_manifest)
    (forcing_dir / "forcing_package.json").write_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(
        {
            "manifest_uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/forcing_package.json",
            "manifest_checksum": sha256_bytes(manifest_content),
            "tsd_checksum": "",
            "csv_checksum": "",
        }
    )
    manifest["forcing"]["files"] = []
    manifest["forcing"]["forcing_mapping_mode"] = "idw"
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "DIRECT_GRID_STANDARD_SHUD_FORCING_MISSING"
    sp_att = (input_dir / "alias-a" / "alias-a.sp.att").read_text(encoding="utf-8")
    assert "2\t0\t0\t0\t3" in sp_att
    assert "2\t0\t0\t0\t1" not in sp_att


def test_runtime_direct_grid_invalid_package_manifest_fails_closed_without_sp_att_rewrite(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    _write_forcing(object_root)
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    (forcing_dir / "forcing_debug.csv").write_text(
        "valid_time,variable,value\n"
        "2026-05-01T00:00:00Z,PRCP,1\n"
        "2026-05-01T00:00:00Z,TEMP,2\n",
        encoding="utf-8",
    )
    bad_bytes = b"{ this is : not, valid json"
    (forcing_dir / "forcing_package.json").write_bytes(bad_bytes)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(
        {
            "manifest_uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/forcing_package.json",
            "manifest_checksum": sha256_bytes(bad_bytes),
            "tsd_checksum": "",
            "csv_checksum": "",
        }
    )
    manifest["forcing"]["files"] = []
    manifest["forcing"]["forcing_mapping_mode"] = "idw"
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "FORCING_PACKAGE_MANIFEST_INVALID"
    sp_att = (input_dir / "alias-a" / "alias-a.sp.att").read_text(encoding="utf-8")
    assert "2\t0\t0\t0\t3" in sp_att
    assert "2\t0\t0\t0\t1" not in sp_att


def test_runtime_direct_grid_unreadable_package_manifest_fails_closed_without_sp_att_rewrite(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    _write_forcing(object_root)
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    (forcing_dir / "forcing_debug.csv").write_text(
        "valid_time,variable,value\n"
        "2026-05-01T00:00:00Z,PRCP,1\n"
        "2026-05-01T00:00:00Z,TEMP,2\n",
        encoding="utf-8",
    )
    package_manifest = {"lineage": {"forcing_mapping_mode": "direct_grid"}, "files": []}
    manifest_content = json_bytes(package_manifest)
    (forcing_dir / "forcing_package.json").write_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    config = SHUDRuntimeConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        shud_executable=str(Path("tests/mock_shud_omp.py").resolve()),
        output_interval_minutes=1440,
        timeout_seconds=30,
    )
    inner_store = LocalObjectStore(config.object_store_root, config.object_store_prefix)
    failing_uri = "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/forcing_package.json"
    runtime = SHUDRuntime(
        config=config,
        repository=repository,
        object_store=_ReadLimitFailingObjectStore(inner_store, failing_uri),
    )
    manifest = _shud_project_manifest_with_forcing_checksums(
        {
            "manifest_uri": failing_uri,
            "manifest_checksum": sha256_bytes(manifest_content),
            "tsd_checksum": "",
            "csv_checksum": "",
        }
    )
    manifest["forcing"]["files"] = []
    manifest["forcing"]["forcing_mapping_mode"] = "idw"
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "FORCING_PACKAGE_MANIFEST_READ_FAILED"
    sp_att = (input_dir / "alias-a" / "alias-a.sp.att").read_text(encoding="utf-8")
    assert "2\t0\t0\t0\t3" in sp_att
    assert "2\t0\t0\t0\t1" not in sp_att


def test_runtime_direct_grid_sp_att_forc_out_of_tsd_id_set_fails_before_staged_status(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"spatial_mapping_method": "direct_grid"},
        station_ids=(1, 2),
    )
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    assert exc_info.value.error_code == "DIRECT_GRID_FORCING_OWNERSHIP_RANGE"
    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "DIRECT_GRID_FORCING_OWNERSHIP_RANGE"


def test_runtime_direct_grid_sp_att_long_line_fails_before_staged_status(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    (
        object_root
        / "models"
        / "basins_basin_a_shud"
        / "vbasins-test"
        / "package"
        / "alias-a.sp.att"
    ).write_text(
        "2\n"
        "ID\tA\tB\tC\tFORC\n"
        f"1\t{'0' * (64 * 1024 + 1)}\t0\t0\t1\n",
        encoding="utf-8",
    )
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1,),
    )
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    assert exc_info.value.error_code == "DIRECT_GRID_SP_ATT_LINE_TOO_LONG"
    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "DIRECT_GRID_SP_ATT_LINE_TOO_LONG"


def test_runtime_direct_grid_tsd_forc_too_large_fails_before_staged_status(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1,),
    )
    tsd_path = (
        object_root
        / "forcing"
        / "gfs"
        / "2026050100"
        / "basin_v01"
        / "demo_model"
        / "shud"
        / "qhh.tsd.forc"
    )
    tsd_bytes = b"1 20260501\n/data\nID Lon Lat X Y Z Filename\n" + b"1 100 30 1 1 1 forcing.csv\n" * 400_000
    tsd_path.write_bytes(tsd_bytes)
    checksums["tsd_checksum"] = sha256_bytes(tsd_bytes)
    manifest_path = (
        object_root
        / "forcing"
        / "gfs"
        / "2026050100"
        / "basin_v01"
        / "demo_model"
        / "forcing_package.json"
    )
    package_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    package_manifest["files"][0]["checksum"] = checksums["tsd_checksum"]
    manifest_content = json_bytes(package_manifest)
    manifest_path.write_bytes(manifest_content)
    checksums["manifest_checksum"] = sha256_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    assert exc_info.value.error_code == "DIRECT_GRID_TSD_FORC_TOO_LARGE"
    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "DIRECT_GRID_TSD_FORC_TOO_LARGE"


def test_runtime_direct_grid_oversized_tsd_directory_member_is_bounded_before_unbounded_read(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1,),
    )
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    tsd_path = forcing_dir / "shud" / "qhh.tsd.forc"
    oversized_tsd = (
        b"1 20260501\n/data\nID Lon Lat X Y Z Filename\n"
        + b"1 100 30 1 1 1 forcing.csv\n" * 400_000
    )
    tsd_path.write_bytes(oversized_tsd)
    package_manifest_path = forcing_dir / "forcing_package.json"
    package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
    package_manifest["files"][0]["checksum"] = sha256_bytes(oversized_tsd)
    manifest_content = json_bytes(package_manifest)
    package_manifest_path.write_bytes(manifest_content)
    checksums["manifest_checksum"] = sha256_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    config = SHUDRuntimeConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        shud_executable=str(Path("tests/mock_shud_omp.py").resolve()),
        output_interval_minutes=1440,
        timeout_seconds=30,
    )
    runtime = _UnboundedSensitiveReadFailingRuntime(
        config=config,
        repository=repository,
        object_store=LocalObjectStore(config.object_store_root, config.object_store_prefix),
        sensitive_name="qhh.tsd.forc",
    )
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "DIRECT_GRID_TSD_FORC_TOO_LARGE"
    assert runtime.unbounded_sensitive_reads == []
    assert not (input_dir / "alias-a" / "shud" / "qhh.tsd.forc").exists()


def test_runtime_legacy_standard_shud_forcing_reader_allows_direct_grid_sized_tsd(tmp_path: Path) -> None:
    from workers.shud_runtime.runtime import _read_shud_forcing_station_rows

    shud_dir = tmp_path / "shud"
    shud_dir.mkdir()
    tsd_path = shud_dir / "qhh.tsd.forc"
    tsd_path.write_bytes(
        b"1 20260501\n/data\nID Lon Lat X Y Z Filename\n"
        + b"1 100 30 1 1 1 forcing.csv\n" * 400_000
    )

    with pytest.raises(SHUDRuntimeError) as exc_info:
        _read_shud_forcing_station_rows(tsd_path, is_direct_grid=True)

    assert exc_info.value.error_code == "DIRECT_GRID_TSD_FORC_TOO_LARGE"
    assert _read_shud_forcing_station_rows(tsd_path, is_direct_grid=False)[0]["filename"] == "forcing.csv"


def test_runtime_direct_grid_first_station_csv_too_large_fails_before_staged_status(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1,),
    )
    csv_path = (
        object_root
        / "forcing"
        / "gfs"
        / "2026050100"
        / "basin_v01"
        / "demo_model"
        / "shud"
        / "forcing.csv"
    )
    csv_bytes = (
        b"2\t6\t20260501\t20260501\n"
        b"Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        + b"0\t1\t2\t3\t4\t5\n" * 700_000
    )
    csv_path.write_bytes(csv_bytes)
    checksums["csv_checksum"] = sha256_bytes(csv_bytes)
    manifest_path = (
        object_root
        / "forcing"
        / "gfs"
        / "2026050100"
        / "basin_v01"
        / "demo_model"
        / "forcing_package.json"
    )
    package_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    package_manifest["files"][1]["checksum"] = checksums["csv_checksum"]
    manifest_content = json_bytes(package_manifest)
    manifest_path.write_bytes(manifest_content)
    checksums["manifest_checksum"] = sha256_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    assert exc_info.value.error_code == "DIRECT_GRID_FORCING_CSV_TOO_LARGE"
    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "DIRECT_GRID_FORCING_CSV_TOO_LARGE"


def test_runtime_direct_grid_oversized_csv_directory_member_is_bounded_before_unbounded_read(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1,),
    )
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    csv_path = forcing_dir / "shud" / "forcing.csv"
    oversized_csv = (
        b"2\t6\t20260501\t20260501\n"
        b"Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        + b"0\t1\t2\t3\t4\t5\n" * 700_000
    )
    csv_path.write_bytes(oversized_csv)
    package_manifest_path = forcing_dir / "forcing_package.json"
    package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
    package_manifest["files"][1]["checksum"] = sha256_bytes(oversized_csv)
    manifest_content = json_bytes(package_manifest)
    package_manifest_path.write_bytes(manifest_content)
    checksums["manifest_checksum"] = sha256_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    config = SHUDRuntimeConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        shud_executable=str(Path("tests/mock_shud_omp.py").resolve()),
        output_interval_minutes=1440,
        timeout_seconds=30,
    )
    runtime = _UnboundedSensitiveReadFailingRuntime(
        config=config,
        repository=repository,
        object_store=LocalObjectStore(config.object_store_root, config.object_store_prefix),
        sensitive_name="forcing.csv",
    )
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "DIRECT_GRID_FORCING_CSV_TOO_LARGE"
    assert runtime.unbounded_sensitive_reads == []
    assert not (input_dir / "alias-a" / "shud" / "forcing.csv").exists()


def test_runtime_direct_grid_tar_forcing_package_fails_closed(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1,),
    )
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    tar_path = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model.tar"
    with tarfile.open(tar_path, "w") as archive:
        for file_path in sorted(path for path in forcing_dir.rglob("*") if path.is_file()):
            archive.add(file_path, arcname=file_path.relative_to(forcing_dir).as_posix())
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))
    manifest["forcing"]["forcing_uri"] = "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model.tar"

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    assert exc_info.value.error_code == "DIRECT_GRID_FORCING_TAR_UNSUPPORTED"
    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "DIRECT_GRID_FORCING_TAR_UNSUPPORTED"


def test_runtime_direct_grid_checksum_cap_uses_package_manifest_when_outer_metadata_is_idw(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1,),
    )
    tsd_path = (
        object_root
        / "forcing"
        / "gfs"
        / "2026050100"
        / "basin_v01"
        / "demo_model"
        / "shud"
        / "qhh.tsd.forc"
    )
    tsd_bytes = b"1 20260501\n/data\nID Lon Lat X Y Z Filename\n" + b"1 100 30 1 1 1 forcing.csv\n" * 400_000
    tsd_path.write_bytes(tsd_bytes)
    checksums["tsd_checksum"] = sha256_bytes(tsd_bytes)
    manifest_path = (
        object_root
        / "forcing"
        / "gfs"
        / "2026050100"
        / "basin_v01"
        / "demo_model"
        / "forcing_package.json"
    )
    package_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    package_manifest["files"][0]["checksum"] = checksums["tsd_checksum"]
    manifest_content = json_bytes(package_manifest)
    manifest_path.write_bytes(manifest_content)
    checksums["manifest_checksum"] = sha256_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["forcing"]["forcing_mapping_mode"] = "idw"

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    assert exc_info.value.error_code == "DIRECT_GRID_TSD_FORC_TOO_LARGE"
    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "DIRECT_GRID_TSD_FORC_TOO_LARGE"


def test_runtime_direct_grid_checksum_cap_fails_during_checksum_verification(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1,),
    )
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["forcing"]["forcing_mapping_mode"] = "idw"
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    staged_shud_dir = input_dir / "shud"
    staged_shud_dir.mkdir(parents=True)
    oversized_tsd = (
        b"1 20260501\n/data\nID Lon Lat X Y Z Filename\n"
        + b"1 100 30 1 1 1 forcing.csv\n" * 400_000
    )
    (staged_shud_dir / "qhh.tsd.forc").write_bytes(oversized_tsd)
    (staged_shud_dir / "forcing.csv").write_text(
        "2\t6\t20260501\t20260501\n"
        "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        "0\t1\t2\t3\t4\t5\n",
        encoding="utf-8",
    )
    manifest["forcing"]["files"][0]["checksum"] = sha256_bytes(oversized_tsd)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime._verify_staged_forcing_checksums(manifest, input_dir)

    assert exc_info.value.error_code == "DIRECT_GRID_TSD_FORC_TOO_LARGE"
    assert not (input_dir / "alias-a.tsd.forc").exists()


def test_runtime_direct_grid_checksum_cap_uses_normalized_staged_tsd_path(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1,),
    )
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    staged_shud_dir = input_dir / "shud"
    staged_shud_dir.mkdir(parents=True)
    oversized_tsd = (
        b"1 20260501\n/data\nID Lon Lat X Y Z Filename\n"
        + b"1 100 30 1 1 1 forcing.csv\n" * 400_000
    )
    (staged_shud_dir / "qhh.tsd.forc").write_bytes(oversized_tsd)
    (staged_shud_dir / "forcing.csv").write_text(
        "2\t6\t20260501\t20260501\n"
        "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        "0\t1\t2\t3\t4\t5\n",
        encoding="utf-8",
    )
    manifest["forcing"]["files"][0]["relative_path"] = "./shud/qhh.tsd.forc"
    manifest["forcing"]["files"][0]["checksum"] = sha256_bytes(oversized_tsd)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime._verify_staged_forcing_checksums(manifest, input_dir)

    assert exc_info.value.error_code == "DIRECT_GRID_TSD_FORC_TOO_LARGE"


def test_runtime_direct_grid_station_csv_checksum_is_bounded(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1,),
    )
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    staged_shud_dir = input_dir / "shud"
    staged_shud_dir.mkdir(parents=True)
    (staged_shud_dir / "qhh.tsd.forc").write_text(
        "1 20260501\n"
        "/data\n"
        "ID\tLon\tLat\tX\tY\tZ\tFilename\n"
        "1\t100\t30\t1\t1\t1\tforcing.csv\n",
        encoding="utf-8",
    )
    oversized_csv = (
        b"2\t6\t20260501\t20260501\n"
        b"Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        + b"0\t1\t2\t3\t4\t5\n" * 700_000
    )
    (staged_shud_dir / "forcing.csv").write_bytes(oversized_csv)
    manifest["forcing"]["files"][1]["checksum"] = sha256_bytes(oversized_csv)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime._verify_staged_forcing_checksums(manifest, input_dir)

    assert exc_info.value.error_code == "DIRECT_GRID_FORCING_CSV_TOO_LARGE"


def test_runtime_direct_grid_non_first_station_csv_copy_is_bounded(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1, 2),
    )
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    shud_dir = forcing_dir / "shud"
    first_csv_bytes = (shud_dir / "forcing.csv").read_bytes()
    oversized_csv = (
        b"2\t6\t20260501\t20260501\n"
        b"Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        + b"0\t2\t2\t3\t4\t5\n" * 700_000
    )
    (shud_dir / "forcing_002.csv").write_bytes(oversized_csv)
    package_manifest_path = forcing_dir / "forcing_package.json"
    package_manifest = json.loads(package_manifest_path.read_text(encoding="utf-8"))
    for file_entry in package_manifest["files"]:
        if file_entry["relative_path"] == "shud/forcing_002.csv":
            file_entry["checksum"] = sha256_bytes(oversized_csv)
    manifest_content = json_bytes(package_manifest)
    package_manifest_path.write_bytes(manifest_content)
    checksums["manifest_checksum"] = sha256_bytes(manifest_content)
    checksums["csv_checksum"] = sha256_bytes(first_csv_bytes)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "DIRECT_GRID_FORCING_CSV_TOO_LARGE"
    assert not (input_dir / "alias-a" / "forcing.csv").exists()
    assert not (input_dir / "alias-a" / "forcing_002.csv").exists()


def test_runtime_direct_grid_station_filename_collision_fails_without_overwriting_sp_att(tmp_path: Path) -> None:
    non_project_input_dir = tmp_path / "input-dir-not-named-for-project"
    non_project_input_dir.mkdir()
    with pytest.raises(SHUDRuntimeError) as helper_exc_info:
        _validate_direct_grid_station_filename_target(
            non_project_input_dir / "alias-a.sp.att",
            model_input_dir=non_project_input_dir,
            project_name="alias-a",
        )
    assert helper_exc_info.value.error_code == "DIRECT_GRID_STATION_FILENAME_COLLISION"

    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1,),
    )
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    shud_dir = forcing_dir / "shud"
    (shud_dir / "qhh.tsd.forc").write_text(
        "1 20260501\n"
        "/data\n"
        "ID\tLon\tLat\tX\tY\tZ\tFilename\n"
        "1\t100\t30\t1\t1\t1\talias-a.sp.att\n",
        encoding="utf-8",
    )
    collision_bytes = (
        b"2\t6\t20260501\t20260501\n"
        b"Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        b"0\t1\t2\t3\t4\t5\n"
    )
    (shud_dir / "alias-a.sp.att").write_bytes(collision_bytes)
    (shud_dir / "forcing.csv").unlink()
    checksums["tsd_checksum"] = sha256_bytes((shud_dir / "qhh.tsd.forc").read_bytes())
    checksums["csv_checksum"] = sha256_bytes(collision_bytes)
    manifest_path = forcing_dir / "forcing_package.json"
    package_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    package_manifest["files"][0]["checksum"] = checksums["tsd_checksum"]
    package_manifest["files"][1] = {
        **package_manifest["files"][1],
        "relative_path": "shud/alias-a.sp.att",
        "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/alias-a.sp.att",
        "checksum": checksums["csv_checksum"],
    }
    manifest_content = json_bytes(package_manifest)
    manifest_path.write_bytes(manifest_content)
    checksums["manifest_checksum"] = sha256_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["forcing"]["files"][1]["relative_path"] = "shud/alias-a.sp.att"
    manifest["forcing"]["files"][1]["uri"] = "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/alias-a.sp.att"

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    model_sp_att = (
        tmp_path
        / "workspace"
        / "runs"
        / manifest["run_id"]
        / "input"
        / "alias-a"
        / "alias-a.sp.att"
    )
    assert exc_info.value.error_code == "DIRECT_GRID_STATION_FILENAME_INVALID"
    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "DIRECT_GRID_STATION_FILENAME_INVALID"
    assert "2\t0\t0\t0\t3" in model_sp_att.read_text(encoding="utf-8")


def test_runtime_direct_grid_rejects_non_csv_station_filename_before_unbounded_member_read(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1,),
    )
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    shud_dir = forcing_dir / "shud"
    (shud_dir / "qhh.tsd.forc").write_text(
        "1 20260501\n"
        "/data\n"
        "ID\tLon\tLat\tX\tY\tZ\tFilename\n"
        "1\t100\t30\t1\t1\t1\tforcing.dat\n",
        encoding="utf-8",
    )
    (shud_dir / "forcing.dat").write_bytes(
        b"2\t6\t20260501\t20260501\n"
        b"Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        + b"0\t1\t2\t3\t4\t5\n" * 700_000
    )
    manifest_path = forcing_dir / "forcing_package.json"
    package_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    package_manifest["files"] = [
        {
            "role": "shud_forcing",
            "relative_path": "shud/qhh.tsd.forc",
            "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/qhh.tsd.forc",
            "checksum": sha256_bytes((shud_dir / "qhh.tsd.forc").read_bytes()),
        },
        {
            "role": "shud_forcing_csv",
            "relative_path": "shud/forcing.dat",
            "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/forcing.dat",
            "checksum": sha256_bytes((shud_dir / "forcing.dat").read_bytes()),
        },
    ]
    manifest_content = json_bytes(package_manifest)
    manifest_path.write_bytes(manifest_content)
    checksums["manifest_checksum"] = sha256_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    model_input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input" / "alias-a"
    assert exc_info.value.error_code == "DIRECT_GRID_STATION_FILENAME_INVALID"
    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "DIRECT_GRID_STATION_FILENAME_INVALID"
    assert not (model_input_dir / "forcing.dat").exists()


def test_runtime_direct_grid_rejects_directoried_station_filename_before_basename_copy(
    tmp_path: Path,
) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(
        object_root,
        lineage={"forcing_mapping_mode": "direct_grid"},
        station_ids=(1,),
    )
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    shud_dir = forcing_dir / "shud"
    (shud_dir / "subdir").mkdir()
    (shud_dir / "subdir" / "forcing.csv").write_text(
        "2\t6\t20260501\t20260501\n"
        "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        "0\t7\t2\t3\t4\t5\n",
        encoding="utf-8",
    )
    (shud_dir / "forcing.csv").write_text(
        "2\t6\t20260501\t20260501\n"
        "Time_Day\tPrecip\tTemp\tRH\tWind\tRN\n"
        "0\t1\t2\t3\t4\t5\n",
        encoding="utf-8",
    )
    (shud_dir / "qhh.tsd.forc").write_text(
        "1 20260501\n"
        "/data\n"
        "ID\tLon\tLat\tX\tY\tZ\tFilename\n"
        "1\t100\t30\t1\t1\t1\tsubdir/forcing.csv\n",
        encoding="utf-8",
    )
    manifest_path = forcing_dir / "forcing_package.json"
    package_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    package_manifest["files"] = [
        {
            "role": "shud_forcing",
            "relative_path": "shud/qhh.tsd.forc",
            "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/qhh.tsd.forc",
            "checksum": sha256_bytes((shud_dir / "qhh.tsd.forc").read_bytes()),
        },
        {
            "role": "shud_forcing_csv",
            "relative_path": "shud/forcing.csv",
            "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/forcing.csv",
            "checksum": sha256_bytes((shud_dir / "forcing.csv").read_bytes()),
        },
        {
            "role": "shud_forcing_csv",
            "relative_path": "shud/subdir/forcing.csv",
            "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/subdir/forcing.csv",
            "checksum": sha256_bytes((shud_dir / "subdir" / "forcing.csv").read_bytes()),
        },
    ]
    manifest_content = json_bytes(package_manifest)
    manifest_path.write_bytes(manifest_content)
    checksums["manifest_checksum"] = sha256_bytes(manifest_content)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _drop_runtime_forcing_files(_shud_project_manifest_with_forcing_checksums(checksums))

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    model_input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input" / "alias-a"
    assert exc_info.value.error_code == "DIRECT_GRID_STATION_FILENAME_INVALID"
    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "DIRECT_GRID_STATION_FILENAME_INVALID"
    assert not (model_input_dir / "shud" / "forcing.csv").exists()
    assert not (model_input_dir / "forcing.csv").exists()


def test_runtime_legacy_non_direct_grid_fallback_rewrites_sp_att_to_single_forcing_id(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    _write_forcing(object_root)
    forcing_dir = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    (forcing_dir / "forcing_debug.csv").write_text(
        "valid_time,variable,value\n"
        "2026-05-01T00:00:00Z,PRCP,1\n"
        "2026-05-01T00:00:00Z,TEMP,2\n",
        encoding="utf-8",
    )
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(
        {
            "manifest_uri": "",
            "manifest_checksum": "",
            "tsd_checksum": "",
            "csv_checksum": "",
        }
    )
    manifest["forcing"].pop("package_manifest_uri")
    manifest["forcing"].pop("package_manifest_checksum")
    manifest["forcing"]["files"] = []
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    sp_att = (input_dir / "alias-a" / "alias-a.sp.att").read_text(encoding="utf-8")
    assert "1\t0\t0\t0\t1" in sp_att
    assert "2\t0\t0\t0\t1" in sp_att
    assert (input_dir / "alias-a" / "alias-a.tsd.forc").exists()


def test_runtime_staging_keeps_standard_shud_forcing_time_axis_relative_to_cfg_start(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    output_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)
    cfg_path = runtime.generate_cfg_para(manifest, input_dir, output_dir)

    cfg_values = dict(
        line.split(maxsplit=1)
        for line in cfg_path.read_text(encoding="utf-8").splitlines()
        if line.split(maxsplit=1)[0] in {"START", "END"}
    )
    forcing_rows = (input_dir / "alias-a" / "forcing.csv").read_text(encoding="utf-8").splitlines()
    first_time_day = float(forcing_rows[2].split()[0])
    last_time_day = float(forcing_rows[-1].split()[0])

    assert float(cfg_values["START"]) == pytest.approx(0.0)
    assert float(cfg_values["END"]) == pytest.approx(3.0)
    assert first_time_day == pytest.approx(0.0)
    assert first_time_day <= last_time_day < float(cfg_values["END"])


def test_runtime_staging_interprets_standard_shud_time_day_relative_to_12z_start(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    package = object_root / "models" / "basins_basin_a_shud" / "vbasins-test" / "package"
    (package / "alias-a.cfg.ic").write_text("2\t6\t0.000000\n1\t2\n", encoding="utf-8")
    checksums = _write_standard_shud_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["cycle_time"] = "2026-05-01T12:00:00Z"
    manifest["start_time"] = "2026-05-01T12:00:00Z"
    manifest["end_time"] = "2026-05-04T12:00:00Z"
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    cfg_ic_header = (input_dir / "alias-a" / "alias-a.cfg.ic").read_text(encoding="utf-8").splitlines()[0]
    observed_minute = float(cfg_ic_header.split()[2])
    expected_minute = datetime(2026, 5, 1, 12, tzinfo=UTC).timestamp() / 60.0
    assert observed_minute == pytest.approx(expected_minute)


_MMDAY_UNITS = {
    "PRCP": "mm/day",
    "TEMP": "degC",
    "RH": "0-1",
    "wind": "m/s",
    "Rn": "W/m2",
    "Press": "Pa",
}


@pytest.mark.parametrize("prcp_unit", ["mm", " mm ", "MM", "kg/m2", "mm/hr"])
def test_runtime_staging_rejects_non_mmday_prcp_unit(tmp_path: Path, prcp_unit: str) -> None:
    """#270: an explicit non-mm/day PRCP unit must fail loudly at staging.

    Covers case/whitespace variants to lock the ``.strip().lower()`` normalisation:
    ``"MM"`` and ``" mm "`` are still per-step accumulations and must be rejected.
    """
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    bad_units = {**_MMDAY_UNITS, "PRCP": prcp_unit}
    checksums = _write_standard_shud_forcing(object_root, units=bad_units)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "FORCING_PRCP_UNIT_MISMATCH"
    assert "mm/day" in exc_info.value.message
    assert prcp_unit in exc_info.value.message


@pytest.mark.parametrize("prcp_unit", ["mm/day", "MM/DAY", " mm/day ", "Mm/Day"])
def test_runtime_staging_accepts_mmday_prcp_unit(tmp_path: Path, prcp_unit: str) -> None:
    """#270: a package declaring PRCP in mm/day (any case/whitespace) stages normally."""
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    units = {**_MMDAY_UNITS, "PRCP": prcp_unit}
    checksums = _write_standard_shud_forcing(object_root, units=units)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    assert (input_dir / "alias-a" / "alias-a.tsd.forc").exists()


def test_runtime_staging_tolerates_missing_unit_metadata(tmp_path: Path) -> None:
    """#270: packages without a units block (legacy) must not fail (backward compat)."""
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root, units=None)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    assert (input_dir / "alias-a" / "alias-a.tsd.forc").exists()


def test_runtime_staging_tolerates_units_block_without_prcp_key(tmp_path: Path) -> None:
    """#270: a units block lacking the PRCP key must not fail (best-effort skip)."""
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    units = {k: v for k, v in _MMDAY_UNITS.items() if k != "PRCP"}
    checksums = _write_standard_shud_forcing(object_root, units=units)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    assert (input_dir / "alias-a" / "alias-a.tsd.forc").exists()


class _ReadLimitFailingObjectStore:
    """Delegating object store whose ``read_bytes_limited`` fails for one URI.

    Used to simulate an unreadable / over-read-cap package manifest while leaving
    the separate ``checksum()`` verification intact (``LocalObjectStore`` is a
    frozen dataclass, so a delegating wrapper is cleaner than monkeypatching).
    """

    def __init__(self, inner: LocalObjectStore, failing_uri: str) -> None:
        self._inner = inner
        self._failing_uri = failing_uri

    def read_bytes_limited(self, key_or_uri: str, *, max_bytes: int) -> bytes:
        if key_or_uri == self._failing_uri:
            raise ObjectStoreError(f"Object {key_or_uri} exceeds read limit")
        return self._inner.read_bytes_limited(key_or_uri, max_bytes=max_bytes)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _ChecksumTrackingObjectStore:
    def __init__(self, inner: LocalObjectStore) -> None:
        self._inner = inner
        self.checksum_calls: list[str] = []
        self.checksum_limited_calls: list[tuple[str, int]] = []
        self.read_bytes_limited_calls: list[tuple[str, int]] = []

    def checksum(self, key_or_uri: str) -> str:
        self.checksum_calls.append(key_or_uri)
        return self._inner.checksum(key_or_uri)

    def checksum_limited(self, key_or_uri: str, *, max_bytes: int) -> str:
        self.checksum_limited_calls.append((key_or_uri, max_bytes))
        return self._inner.checksum_limited(key_or_uri, max_bytes=max_bytes)

    def read_bytes_limited(self, key_or_uri: str, *, max_bytes: int) -> bytes:
        self.read_bytes_limited_calls.append((key_or_uri, max_bytes))
        return self._inner.read_bytes_limited(key_or_uri, max_bytes=max_bytes)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _UnboundedSensitiveReadFailingRuntime(SHUDRuntime):
    def __init__(self, *args: Any, sensitive_name: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.sensitive_name = sensitive_name
        self.unbounded_sensitive_reads: list[str] = []

    def _read_object_artifact_bytes(self, source: Path, label: str) -> bytes:
        if source.name == self.sensitive_name:
            self.unbounded_sensitive_reads.append(label)
            raise AssertionError(f"unbounded sensitive read attempted for {source}")
        return super()._read_object_artifact_bytes(source, label)


def test_runtime_staging_fails_closed_on_unreadable_package_manifest_even_with_outer_idw(
    tmp_path: Path,
) -> None:
    """#547: package manifest authority is fail-closed once a manifest URI is supplied."""
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root, units=_MMDAY_UNITS)
    repository = FakeHydroRunRepository()

    config = SHUDRuntimeConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=object_root,
        object_store_prefix="s3://nhms",
        shud_executable=str(Path("tests/mock_shud_omp.py").resolve()),
        output_interval_minutes=1440,
        timeout_seconds=30,
    )
    inner_store = LocalObjectStore(config.object_store_root, config.object_store_prefix)
    failing_store = _ReadLimitFailingObjectStore(inner_store, checksums["manifest_uri"])
    runtime = SHUDRuntime(config=config, repository=repository, object_store=failing_store)

    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["forcing"]["forcing_mapping_mode"] = "idw"
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "FORCING_PACKAGE_MANIFEST_READ_FAILED"
    assert not (input_dir / "alias-a" / "alias-a.tsd.forc").exists()


def test_runtime_staging_fails_closed_on_invalid_package_manifest_even_with_outer_idw(tmp_path: Path) -> None:
    """#547: stale outer IDW metadata cannot mask an invalid authoritative manifest."""
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root, units=_MMDAY_UNITS)

    # Overwrite the on-disk package manifest with malformed JSON and re-point the
    # manifest checksum at those bytes so checksum verification still succeeds.
    bad_bytes = b"{ this is : not, valid json"
    manifest_path = (
        object_root
        / "forcing"
        / "gfs"
        / "2026050100"
        / "basin_v01"
        / "demo_model"
        / "forcing_package.json"
    )
    manifest_path.write_bytes(bad_bytes)
    checksums["manifest_checksum"] = sha256_bytes(bad_bytes)

    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["forcing"]["forcing_mapping_mode"] = "idw"
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "FORCING_PACKAGE_MANIFEST_INVALID"
    assert not (input_dir / "alias-a" / "alias-a.tsd.forc").exists()


@pytest.mark.parametrize(
    "relative_path",
    ["../qhh.tsd.forc", "shud/../qhh.tsd.forc", "/tmp/qhh.tsd.forc"],
)
def test_runtime_staging_rejects_forcing_relative_path_escape(tmp_path: Path, relative_path: str) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["forcing"]["files"][0]["relative_path"] = relative_path
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError, match="relative_path escapes model input directory") as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "FORCING_FILE_PATH_INVALID"
    assert repository.statuses == []


def test_runtime_staging_accepts_forcing_checksums_without_relative_path(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    for file_entry in manifest["forcing"]["files"]:
        file_entry.pop("relative_path")
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    assert (input_dir / "alias-a" / "alias-a.tsd.forc").exists()
    assert (input_dir / "alias-a" / "forcing.csv").exists()


def test_runtime_staging_rejects_forcing_checksum_symlink_relative_path(tmp_path: Path) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    target_content = b"target forcing\n"
    manifest["forcing"]["files"] = [
        {
            "role": "shud_forcing_csv",
            "relative_path": "shud/link.csv",
            "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/link.csv",
            "checksum": sha256_bytes(target_content),
        }
    ]
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    staged_dir = input_dir / "shud"
    staged_dir.mkdir(parents=True)
    (staged_dir / "target.csv").write_bytes(target_content)
    (staged_dir / "link.csv").symlink_to("target.csv")

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime._verify_staged_forcing_checksums(manifest, input_dir)

    assert exc_info.value.error_code == "FORCING_FILE_NOT_STAGED"
    assert "symlink" in exc_info.value.message


def test_runtime_staging_rejects_object_store_source_symlink_descendant(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    outside_secret = tmp_path / "outside-secret.txt"
    outside_secret.write_text("secret\n", encoding="utf-8")
    (object_root / "models" / "demo_model" / "package" / "leaked.mesh").symlink_to(outside_secret)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "ARTIFACT_UNSAFE"
    assert "symlink" in exc_info.value.message
    assert not (input_dir / "leaked.mesh").exists()


def test_runtime_staging_rejects_preexisting_destination_symlink_escape(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    outside_target = tmp_path / "outside-target.txt"
    outside_target.write_text("keep\n", encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)
    (input_dir / "demo.mesh").symlink_to(outside_target)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code == "WORKSPACE_PATH_UNSAFE"
    assert outside_target.read_text(encoding="utf-8") == "keep\n"
    assert (input_dir / "demo.mesh").is_symlink()


@pytest.mark.parametrize("member_name", ["../evil.mesh", "/tmp/evil.mesh"])
def test_runtime_tar_artifact_staging_rejects_traversal_member(tmp_path: Path, member_name: str) -> None:
    object_root = tmp_path / "object-store"
    package_tar = object_root / "models" / "demo_model" / "package.tar"
    package_tar.parent.mkdir(parents=True)
    with tarfile.open(package_tar, "w") as archive:
        payload = b"mesh\n"
        info = tarfile.TarInfo(member_name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    input_dir = tmp_path / "workspace" / "runs" / "run-a" / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime._stage_artifact("s3://nhms/models/demo_model/package.tar", input_dir)

    assert exc_info.value.error_code == "ARTIFACT_TAR_UNSAFE"
    assert not (tmp_path / "evil.mesh").exists()


def test_runtime_tar_artifact_staging_rejects_symlink_member(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    package_tar = object_root / "models" / "demo_model" / "package.tar"
    package_tar.parent.mkdir(parents=True)
    with tarfile.open(package_tar, "w") as archive:
        info = tarfile.TarInfo("leaked.mesh")
        info.type = tarfile.SYMTYPE
        info.linkname = str(tmp_path / "outside-secret.txt")
        archive.addfile(info)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    input_dir = tmp_path / "workspace" / "runs" / "run-a" / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime._stage_artifact("s3://nhms/models/demo_model/package.tar", input_dir)

    assert exc_info.value.error_code == "ARTIFACT_TAR_UNSAFE"
    assert not (input_dir / "leaked.mesh").exists()


def test_output_verification_rejects_wrong_row_count(tmp_path: Path) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "demo.rivqdown").write_text("time,seg1,seg2\n2026-05-01T00:00:00Z,1,2\n", encoding="utf-8")

    with pytest.raises(SHUDRuntimeError, match="expected 3 data rows"):
        runtime.verify_output(manifest, output_dir)


def test_upload_directory_rejects_object_target_symlink_to_workspace_file(tmp_path: Path) -> None:
    config = SHUDRuntimeConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=tmp_path / "object-store",
        object_store_prefix="s3://nhms",
        upload_retries=1,
    )
    runtime = SHUDRuntime(
        config=config,
        repository=FakeHydroRunRepository(),
        object_store=LocalObjectStore(config.object_store_root, config.object_store_prefix),
    )
    output_dir = Path(config.workspace_root) / "runs" / "run-a" / "output"
    output_dir.mkdir(parents=True)
    output_file = output_dir / "demo.rivqdown"
    output_file.write_bytes(b"workspace output\n")
    object_target = Path(config.object_store_root) / "runs" / "run-a" / "output" / "demo.rivqdown"
    object_target.parent.mkdir(parents=True)
    object_target.symlink_to(output_file)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime._upload_directory(output_dir, "runs/run-a/output")

    assert exc_info.value.error_code == "UPLOAD_FAILED"
    assert "Target file must not be a symlink" in exc_info.value.message
    assert output_file.read_bytes() == b"workspace output\n"
    assert object_target.is_symlink()


def test_workspace_failure_marks_run_failed(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_package(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()

    with pytest.raises(SHUDRuntimeError, match="Object storage artifact not found"):
        runtime.execute(manifest)

    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == "ARTIFACT_NOT_FOUND"


def test_workspace_failure_writes_task_outcome_receipt_and_mirrors_it(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_package(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    run_id = manifest["run_id"]

    with pytest.raises(SHUDRuntimeError, match="Object storage artifact not found"):
        runtime.execute(manifest)

    workspace_receipt = tmp_path / "workspace" / "runs" / run_id / "logs" / "task_outcome.json"
    assert workspace_receipt.is_file()
    payload = json.loads(workspace_receipt.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "nhms.shud_task_outcome.v1"
    assert payload["run_id"] == run_id
    assert payload["error_code"] == "ARTIFACT_NOT_FOUND"
    assert 0 < len(payload["error_message"]) <= 512
    assert payload["failed_at"].endswith("Z")

    # The receipt MUST be written before ``upload_logs`` or the object-store
    # mirror -- accounting's only trust root -- never carries it.
    mirrored_receipt = object_root / "runs" / run_id / "logs" / "task_outcome.json"
    assert mirrored_receipt.is_file()
    assert json.loads(mirrored_receipt.read_text(encoding="utf-8")) == payload


def test_task_outcome_receipt_truncates_long_error_messages(tmp_path: Path) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    log_dir = tmp_path / "workspace" / "runs" / "run-a" / "logs"
    log_dir.mkdir(parents=True)

    runtime._write_task_outcome_receipt(log_dir, "run-a", SHUDRuntimeError("ARTIFACT_NOT_FOUND", "x" * 900))

    payload = json.loads((log_dir / "task_outcome.json").read_text(encoding="utf-8"))
    assert payload["error_message"] == "x" * 512


def test_task_outcome_receipt_binds_the_slurm_array_attempt_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The reader keys the receipt on the cycle-stable ``run_id``; only this
    # binding lets it tell attempt N's leftover receipt from attempt N+1's.
    monkeypatch.setenv("SLURM_ARRAY_JOB_ID", "4000")
    monkeypatch.setenv("SLURM_ARRAY_TASK_ID", "7")
    monkeypatch.setenv("SLURM_JOB_ID", "4007")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    log_dir = tmp_path / "workspace" / "runs" / "run-a" / "logs"
    log_dir.mkdir(parents=True)

    runtime._write_task_outcome_receipt(log_dir, "run-a", SHUDRuntimeError("ARTIFACT_NOT_FOUND", "boom"))

    payload = json.loads((log_dir / "task_outcome.json").read_text(encoding="utf-8"))
    assert payload["slurm_job_id"] == "4000"
    assert payload["array_task_id"] == 7


def test_task_outcome_receipt_identity_falls_back_to_the_non_array_job_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLURM_ARRAY_JOB_ID", raising=False)
    monkeypatch.delenv("SLURM_ARRAY_TASK_ID", raising=False)
    monkeypatch.setenv("SLURM_JOB_ID", "9100")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    log_dir = tmp_path / "workspace" / "runs" / "run-a" / "logs"
    log_dir.mkdir(parents=True)

    runtime._write_task_outcome_receipt(log_dir, "run-a", SHUDRuntimeError("ARTIFACT_NOT_FOUND", "boom"))

    payload = json.loads((log_dir / "task_outcome.json").read_text(encoding="utf-8"))
    assert payload["slurm_job_id"] == "9100"
    assert payload["array_task_id"] is None


def test_task_outcome_receipt_identity_is_null_without_slurm_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("SLURM_ARRAY_JOB_ID", "SLURM_JOB_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SLURM_ARRAY_TASK_ID", "not-a-number")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    log_dir = tmp_path / "workspace" / "runs" / "run-a" / "logs"
    log_dir.mkdir(parents=True)

    runtime._write_task_outcome_receipt(log_dir, "run-a", SHUDRuntimeError("ARTIFACT_NOT_FOUND", "boom"))

    payload = json.loads((log_dir / "task_outcome.json").read_text(encoding="utf-8"))
    assert payload["slurm_job_id"] is None
    assert payload["array_task_id"] is None


@pytest.mark.parametrize(
    ("field_path", "value"),
    [
        ("run_id", "bad/run"),
        ("model.model_id", "../demo"),
        ("model.project_name", "-demo"),
        ("forcing.forcing_version_id", "forc\\evil"),
    ],
)
def test_manifest_path_components_are_rejected_before_db_updates(
    tmp_path: Path,
    field_path: str,
    value: str,
) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = deepcopy(_manifest())
    target: dict[str, Any] = manifest
    parts = field_path.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value

    with pytest.raises(ValueError, match="Invalid path component"):
        runtime.execute(manifest)

    assert repository.statuses == []
    assert repository.created == []


def test_runtime_from_env_requires_database_url_in_normal_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", raising=False)
    monkeypatch.delenv("NHMS_SHUD_DB_FREE", raising=False)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        SHUDRuntime.from_env()

    assert exc_info.value.error_code == "DATABASE_URL_MISSING"


def test_runtime_from_env_uses_db_free_repository_without_database_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", "true")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(tmp_path / "object-store"))

    runtime = SHUDRuntime.from_env()

    assert isinstance(runtime.repository, DbFreeHydroRunRepository)
    assert runtime.state_manager is None


def test_runtime_from_env_allows_missing_database_url_only_for_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    runtime = SHUDRuntime.from_env(dry_run=True)

    assert runtime.config.dry_run is True


def test_subprocess_timeout_bytes_are_decoded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    workspace = tmp_path / "workspace"
    output_dir = workspace / "output"
    log_dir = workspace / "logs"
    log_dir.mkdir(parents=True)
    cfg_path = workspace / "input" / "demo.cfg.para"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        "START_TIME = 2026-05-01T00:00:00Z\n"
        "END_TIME = 2026-05-04T00:00:00Z\n"
        f"OUTPUT_DIR = {output_dir}\n"
        "MODEL_OUTPUT_INTERVAL = 1440\n"
        "SEGMENT_COUNT = 2\n"
        "INIT_MODE = 1\n",
        encoding="utf-8",
    )

    def raise_timeout(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(["shud"], 1)

    monkeypatch.setattr(SHUDRuntime, "_wait_for_shud_process", raise_timeout)
    def fake_popen(*_args: Any, **kwargs: Any) -> SimpleNamespace:
        kwargs["stdout"].write("stdout bytes")
        kwargs["stderr"].write("stderr bytes")
        return SimpleNamespace(
            args=["shud"],
            kill=lambda: None,
            wait=lambda timeout=None: None,
        )

    monkeypatch.setattr("workers.shud_runtime.runtime.subprocess.Popen", fake_popen)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(_manifest(), cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "SHUD_TIMEOUT"
    assert (log_dir / "shud_stdout.log").read_text(encoding="utf-8") == "stdout bytes"
    assert (log_dir / "shud_stderr.log").read_text(encoding="utf-8") == "stderr bytes"


def test_create_run_conflict_only_resets_retriable_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakePsycopgError(Exception):
        pass

    class FakeCursor:
        def __init__(self) -> None:
            self.statement = ""
            self.statements: list[str] = []

        def __enter__(self) -> FakeCursor:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def execute(self, statement: str, _parameters: tuple[Any, ...]) -> None:
            self.statement = statement
            self.statements.append(statement)

        def fetchone(self) -> None:
            return None

    class FakeConnection:
        def __init__(self, cursor: FakeCursor) -> None:
            self.autocommit = True
            self.cursor_instance = cursor

        def cursor(self, **_kwargs: Any) -> FakeCursor:
            return self.cursor_instance

        def commit(self) -> None:
            pass

        def rollback(self) -> None:
            pass

        def close(self) -> None:
            pass

    cursor = FakeCursor()
    fake_psycopg2 = SimpleNamespace(
        Error=FakePsycopgError,
        connect=lambda _database_url: FakeConnection(cursor),
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake_psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", SimpleNamespace(RealDictCursor=object))

    from workers.shud_runtime.runtime import PsycopgHydroRunRepository

    with pytest.raises(SHUDRuntimeError) as exc_info:
        PsycopgHydroRunRepository("postgresql://example").create_run(_manifest(), "runs/demo/input/manifest.json")

    assert exc_info.value.error_code == "HYDRO_RUN_NOT_RETRIABLE"
    retriable_conflict_clause = "WHERE hydro.hydro_run.status IN ('failed', 'cancelled', 'pending')"
    assert any(retriable_conflict_clause in statement for statement in cursor.statements)


# --- Issue #257 / M23-6: SHUD executable + project-input preflight -----------


# Mock that mirrors the REAL compiled SHUD binary observed on node-22:
#   * any flag (--version/-v/--help/-h) -> "Unknown option", exit 1, NO token;
#   * no argument                       -> prints the identity banner, exit 0.
# This is the regression guard for the real-binary finding: a preflight that only
# probed flags would wrongly mark the genuine SHUD as having no version signal.
_REAL_SHUD_BEHAVIOR_SCRIPT = (
    "#!/bin/sh\n"
    'if [ "$#" -gt 0 ]; then\n'
    '  echo "Unknown option: $1" >&2\n'
    "  exit 1\n"
    "fi\n"
    'echo "Simulator for Hydrologic Unstructured Domains v2.0  2022"\n'
    'echo "./shud [-0gv] [-p project_file] [-c Calib_file] [-o output] [-n Num_Threads] <project_name>"\n'
    "exit 0\n"
)


def _write_real_shud_behavior_binary(path: Path) -> Path:
    path.write_text(_REAL_SHUD_BEHAVIOR_SCRIPT, encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_shud_dirs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    workspace = tmp_path / "workspace"
    output_dir = workspace / "output"
    log_dir = workspace / "logs"
    log_dir.mkdir(parents=True)
    cfg_path = workspace / "input" / "demo.cfg.para"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        "START_TIME = 2026-05-01T00:00:00Z\n"
        "END_TIME = 2026-05-04T00:00:00Z\n"
        f"OUTPUT_DIR = {output_dir}\n"
        "MODEL_OUTPUT_INTERVAL = 1440\n"
        "SEGMENT_COUNT = 2\n"
        "INIT_MODE = 1\n",
        encoding="utf-8",
    )
    return workspace, output_dir, log_dir, cfg_path


def _run_shud_project_dirs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """`_run_shud_dirs` for `command_style="shud_project"`.

    Native SHUD reads a tab-separated `input/<project>/<project>.cfg.para` with
    START/END expressed in DAYS and takes the output dir from `-o`, so the
    recovery rerun rewrites a different key (`END`) with a different separator
    than the cfg-style lane.
    """

    workspace = tmp_path / "workspace"
    output_dir = workspace / "output"
    log_dir = workspace / "logs"
    log_dir.mkdir(parents=True)
    cfg_path = workspace / "input" / "demo" / "demo.cfg.para"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        "START\t0\nEND\t3\nASCII_OUTPUT\t1\nSCR_INTV\t1440\nSEGMENT_COUNT\t2\n",
        encoding="utf-8",
    )
    return workspace, output_dir, log_dir, cfg_path


@pytest.mark.parametrize("stub", ["/bin/true", "/bin/false", "true", "false"])
def test_run_shud_rejects_stub_executable_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub: str,
) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=Path(stub))
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)

    def _fail_subprocess(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("stub executable must be rejected before invoking SHUD")

    monkeypatch.setattr("workers.shud_runtime.runtime.subprocess.run", _fail_subprocess)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(_manifest(), cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "SHUD_EXECUTABLE_STUB_REJECTED"


def test_run_shud_rejects_empty_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=Path(" "))
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    monkeypatch.setattr(
        "workers.shud_runtime.runtime.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(_manifest(), cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "SHUD_EXECUTABLE_NOT_CONFIGURED"


def test_run_shud_rejects_missing_compiled_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeHydroRunRepository()
    missing = tmp_path / "no_such_shud_binary"
    runtime = _runtime(tmp_path, repository, shud_executable=missing)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    monkeypatch.setattr(
        "workers.shud_runtime.runtime.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(_manifest(), cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "SHUD_EXECUTABLE_MISSING"


def test_run_shud_rejects_non_executable_compiled_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeHydroRunRepository()
    binary = tmp_path / "shud_omp"
    binary.write_text("SHUD\n", encoding="utf-8")
    binary.chmod(0o644)
    runtime = _runtime(tmp_path, repository, shud_executable=binary)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    monkeypatch.setattr(
        "workers.shud_runtime.runtime.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(_manifest(), cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "SHUD_EXECUTABLE_NOT_EXECUTABLE"


def test_run_shud_missing_python_runtime_script_is_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=tmp_path / "absent_engine.py")
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    monkeypatch.setattr(
        "workers.shud_runtime.runtime.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(_manifest(), cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "SHUD_EXECUTABLE_MISSING"


def test_run_shud_allows_valid_python_runtime_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)  # tests/mock_shud_omp.py
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    del monkeypatch

    runtime.run_shud(_manifest(), cfg_path, workspace, output_dir, log_dir)

    assert (output_dir / "demo.rivqdown").exists()
    assert (log_dir / "shud_stdout.log").read_text(encoding="utf-8")


def test_run_shud_fails_when_requested_state_checkpoints_are_missing(tmp_path: Path) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)  # tests/mock_shud_omp.py writes no cfg.ic.update checkpoints.
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [6, 12]

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f006" in exc_info.value.message
    assert "f012" in exc_info.value.message
    # The solver writes `demo.cfg.ic` but never `demo.cfg.ic.update`, so the
    # watcher saw no header at all: the trail must say so explicitly rather than
    # render an empty list as a blank the reader can mistake for "not reported".
    assert "observed cfg.ic.update header minutes: none" in exc_info.value.message


def test_state_checkpoint_poll_seconds_defaults_to_fast_checkpoint_capture() -> None:
    manifest = _manifest()

    assert _state_checkpoint_poll_seconds(manifest) == pytest.approx(0.01)


_FAST_SOLVER_STUB = '''
import sys
from datetime import datetime
from pathlib import Path

def _read_cfg(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values

def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

cfg = _read_cfg(sys.argv[1])
output_dir = Path(cfg["OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
total_minutes = (_parse(cfg["END_TIME"]) - _parse(cfg["START_TIME"])).total_seconds() / 60.0
# A solve so fast every intermediate in-place rewrite of cfg.ic.update is
# invisible to the 0.01s watcher: only the FINAL header ever survives on disk.
(output_dir / "demo.cfg.ic.update").write_text(
    "2 2 %.6f\\n1 0.1\\n2 0.2\\n1 0\\n2 0\\n" % total_minutes,
    encoding="utf-8",
)
print("The successful end.")
'''


_STUCK_HEADER_SOLVER_STUB = '''
import sys
from pathlib import Path

def _read_cfg(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values

cfg = _read_cfg(sys.argv[1])
output_dir = Path(cfg["OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
# Broken solver: the ic.update header never reaches the requested f-hour,
# regardless of the configured END_TIME.
(output_dir / "demo.cfg.ic.update").write_text(
    "2 2 1440.000000\\n1 0.1\\n2 0.2\\n1 0\\n2 0\\n",
    encoding="utf-8",
)
print("The successful end.")
'''


_SLOW_SOLVER_STUB = '''
import sys
import time
from datetime import datetime
from pathlib import Path

def _read_cfg(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values

def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

cfg = _read_cfg(sys.argv[1])
output_dir = Path(cfg["OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
total_minutes = (_parse(cfg["END_TIME"]) - _parse(cfg["START_TIME"])).total_seconds() / 60.0
state = output_dir / "demo.cfg.ic.update"
# Normal-cadence solve: each in-place rewrite of cfg.ic.update stays alive for
# 25x the 0.01s watcher poll, so the in-flight watcher samples f012 itself.
for minute in (720.0, total_minutes):
    state.write_text("2 2 %.6f\\n1 0.1\\n2 0.2\\n1 0\\n2 0\\n" % minute, encoding="utf-8")
    time.sleep(0.25)
print("The successful end.")
'''


_SILENT_RECOVERY_SOLVER_STUB = '''
import sys
from datetime import datetime
from pathlib import Path

def _read_cfg(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values

def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

cfg = _read_cfg(sys.argv[1])
output_dir = Path(cfg["OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
if "state_checkpoint_recovery" in output_dir.parts:
    # Recovery rerun: exits 0 but writes NO state file (engine did not restart
    # the integration).  Anything found in the scratch dir afterwards can only
    # be residue from an earlier attempt.
    print("The successful end.")
    sys.exit(0)
total_minutes = (_parse(cfg["END_TIME"]) - _parse(cfg["START_TIME"])).total_seconds() / 60.0
(output_dir / "demo.cfg.ic.update").write_text(
    "2 2 %.6f\\n1 0.1\\n2 0.2\\n1 0\\n2 0\\n" % total_minutes,
    encoding="utf-8",
)
print("The successful end.")
'''


_PROJECT_FAST_SOLVER_STUB = '''
import sys
from pathlib import Path

argv = sys.argv[1:]
output_dir = Path(argv[argv.index("-o") + 1])
project = argv[-1]
cfg = {}
for line in (Path("input") / project / (project + ".cfg.para")).read_text(encoding="utf-8").splitlines():
    parts = line.split("\\t")
    if len(parts) < 2:
        continue
    cfg[parts[0].strip()] = parts[1].strip()
total_minutes = (float(cfg["END"]) - float(cfg["START"])) * 1440.0
output_dir.mkdir(parents=True, exist_ok=True)
# Same race-losing behavior as _FAST_SOLVER_STUB, in native project mode: the
# output dir comes from -o and the horizon from the tab-separated cfg in days.
(output_dir / (project + ".cfg.ic.update")).write_text(
    "2 2 %.6f\\n1 0.1\\n2 0.2\\n1 0\\n2 0\\n" % total_minutes,
    encoding="utf-8",
)
print("The successful end.")
'''


_IC_DERIVED_SOLVER_STUB = '''
import sys
from datetime import datetime
from pathlib import Path

def _read_cfg(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values

def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

cfg_path = Path(sys.argv[1])
cfg = _read_cfg(cfg_path)
output_dir = Path(cfg["OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
total_minutes = (_parse(cfg["END_TIME"]) - _parse(cfg["START_TIME"])).total_seconds() / 60.0
# Integration that provably depends on the STAGED IC: the state body is carried
# forward from demo.cfg.ic, so a rerun that ignored the staged IC (or read some
# other file) cannot produce this body.
ic_body = (cfg_path.parent / "demo.cfg.ic").read_text(encoding="utf-8").splitlines()[1:]
(output_dir / "demo.cfg.ic.update").write_text(
    "2 2 %.6f\\n" % total_minutes + "\\n".join(ic_body) + "\\n",
    encoding="utf-8",
)
print("The successful end.")
'''


_FAILING_RECOVERY_SOLVER_STUB = '''
import sys
from datetime import datetime
from pathlib import Path

def _read_cfg(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values

def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

cfg = _read_cfg(sys.argv[1])
output_dir = Path(cfg["OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
if "state_checkpoint_recovery" in output_dir.parts:
    sys.stderr.write("recovery leg crashed\\n")
    sys.exit(1)
total_minutes = (_parse(cfg["END_TIME"]) - _parse(cfg["START_TIME"])).total_seconds() / 60.0
(output_dir / "demo.cfg.ic.update").write_text(
    "2 2 %.6f\\n1 0.1\\n2 0.2\\n1 0\\n2 0\\n" % total_minutes,
    encoding="utf-8",
)
print("The successful end.")
'''


_HANGING_RECOVERY_SOLVER_STUB = '''
import sys
import time
from datetime import datetime
from pathlib import Path

def _read_cfg(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values

def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

cfg = _read_cfg(sys.argv[1])
output_dir = Path(cfg["OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
if "state_checkpoint_recovery" in output_dir.parts:
    # Recovery leg hangs far past any sane budget.
    time.sleep(300)
    sys.exit(0)
# Main solve burns most of the shared timeout budget, then leaves only the final
# header behind (f012 missed).
time.sleep(4.5)
total_minutes = (_parse(cfg["END_TIME"]) - _parse(cfg["START_TIME"])).total_seconds() / 60.0
(output_dir / "demo.cfg.ic.update").write_text(
    "2 2 %.6f\\n1 0.1\\n2 0.2\\n1 0\\n2 0\\n" % total_minutes,
    encoding="utf-8",
)
print("The successful end.")
'''


_TRUNCATED_BODY_RECOVERY_SOLVER_STUB = '''
import sys
from datetime import datetime
from pathlib import Path

def _read_cfg(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values

def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

cfg = _read_cfg(sys.argv[1])
output_dir = Path(cfg["OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
total_minutes = (_parse(cfg["END_TIME"]) - _parse(cfg["START_TIME"])).total_seconds() / 60.0
if "state_checkpoint_recovery" in output_dir.parts:
    # Recovery leg: rc == 0 and the header lands EXACTLY on the target minute,
    # but the body stops one river row short (2 mesh + 1 river for a 2-river
    # model) -- an interrupted/partially flushed write. Only the structure gate
    # can tell this apart from a good state.
    (output_dir / "demo.cfg.ic.update").write_text(
        "2 2 %.6f\\n1 0.1\\n2 0.2\\n1 0\\n" % total_minutes,
        encoding="utf-8",
    )
    print("The successful end.")
    sys.exit(0)
(output_dir / "demo.cfg.ic.update").write_text(
    "2 2 %.6f\\n1 0.1\\n2 0.2\\n1 0\\n2 0\\n" % total_minutes,
    encoding="utf-8",
)
print("The successful end.")
'''


_EXECUTE_FAST_SOLVER_STUB = '''
import sys
from datetime import datetime, timedelta
from pathlib import Path

def _read_cfg(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values

def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

cfg = _read_cfg(sys.argv[1])
output_dir = Path(cfg["OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
start = _parse(cfg["START_TIME"])
end = _parse(cfg["END_TIME"])
interval = int(cfg.get("MODEL_OUTPUT_INTERVAL", "1440"))
segments = int(cfg.get("SEGMENT_COUNT", "1"))
steps = int((end - start).total_seconds() // (interval * 60))
# Real deliverable (verify_output reads it), same shape as tests/mock_shud_omp.py.
rows = [",".join(["time"] + ["seg_%04d" % index for index in range(1, segments + 1)])]
for step in range(steps):
    stamp = (start + timedelta(minutes=step * interval)).isoformat()
    rows.append(",".join([stamp] + ["%.3f" % (100.0 + index + step) for index in range(1, segments + 1)]))
(output_dir / "demo.rivqdown").write_text("\\n".join(rows) + "\\n", encoding="utf-8")
# ... plus the race-losing restart state: only the FINAL header survives.
(output_dir / "demo.cfg.ic.update").write_text(
    "2 2 %.6f\\n1 0.1\\n2 0.2\\n1 0\\n2 0\\n" % ((end - start).total_seconds() / 60.0),
    encoding="utf-8",
)
print("The successful end.")
'''


_RESIDUE_ONLY_SOLVER_STUB = '''
import sys
from pathlib import Path

def _read_cfg(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values

cfg = _read_cfg(sys.argv[1])
output_dir = Path(cfg["OUTPUT_DIR"])
(output_dir / "sub").mkdir(parents=True, exist_ok=True)
# A successful solve that left NO final state at either exact candidate path:
# a foreign-named residue in the root and a correctly-named one one level down.
(output_dir / "residue.cfg.ic.update").write_text(
    "2 2 4320.000000\\n1 0.1\\n2 0.2\\n1 0\\n2 0\\n",
    encoding="utf-8",
)
(output_dir / "sub" / "demo.cfg.ic.update").write_text(
    "2 2 4320.000000\\n1 0.3\\n2 0.4\\n1 0\\n2 0\\n",
    encoding="utf-8",
)
print("The successful end.")
'''


_EXIT_NONZERO_SOLVER_STUB = '''
import sys
from pathlib import Path

def _read_cfg(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values

cfg = _read_cfg(sys.argv[1])
output_dir = Path(cfg["OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
# The killed-attempt geometry: a restart state IS on disk, but the solve failed.
(output_dir / "demo.cfg.ic.update").write_text(
    "2 2 720.000000\\n1 0.1\\n2 0.2\\n1 0\\n2 0\\n",
    encoding="utf-8",
)
print("solver died", file=sys.stderr)
raise SystemExit(3)
'''


# A gate-valid f012 state (header 720, structurally complete for 2 rivers) whose
# body is distinguishable from what the stubs produce in this run: it stands for
# a previous attempt's leftovers in a reused run workspace.
_STALE_SCRATCH_STATE = "2 2 720.000000\n1 0.7\n2 0.8\n1 0\n2 0\n"

# A staged IC whose body values appear nowhere else in these fixtures, so a
# recovered checkpoint carrying them can only have come from re-integrating it.
_DISTINCTIVE_STAGED_IC = "2 2 0.000000\n1 0.4242\n2 0.8484\n1 0.1717\n2 0.3535\n"


@pytest.mark.parametrize("command_style", ["cfg", "shud_project"])
def test_run_shud_recovers_watcher_missed_checkpoint_via_deterministic_rerun(
    tmp_path: Path,
    command_style: str,
) -> None:
    """#1315: a solve faster than the sampling watcher must not hard-fail.

    The stub leaves only the final ic.update header on disk (the deterministic
    limit of the race the real 1.659s xinanjiang run lost), so the in-flight
    watcher can never capture f012. The post-run recovery rerun with END
    shortened to 12h must derive the checkpoint deterministically.

    Both command styles are exercised because they rewrite DIFFERENT cfg keys
    with different separators and units (`END_TIME` ISO with ` = ` vs `END` in
    days with a tab); production basins run the project style, so a cfg-only
    test would leave the production lane unproven.

    The scratch root is pre-seeded with a stale gate-valid state so this also
    pins the positive half of fresh-scoping: a rerun that DOES produce a state
    wins over the residue, byte for byte.
    """

    project_mode = command_style == "shud_project"
    stub = tmp_path / "fast_solver.py"
    stub.write_text(_PROJECT_FAST_SOLVER_STUB if project_mode else _FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    dirs = _run_shud_project_dirs(tmp_path) if project_mode else _run_shud_dirs(tmp_path)
    workspace, output_dir, log_dir, cfg_path = dirs
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    if project_mode:
        manifest["runtime"]["command_style"] = "shud_project"
    cfg_before = cfg_path.read_bytes()
    stale_scratch = workspace / "state_checkpoint_recovery" / "f012"
    stale_scratch.mkdir(parents=True)
    (stale_scratch / "demo.cfg.ic.update").write_text(_STALE_SCRATCH_STATE, encoding="utf-8")

    runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    checkpoint = output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update"
    installed = checkpoint.read_text(encoding="utf-8")
    assert installed.startswith("2 2 720.000000")
    # Fresh rerun content, not the pre-seeded residue.
    assert installed == "2 2 720.000000\n1 0.1\n2 0.2\n1 0\n2 0\n"
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert [item["lead_hours"] for item in payload["checkpoints"]] == [12]
    assert payload["checkpoints"][0]["provenance"] == "post_run_recovery"
    # The diagnostic trail is present on the success path too, and names the lane
    # that produced the checkpoint.
    assert payload["observed_header_minutes"] == [4320.0]
    assert payload["recovery_outcomes"] == {"12": "recovered"}
    # The recovery rerun must not leak a shortened horizon into the shared cfg
    # (byte identity, not just the key it rewrote).
    assert cfg_path.read_bytes() == cfg_before
    if project_mode:
        assert "END\t3" in cfg_path.read_text(encoding="utf-8")
    else:
        assert "END_TIME = 2026-05-04T00:00:00Z" in cfg_path.read_text(encoding="utf-8")
    # The in-process recovery rerun is not a scheduler-visible run: no run row,
    # no status transition, no candidate was created for it.
    assert repository.created == []
    assert repository.statuses == []
    assert repository.failures == []


def test_run_shud_recovers_every_missing_hour_with_scoped_scratch_and_logs(tmp_path: Path) -> None:
    """#1315: multi-hour miss recovers each hour in its own scratch dir.

    Production chains request more than one checkpoint hour; a single-hour test
    cannot show that the per-hour END rewrite, scratch scoping and log naming
    stay independent across iterations.
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [6, 12]
    cfg_before = cfg_path.read_bytes()

    runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    checkpoint_dir = output_dir / "state_checkpoints"
    assert (checkpoint_dir / "demo.f006.cfg.ic.update").read_text(encoding="utf-8").startswith("2 2 360.000000")
    assert (checkpoint_dir / "demo.f012.cfg.ic.update").read_text(encoding="utf-8").startswith("2 2 720.000000")
    payload = json.loads((checkpoint_dir / "state_checkpoints.json").read_text(encoding="utf-8"))
    assert [item["lead_hours"] for item in payload["checkpoints"]] == [6, 12]
    assert payload["recovery_outcomes"] == {"6": "recovered", "12": "recovered"}
    recovery_root = workspace / "state_checkpoint_recovery"
    assert sorted(path.name for path in recovery_root.iterdir()) == ["f006", "f012"]
    for hour in ("f006", "f012"):
        assert (log_dir / f"state_checkpoint_recovery_{hour}.out.log").exists()
        assert (log_dir / f"state_checkpoint_recovery_{hour}.err.log").exists()
    assert cfg_path.read_bytes() == cfg_before


def test_run_shud_recovered_checkpoint_body_derives_from_the_staged_ic(tmp_path: Path) -> None:
    """#1315: the recovery rerun must re-integrate the SAME staged IC.

    Every other stub synthesizes the state from END alone, so a rerun that
    ignored the staged initial condition would still pass them. Here the stub's
    output body is carried forward from `demo.cfg.ic`, whose values appear
    nowhere else: a recovered body containing them is evidence that the rerun
    consumed the run's own staged IC, which is what makes the recovered state
    equivalent to the watcher-target state.
    """

    stub = tmp_path / "ic_derived_solver.py"
    stub.write_text(_IC_DERIVED_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    (cfg_path.parent / "demo.cfg.ic").write_text(_DISTINCTIVE_STAGED_IC, encoding="utf-8")
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]

    runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    installed = (output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update").read_text(encoding="utf-8")
    assert installed == "2 2 720.000000\n" + "".join(_DISTINCTIVE_STAGED_IC.splitlines(keepends=True)[1:])


def test_run_shud_recovery_repeats_deterministically(tmp_path: Path) -> None:
    """#1315 AC1 repeat leg: 100 repeats of the race-losing solve, 0 misses.

    The issue acceptance box asks for 100 consecutive recoveries because the
    original defect was a probabilistic sampling race: a handful of green runs
    would not distinguish "deterministic" from "lucky".
    """

    attempts = 100
    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    recovered = 0
    for attempt in range(attempts):
        run_root = tmp_path / f"attempt{attempt}"
        run_root.mkdir()
        runtime = _runtime(run_root, repository, shud_executable=stub)
        workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(run_root)
        manifest = _manifest()
        manifest["runtime"]["state_checkpoint_hours"] = [12]

        runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

        checkpoint = output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update"
        assert checkpoint.exists()
        assert checkpoint.read_text(encoding="utf-8").startswith("2 2 720.000000")
        recovered += 1

    assert recovered == attempts


def test_run_shud_checkpoint_capture_is_solve_speed_independent_slow_leg(tmp_path: Path) -> None:
    """#1315 AC2 (slow leg): a normal-cadence solve is captured by the watcher.

    Pairs with ``test_run_shud_recovers_watcher_missed_checkpoint_via_deterministic_rerun``
    (fast leg): both end with an f012 checkpoint on disk, which is what
    "capture does not depend on solve speed" means. Here the intermediate
    header state lives far longer than the poll interval, so the watcher wins
    the sample and the recovery rerun must never be invoked at all — the
    unchanged sibling behavior the fix must not disturb.
    """

    stub = tmp_path / "slow_solver.py"
    stub.write_text(_SLOW_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]

    runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    checkpoint = output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update"
    assert checkpoint.read_text(encoding="utf-8").startswith("2 2 720.000000")
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    entry = payload["checkpoints"][0]
    assert entry["lead_hours"] == 12
    # Watcher capture: no recovery provenance, and no recovery scratch root.
    assert "provenance" not in entry
    assert not (workspace / "state_checkpoint_recovery").exists()
    # The observed-header trail is DEDUPED, not one sample per poll: each header
    # here survives 25 polls, so an append-every-sample trail would bury the two
    # states the solver actually passed through under ~50 repeats.
    assert payload["observed_header_minutes"] == [720.0, 4320.0]


def test_run_shud_recovery_never_installs_stale_scratch_state(tmp_path: Path) -> None:
    """#1315 File IO lane: a reused workspace's leftover state is not a checkpoint.

    ``install_recovered`` reads a fixed filename out of the per-hour scratch
    root, so a gate-valid ``demo.cfg.ic.update`` left there by an earlier
    attempt is byte-indistinguishable from a fresh result. The scratch root
    must be fresh-scoped before the rerun: here the rerun exits 0 without
    writing any state, so the hour stays missing and the run fails hard rather
    than publishing the stale state into the warm-start lineage.
    """

    stub = tmp_path / "silent_recovery_solver.py"
    stub.write_text(_SILENT_RECOVERY_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    stale_scratch = workspace / "state_checkpoint_recovery" / "f012"
    stale_scratch.mkdir(parents=True)
    stale_state = stale_scratch / "demo.cfg.ic.update"
    stale_state.write_text(_STALE_SCRATCH_STATE, encoding="utf-8")
    cfg_before = cfg_path.read_bytes()

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f012" in exc_info.value.message
    assert not (output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update").exists()
    assert not stale_state.exists()
    # "the rerun produced nothing" is distinguishable from every other lane.
    assert "f012=gate_rejected(no_state)" in exc_info.value.message
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert payload["recovery_outcomes"] == {"12": "gate_rejected(no_state)"}
    assert cfg_path.read_bytes() == cfg_before


def test_run_shud_recovery_refuses_unclean_scratch_without_half_clearing_it(tmp_path: Path) -> None:
    """#1315: refusing an unexpected scratch dir must be diagnosable, not destructive.

    The scratch clear inspects a fixed filename's directory; if it meets
    something it will not touch (a sub-directory, symlink, device node) it
    refuses that hour. Refusing mid-iteration would already have unlinked the
    entries it walked first — deleting exactly the evidence about what an
    earlier attempt did, permanently, since the refusal repeats on every retry.
    So: inspect everything first, unlink nothing, and say why in the per-hour
    recovery log and the run's own failure message.
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    scratch = workspace / "state_checkpoint_recovery" / "f012"
    scratch.mkdir(parents=True)
    # Sorted order puts a regular file on either side of the offending entry.
    (scratch / "aaa.txt").write_text("first\n", encoding="utf-8")
    (scratch / "leftover_subdir").mkdir()
    (scratch / "zzz.txt").write_text("last\n", encoding="utf-8")

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    # Nothing was removed: the refusal is a no-op on the directory.
    assert (scratch / "aaa.txt").read_text(encoding="utf-8") == "first\n"
    assert (scratch / "zzz.txt").read_text(encoding="utf-8") == "last\n"
    assert (scratch / "leftover_subdir").is_dir()
    assert "f012=skipped_scratch_unclean" in exc_info.value.message
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert payload["recovery_outcomes"] == {"12": "skipped_scratch_unclean"}
    refusal_log = (log_dir / "state_checkpoint_recovery_f012.err.log").read_text(encoding="utf-8")
    assert "leftover_subdir" in refusal_log
    assert "not a regular file" in refusal_log


def test_run_shud_recovery_refusal_log_failure_does_not_alter_the_hour_outcome(tmp_path: Path) -> None:
    """#1315 r3-audit: writing the refusal reason is best-effort, like the manifest.

    Same class as the manifest-masking lane: a diagnostics write that fails must
    not be able to relabel (or escape from) the outcome it was only meant to
    describe. The hour's verdict stays exactly `skipped_scratch_unclean` — not a
    composed lane that reads as if the scratch dir failed twice.
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    scratch = workspace / "state_checkpoint_recovery" / "f012"
    scratch.mkdir(parents=True)
    (scratch / "leftover_subdir").mkdir()

    real_write = runtime_module._write_text_no_follow

    def _fail_refusal_log_write(path: Path, content: str, **kwargs: Any) -> Path:
        if Path(path).name == "state_checkpoint_recovery_f012.err.log":
            raise SHUDRuntimeError("WORKSPACE_WRITE_FAILED", f"Failed to write staged file {path}: no space left")
        return real_write(path, content, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime_module, "_write_text_no_follow", _fail_refusal_log_write)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert not (log_dir / "state_checkpoint_recovery_f012.err.log").exists()
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert payload["recovery_outcomes"] == {"12": "skipped_scratch_unclean"}


def test_run_shud_recovery_skips_only_the_hour_whose_cfg_write_fails(tmp_path: Path) -> None:
    """#1315: an ENOSPC-class write failure on one hour must not abort the loop.

    The no-follow cfg writer raises `SHUDRuntimeError`, not `OSError`, so a
    failure here used to escape the recovery loop entirely: remaining hours were
    never attempted, `state_checkpoints.json` was never written, and the caller
    saw `WORKSPACE_WRITE_FAILED` instead of the stable
    `STATE_CHECKPOINTS_MISSING` contract.
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [6, 12]
    cfg_before = cfg_path.read_bytes()

    real_write = runtime_module._write_text_no_follow

    def _fail_f006_cfg_write(path: Path, content: str, **kwargs: Any) -> Path:
        if "END_TIME = 2026-05-01T06:00:00Z" in content:
            raise SHUDRuntimeError("WORKSPACE_WRITE_FAILED", f"Failed to write staged file {path}: no space left")
        return real_write(path, content, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime_module, "_write_text_no_follow", _fail_f006_cfg_write)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f006" in exc_info.value.message
    assert "f012" not in exc_info.value.message.split("(")[0]
    assert "f006=cfg_write_failed" in exc_info.value.message
    checkpoint_dir = output_dir / "state_checkpoints"
    # The other hour is still recovered, and the manifest is still written.
    assert (checkpoint_dir / "demo.f012.cfg.ic.update").read_text(encoding="utf-8").startswith("2 2 720.000000")
    assert not (checkpoint_dir / "demo.f006.cfg.ic.update").exists()
    payload = json.loads((checkpoint_dir / "state_checkpoints.json").read_text(encoding="utf-8"))
    assert [item["lead_hours"] for item in payload["checkpoints"]] == [12]
    assert payload["recovery_outcomes"] == {"6": "cfg_write_failed", "12": "recovered"}
    assert cfg_path.read_bytes() == cfg_before


def test_run_shud_recovery_contains_install_time_failure_to_that_hour(tmp_path: Path) -> None:
    """#1315 CAND-1: an install-time IO failure must skip only its own hour.

    The per-hour containment try used to end BEFORE `install_recovered`, so a
    read/write failure inside the install step (ENOSPC, a racing unlink, an
    unsafe path) escaped the recovery loop entirely: the remaining hours were
    never attempted, `state_checkpoints.json` was never written, and the caller
    saw the workspace error code instead of the stable
    `STATE_CHECKPOINTS_MISSING` contract.
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [6, 12]
    cfg_before = cfg_path.read_bytes()

    real_read = runtime_module._read_cfg_ic_header_minute

    def _fail_f006_install_read(path: Path) -> float | None:
        # Only the f006 install step reads this path; the watcher reads the
        # solver's own output dir and every other hour is untouched.
        if "state_checkpoint_recovery" in Path(path).parts and Path(path).parent.name == "f006":
            raise SHUDRuntimeError("WORKSPACE_READ_FAILED", f"Failed to read staged file {path}: I/O error")
        return real_read(path)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime_module, "_read_cfg_ic_header_minute", _fail_f006_install_read)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f006=install_failed" in exc_info.value.message
    checkpoint_dir = output_dir / "state_checkpoints"
    # The other hour is still recovered and the manifest is still written.
    assert (checkpoint_dir / "demo.f012.cfg.ic.update").read_text(encoding="utf-8").startswith("2 2 720.000000")
    payload = json.loads((checkpoint_dir / "state_checkpoints.json").read_text(encoding="utf-8"))
    assert [item["lead_hours"] for item in payload["checkpoints"]] == [12]
    assert payload["recovery_outcomes"] == {"6": "install_failed", "12": "recovered"}
    assert cfg_path.read_bytes() == cfg_before


def test_run_shud_recovery_contains_safe_filesystem_error_from_install(tmp_path: Path) -> None:
    """#1315 CAND-1 (sibling class): raw safe-fs helpers raise `SafeFilesystemError`.

    `install_recovered` calls `unlink_no_follow` directly on the gate-reject
    path, and that helper raises `SafeFilesystemError` — neither an `OSError`
    nor a `SHUDRuntimeError`. The containment tuple must catch the whole class,
    otherwise this one lane still tears down the loop and the error code.
    """

    stub = tmp_path / "stuck_solver.py"
    stub.write_text(_STUCK_HEADER_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    cfg_before = cfg_path.read_bytes()

    real_unlink = runtime_module.unlink_no_follow

    def _fail_checkpoint_unlink(path: Path, **kwargs: Any) -> None:
        if Path(path).name.endswith(".f012.cfg.ic.update"):
            raise SafeFilesystemError(f"Failed to unlink {path}: device busy", kind="io")
        real_unlink(path, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime_module, "unlink_no_follow", _fail_checkpoint_unlink)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f012=install_failed" in exc_info.value.message
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert payload["checkpoints"] == []
    assert payload["recovery_outcomes"] == {"12": "install_failed"}
    assert cfg_path.read_bytes() == cfg_before


def test_run_shud_manifest_write_failure_does_not_mask_missing_checkpoints(tmp_path: Path) -> None:
    """#1315 CAND-2: the diagnostics write must not decide the run's error code.

    `state_checkpoints.json` is written right before the miss is raised. When
    that write itself failed (full disk — exactly the condition that makes the
    trail worth having), the workspace error escaped and the run was classified
    as `WORKSPACE_WRITE_FAILED`, hiding the real `STATE_CHECKPOINTS_MISSING`
    outcome from the failure classifier and every operator artifact.
    """

    stub = tmp_path / "stuck_solver.py"
    stub.write_text(_STUCK_HEADER_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]

    real_write = runtime_module._write_text_no_follow

    def _fail_manifest_write(path: Path, content: str, **kwargs: Any) -> Path:
        if Path(path).name == "state_checkpoints.json":
            raise SHUDRuntimeError("WORKSPACE_WRITE_FAILED", f"Failed to write staged file {path}: no space left")
        return real_write(path, content, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime_module, "_write_text_no_follow", _fail_manifest_write)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f012" in exc_info.value.message
    # The lost diagnostics are reported, not substituted for the outcome.
    assert "state_checkpoints.json write failed" in exc_info.value.message
    assert not (output_dir / "state_checkpoints" / "state_checkpoints.json").exists()


def test_run_shud_manifest_write_failure_fails_the_run_when_nothing_is_missing(tmp_path: Path) -> None:
    """#1315 r3-test-01: with nothing missing the manifest is a DELIVERABLE.

    Complement of `..._does_not_mask_missing_checkpoints`: swallowing the
    manifest-write failure is correct ONLY while a miss owns the error code.
    When every requested hour was captured, `state_checkpoints.json` is what
    cross-cycle warm-start chaining reads to find the checkpoint file, so the
    write failure must still take the run down with the workspace error code —
    a run that reports success while its checkpoint index is absent would hand
    the next cycle a cold start with no failure receipt anywhere.

    Slow-leg base (`_SLOW_SOLVER_STUB`): the watcher captures f012 itself, so
    the miss branch is never entered and the manifest write is the only failure.
    """

    stub = tmp_path / "slow_solver.py"
    stub.write_text(_SLOW_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]

    real_write = runtime_module._write_text_no_follow

    def _fail_manifest_write(path: Path, content: str, **kwargs: Any) -> Path:
        if Path(path).name == "state_checkpoints.json":
            raise SHUDRuntimeError("WORKSPACE_WRITE_FAILED", f"Failed to write staged file {path}: no space left")
        return real_write(path, content, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime_module, "_write_text_no_follow", _fail_manifest_write)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    # The original workspace error is re-raised unchanged, not reclassified.
    assert exc_info.value.error_code == "WORKSPACE_WRITE_FAILED"
    assert "state_checkpoints.json" in exc_info.value.message
    assert "STATE_CHECKPOINTS_MISSING" not in exc_info.value.message
    # Evidence that this is the deliverable lane: the hour WAS captured by the
    # watcher (no recovery involved), only its index write failed.
    checkpoint_dir = output_dir / "state_checkpoints"
    assert (checkpoint_dir / "demo.f012.cfg.ic.update").read_text(encoding="utf-8").startswith("2 2 720.000000")
    assert not (checkpoint_dir / "state_checkpoints.json").exists()
    assert not (workspace / "state_checkpoint_recovery").exists()


def test_run_shud_recovery_rejects_structurally_truncated_state(tmp_path: Path) -> None:
    """#1315 r2-test-01: the recovery producer keeps the e13ae809 BODY gate.

    The header gate alone cannot see a partially flushed state: here the rerun's
    header lands exactly on the target minute (720) while the body is one river
    row short. Installing it would publish a truncated restart state into the
    warm-start lineage, which is precisely what `state_ic_structure_complete`
    exists to stop — delete that clause from `install_recovered` and this test
    turns green-to-red (the run would succeed with the truncated checkpoint).
    """

    stub = tmp_path / "truncated_body_solver.py"
    stub.write_text(_TRUNCATED_BODY_RECOVERY_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f012" in exc_info.value.message
    # Partial-discard semantics: the candidate is unlinked, not left behind.
    checkpoint_dir = output_dir / "state_checkpoints"
    assert not (checkpoint_dir / "demo.f012.cfg.ic.update").exists()
    assert sorted(path.name for path in checkpoint_dir.iterdir()) == ["state_checkpoints.json"]
    # The rerun DID land on the target header: only the body gate rejected it.
    assert "f012=gate_rejected(header=720)" in exc_info.value.message
    payload = json.loads((checkpoint_dir / "state_checkpoints.json").read_text(encoding="utf-8"))
    assert payload["checkpoints"] == []
    assert payload["recovery_outcomes"] == {"12": "gate_rejected(header=720)"}


def test_run_shud_recovery_records_cfg_restore_failure_alongside_its_own_outcome(tmp_path: Path) -> None:
    """#1315 r2-test-04: a failed cfg restore is recorded, never masking the lane.

    The restore runs in a `finally`, so it is the one write that can fail AFTER
    the hour's real outcome is decided. It must ride along in the outcome trail
    (the workspace is left with a shortened horizon and an operator has to know)
    without changing what the recovery itself achieved.
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    original_cfg = cfg_path.read_text(encoding="utf-8")

    real_write = runtime_module._write_text_no_follow

    def _fail_only_the_restore_write(path: Path, content: str, **kwargs: Any) -> Path:
        # Content-discriminating: the shortened-horizon write and the manifest
        # write still go through; only writing the ORIGINAL bytes back fails.
        if content == original_cfg:
            raise SHUDRuntimeError("WORKSPACE_WRITE_FAILED", f"Failed to write staged file {path}: no space left")
        return real_write(path, content, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime_module, "_write_text_no_follow", _fail_only_the_restore_write)
        runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    checkpoint_dir = output_dir / "state_checkpoints"
    # The recovery itself is unaffected: the checkpoint is installed and the run
    # does not fail because the cfg could not be put back.
    assert (checkpoint_dir / "demo.f012.cfg.ic.update").read_text(encoding="utf-8").startswith("2 2 720.000000")
    payload = json.loads((checkpoint_dir / "state_checkpoints.json").read_text(encoding="utf-8"))
    assert [item["lead_hours"] for item in payload["checkpoints"]] == [12]
    # Restore runs before install, so it lands first in the composed outcome.
    assert payload["recovery_outcomes"] == {"12": "cfg_restore_failed+recovered"}
    # Evidence that the restore really did not happen: the workspace cfg still
    # carries the shortened recovery horizon.
    assert "END_TIME = 2026-05-01T12:00:00Z" in cfg_path.read_text(encoding="utf-8")


def test_execute_with_recovered_checkpoint_creates_exactly_one_run(tmp_path: Path) -> None:
    """#1315 r2-test-05: the recovery rerun is invisible to the run repository.

    `run_shud` never touches the repository, so asserting "no rows" there is
    tautological. `execute()` is the level where a run row, its status
    progression and any failure are actually recorded: a recovery rerun must
    stay an in-process implementation detail — one created run, the normal
    status sequence, no failures — not a second scheduler-visible run.
    """

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    stub = tmp_path / "execute_fast_solver.py"
    stub.write_text(_EXECUTE_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]

    result = runtime.execute(manifest)

    assert result.status == "succeeded"
    assert len(repository.created) == 1
    assert repository.created[0]["run_id"] == manifest["run_id"]
    assert repository.statuses == ["created", "staged", "running", "succeeded"]
    assert repository.failures == []
    # The checkpoint really did come from the recovery lane in this run.
    output_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "output"
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert [item["lead_hours"] for item in payload["checkpoints"]] == [12]
    assert payload["checkpoints"][0]["provenance"] == "post_run_recovery"


def test_run_shud_recovery_reports_non_zero_exit_lane(tmp_path: Path) -> None:
    """#1315: an rc != 0 rerun leaves the hour missing and says so."""

    stub = tmp_path / "failing_recovery_solver.py"
    stub.write_text(_FAILING_RECOVERY_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f012=exit_1" in exc_info.value.message
    assert not (output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update").exists()
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert payload["recovery_outcomes"] == {"12": "exit_1"}
    assert "recovery leg crashed" in (log_dir / "state_checkpoint_recovery_f012.err.log").read_text(encoding="utf-8")


def test_run_shud_main_solve_and_recovery_share_one_timeout_budget(tmp_path: Path) -> None:
    """#1315: recovery reruns must not multiply the run's wall-time budget.

    Pre-change `run_shud` could never exceed 1x `timeout_seconds`. A per-rerun
    fresh timeout turns a hung engine into (1 + missing hours) x budget, and an
    unbounded multiple of the configured timeout is not a bound at all -- it
    risks a Slurm SIGKILL mid-loop that loses the outcome receipt entirely. The
    main solve burns most of the budget here, so the hanging recovery leg may
    only have the remainder.

    Two hours are requested so BOTH halves of the bound are pinned
    deterministically: the first missing hour still has budget and is killed at
    the shared deadline (`timeout`), the second has none left and is never
    spawned at all (`budget_exhausted`). A single-hour version could only assert
    a disjunction, which passes even if the budget is silently per-hour.

    Budget sizing (r3-corr-01): the 4.5s main solve leaves ~5.4s of the 10s
    budget for f006, so the first hour keeps its `timeout` classification unless
    a loaded runner overruns the main solve by more than 4.4s. A tighter budget
    would flip f006 to `budget_exhausted` on a slow runner and turn the exact
    outcome map into a flake. Discrimination is unaffected: a per-hour budget
    still costs 4.5 + 10 + 10 = ~24.5s, far past the 15s bound.
    """

    budget = 10
    stub = tmp_path / "hanging_recovery_solver.py"
    stub.write_text(_HANGING_RECOVERY_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub, timeout_seconds=budget)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [6, 12]

    started = time.monotonic()
    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)
    elapsed = time.monotonic() - started

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    # Generous CI margin, still far below the pre-change 1 + 1 = 2 x budget.
    assert elapsed < 1.5 * budget
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    # 4.5s main solve + ~5.4s remaining for f006 -> killed at the shared
    # deadline; f012 then finds < 1s left and is skipped unspawned.
    assert payload["recovery_outcomes"] == {"6": "timeout", "12": "budget_exhausted"}
    assert payload["checkpoints"] == []
    assert not (log_dir / "state_checkpoint_recovery_f012.out.log").exists()


def test_run_shud_recovery_keeps_partial_gates_and_reports_observed_headers(tmp_path: Path) -> None:
    """#1315: a recovery rerun whose header misses the target must be rejected
    (e13ae809 gates preserved) and the hard failure must carry the observed
    header trail for diagnosis."""

    stub = tmp_path / "stuck_solver.py"
    stub.write_text(_STUCK_HEADER_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    cfg_before = cfg_path.read_bytes()

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f012" in exc_info.value.message
    assert "observed cfg.ic.update header minutes: 1440" in exc_info.value.message
    assert not (output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update").exists()
    # Total miss: the manifest is still written, because this is exactly the run
    # whose evidence an operator needs (production requests a single hour, so
    # `captured` is empty here).
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert payload["checkpoints"] == []
    assert payload["observed_header_minutes"] == [1440.0]
    assert payload["recovery_outcomes"] == {"12": "gate_rejected(header=1440)"}
    # The rerun's OWN header is what makes this a solver bug report rather than
    # "recovery never ran".
    assert "f012=gate_rejected(header=1440)" in exc_info.value.message
    # The manifest write SUCCEEDED here, so the failure message must carry no
    # write-failure note: an unconditional note would send an operator chasing a
    # disk problem that did not happen.
    assert "state_checkpoints.json write failed" not in exc_info.value.message
    assert cfg_path.read_bytes() == cfg_before


def test_run_shud_writes_witness_manifest_when_no_hours_are_requested(tmp_path: Path) -> None:
    """#1325 A7(a3): the zero-hour lane now writes the manifest, inverted from #1315.

    Supersedes the #1315 r3-audit guard that asserted `state_checkpoints/`
    stayed absent on a zero-hour solve (rationale in design D2.1): the sole
    production reader is now the publish-side admission gate, which needs the
    solver-success witness for exactly this lane, and
    `requested_checkpoint_hours` — not the presence of the file — is what
    discriminates "asked for nothing" from "asked and captured nothing".
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    assert "state_checkpoint_hours" not in manifest["runtime"]

    runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    # The solve really ran (its restart state is on disk) and is now witnessed.
    final_ic_path = output_dir / "demo.cfg.ic.update"
    assert final_ic_path.exists()
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert payload["checkpoints"] == []
    assert payload["provenance"]["requested_checkpoint_hours"] == []
    assert payload["final_ic"]["relative_path"] == "demo.cfg.ic.update"
    assert payload["final_ic"]["original_shud_filename"] == "demo.cfg.ic.update"
    assert payload["final_ic"]["checksum"] == sha256_bytes(final_ic_path.read_bytes())


def test_run_shud_zero_hour_solve_records_final_cfg_ic_when_no_update_exists(tmp_path: Path) -> None:
    """#1325 A7(a): the second exact candidate path is the real production shape.

    `tests/mock_shud_omp.py` writes `demo.cfg.ic` and never `demo.cfg.ic.update`
    — the `.cfg.ic`-only tree the fallback publish lane runs on.
    """

    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)  # tests/mock_shud_omp.py
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()

    runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert payload["checkpoints"] == []
    assert payload["final_ic"]["relative_path"] == "demo.cfg.ic"
    assert payload["final_ic"]["original_shud_filename"] == "demo.cfg.ic"
    assert payload["final_ic"]["checksum"] == sha256_bytes((output_dir / "demo.cfg.ic").read_bytes())


def test_run_shud_final_ic_ignores_residue_outside_the_two_candidate_paths(tmp_path: Path) -> None:
    """#1325 A7(a2): the writer blesses exactly two paths, never a search hit.

    A residue under another name, or the right name one directory down, must
    stay unnamed — otherwise the publish-side hole simply relocates from the
    gate to the writer.
    """

    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)  # tests/mock_shud_omp.py writes demo.cfg.ic
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    (output_dir / "sub").mkdir(parents=True)
    (output_dir / "aaa-residue.cfg.ic.update").write_text(
        "2 2 60.000000\n1 0.7\n2 0.8\n1 0\n2 0\n", encoding="utf-8"
    )
    (output_dir / "sub" / "demo.cfg.ic.update").write_text(
        "2 2 60.000000\n1 0.5\n2 0.6\n1 0\n2 0\n", encoding="utf-8"
    )

    runtime.run_shud(_manifest(), cfg_path, workspace, output_dir, log_dir)

    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert payload["final_ic"]["relative_path"] == "demo.cfg.ic"


def test_run_shud_records_no_final_ic_when_neither_candidate_path_exists(tmp_path: Path) -> None:
    """#1325 A7(a2): no final state at either exact path -> no `final_ic` key."""

    stub = tmp_path / "residue_only_solver.py"
    stub.write_text(_RESIDUE_ONLY_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)

    runtime.run_shud(_manifest(), cfg_path, workspace, output_dir, log_dir)

    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert "final_ic" not in payload
    assert payload["checkpoints"] == []


def test_run_shud_manifest_provenance_records_run_identity_and_job_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1325 A7(b): provenance is the gate's identity evidence for this attempt."""

    from datetime import datetime

    monkeypatch.setenv("SLURM_ARRAY_JOB_ID", "884422")
    monkeypatch.setenv("SLURM_ARRAY_TASK_ID", "7")
    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()

    runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    provenance = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )["provenance"]
    generated_at = datetime.fromisoformat(str(provenance["generated_at"]).replace("Z", "+00:00"))

    assert provenance["run_id"] == manifest["run_id"]
    assert provenance["slurm_job_id"] == "884422"
    assert provenance["array_task_id"] == 7
    assert provenance["requested_checkpoint_hours"] == []
    assert generated_at.utcoffset() is not None
    assert generated_at.utcoffset().total_seconds() == 0


def test_run_shud_solver_failure_writes_no_manifest(tmp_path: Path) -> None:
    """#1325 A7(c): the witness invariant — a failed solve leaves no manifest."""

    stub = tmp_path / "exit_nonzero_solver.py"
    stub.write_text(_EXIT_NONZERO_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(_manifest(), cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "SHUD_EXIT_3"
    # The restart state exists, so only manifest ABSENCE distinguishes this tree
    # from a healthy one downstream.
    assert (output_dir / "demo.cfg.ic.update").exists()
    assert not (output_dir / "state_checkpoints").exists()


def test_run_shud_total_miss_manifest_records_requested_checkpoint_hours(tmp_path: Path) -> None:
    """#1325 A7(d): the `STATE_CHECKPOINTS_MISSING` lane keeps writing its trails.

    Its manifest now also carries the non-empty `requested_checkpoint_hours`,
    which is precisely what stops the publish side from mistaking this tree for
    a zero-hour run and downgrading to the final IC.
    """

    stub = tmp_path / "stuck_solver.py"
    stub.write_text(_STUCK_HEADER_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert payload["checkpoints"] == []
    assert payload["provenance"]["requested_checkpoint_hours"] == [12]
    assert payload["recovery_outcomes"] == {"12": "gate_rejected(header=1440)"}


# ---------------------------------------------------------------------------
# #1325 A11: writer -> publish-gate round-trip.
#
# Every other gate anchor hand-builds the manifest, so the two halves of the
# witness contract (`_StateCheckpointTracker.write_manifest` and
# `state_cli._admit_state_publish_source`) are pinned only against the fixture,
# never against each other. These tests drive a REAL solve and feed the tree the
# writer actually produced to the real publish path: a one-sided key, shape or
# path-rooting change fails here and nowhere else.
# ---------------------------------------------------------------------------


class _RoundTripStateManager:
    """`StateManager` double recording what the publish gate handed it."""

    def __init__(self, object_store: LocalObjectStore) -> None:
        self.object_store = object_store
        self.saved: list[dict[str, Any]] = []
        self.qc_runs: list[str] = []

    def save_state_snapshot(self, **kwargs: Any) -> Any:
        self.saved.append(kwargs)
        state_id = f"state_{len(self.saved):03d}"
        snapshot = SimpleNamespace(
            state_uri=f"states/{state_id}/state.cfg.ic",
            checksum=f"sha256:{state_id}",
            valid_time=kwargs["valid_time"],
            source_id=kwargs.get("source_id"),
            cycle_id=kwargs.get("cycle_id"),
            lead_hours=kwargs.get("lead_hours"),
            model_package_version=kwargs.get("model_package_version"),
            model_package_checksum=kwargs.get("model_package_checksum"),
            original_shud_filename=kwargs.get("original_shud_filename"),
        )
        return SimpleNamespace(state_id=state_id, status="created", snapshot=snapshot)

    def run_qc(self, state_id: str) -> bool:
        self.qc_runs.append(state_id)
        return True


def _round_trip_dirs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """`_run_shud_dirs` in the geometry the publish gate probes.

    `save_state_for_run` looks for `<workspace>/runs/<run_id>/output`, so the
    round-trip has to run the solve into exactly that directory instead of the
    flat `workspace/output` the other runtime tests use.
    """

    workspace = tmp_path / "workspace"
    output_dir = workspace / "runs" / _manifest()["run_id"] / "output"
    log_dir = workspace / "logs"
    log_dir.mkdir(parents=True)
    cfg_path = workspace / "input" / "demo.cfg.para"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(
        "START_TIME = 2026-05-01T00:00:00Z\n"
        "END_TIME = 2026-05-04T00:00:00Z\n"
        f"OUTPUT_DIR = {output_dir}\n"
        "MODEL_OUTPUT_INTERVAL = 1440\n"
        "SEGMENT_COUNT = 2\n"
        "INIT_MODE = 1\n",
        encoding="utf-8",
    )
    return workspace, output_dir, log_dir, cfg_path


def _round_trip_run_context() -> Any:
    """The publish-side context for the run `_manifest()` describes."""

    from datetime import UTC, datetime

    from packages.common.state_cli import StateRunContext

    manifest = _manifest()
    return StateRunContext(
        run_id=manifest["run_id"],
        model_id=manifest["model"]["model_id"],
        end_time=datetime(2026, 5, 4, tzinfo=UTC),
        output_uri=None,
        source_id=manifest["source_id"],
        cycle_time=datetime(2026, 5, 1, tzinfo=UTC),
        model_package_version=manifest["model"]["model_package_uri"],
        model_package_checksum="package-sha-1",
    )


def test_writer_manifest_round_trips_through_the_publish_gate(tmp_path: Path) -> None:
    """#1325 A11(a): a real captured checkpoint tree passes the gate and publishes."""

    from packages.common.state_cli import save_state_for_run

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _round_trip_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]

    runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    manager = _RoundTripStateManager(LocalObjectStore(tmp_path / "object-store", "s3://nhms"))
    result = save_state_for_run(
        manifest["run_id"],
        manager=manager,
        run_context=_round_trip_run_context(),
        workspace_root=workspace,
    )

    from datetime import UTC, datetime

    assert len(manager.saved) == 1
    assert manager.saved[0]["valid_time"] == datetime(2026, 5, 1, 12, tzinfo=UTC)
    assert manager.saved[0]["lead_hours"] == 12
    assert manager.saved[0]["original_shud_filename"] == "demo.f012.cfg.ic.update"
    assert manager.qc_runs == [result["state_id"]]


def test_zero_hour_writer_manifest_round_trips_through_the_gate_without_slurm_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1325 A11(b): the fallback lane round-trips off Slurm, null job facts and all.

    Outside a Slurm allocation the writer records `slurm_job_id` and
    `array_task_id` as null; G3 requires the KEYS, not the values, so this also
    pins the null branch the Slurm-env provenance anchor cannot reach.
    """

    from packages.common.state_cli import save_state_for_run

    for name in ("SLURM_ARRAY_JOB_ID", "SLURM_JOB_ID", "SLURM_ARRAY_TASK_ID"):
        monkeypatch.delenv(name, raising=False)
    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _round_trip_dirs(tmp_path)
    manifest = _manifest()  # no `state_checkpoint_hours`: the analysis / short-horizon lane

    runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    provenance = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )["provenance"]
    assert provenance["slurm_job_id"] is None
    assert provenance["array_task_id"] is None
    assert provenance["requested_checkpoint_hours"] == []

    manager = _RoundTripStateManager(LocalObjectStore(tmp_path / "object-store", "s3://nhms"))
    result = save_state_for_run(
        manifest["run_id"],
        manager=manager,
        run_context=_round_trip_run_context(),
        workspace_root=workspace,
    )

    from datetime import UTC, datetime

    assert len(manager.saved) == 1
    assert manager.saved[0]["valid_time"] == datetime(2026, 5, 4, tzinfo=UTC)
    assert manager.saved[0]["original_shud_filename"] == "demo.cfg.ic.update"
    assert manager.qc_runs == [result["state_id"]]


def test_run_shud_recovery_records_cfg_read_failure_for_every_missing_hour(tmp_path: Path) -> None:
    """#1315 r3-audit: an unreadable cfg aborts recovery without changing the contract.

    The recovery loop shortens the run's own cfg, so it must read it first. If
    that read fails there is nothing to shorten for ANY hour — but the failure
    belongs to the recovery lane, not to the caller: the run must still fail
    with `STATE_CHECKPOINTS_MISSING`, still write the manifest, and say per hour
    that recovery never got as far as spawning anything.
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [6, 12]
    cfg_before = cfg_path.read_bytes()

    real_read = runtime_module._read_text_no_follow

    def _fail_cfg_read(path: Path, **kwargs: Any) -> str:
        if Path(path) == cfg_path:
            raise SHUDRuntimeError("WORKSPACE_READ_FAILED", f"Failed to read staged file {path}: I/O error")
        return real_read(path, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime_module, "_read_text_no_follow", _fail_cfg_read)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f006=cfg_read_failed" in exc_info.value.message
    assert "f012=cfg_read_failed" in exc_info.value.message
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert payload["recovery_outcomes"] == {"6": "cfg_read_failed", "12": "cfg_read_failed"}
    # No hour got far enough to create a scratch dir or touch the cfg.
    assert not (workspace / "state_checkpoint_recovery").exists()
    assert cfg_path.read_bytes() == cfg_before


def test_run_shud_recovery_records_install_write_failure_distinctly(tmp_path: Path) -> None:
    """#1315 r3-audit: a failed publish of the recovered state is its own lane.

    `install_recovered` writes the accepted candidate into
    `output/state_checkpoints/`. That write is the one step that has already
    passed the rerun, so "the engine produced nothing" and "the engine produced
    a state we could not publish" must not collapse into the same outcome
    string: the first is a solver bug report, the second is a workspace/disk
    one. The hour stays missing either way.
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]

    real_write = runtime_module._write_staged_bytes

    def _fail_checkpoint_publish(path: Path, content: bytes, **kwargs: Any) -> Path:
        if Path(path).name == "demo.f012.cfg.ic.update":
            raise SHUDRuntimeError("WORKSPACE_WRITE_FAILED", f"Failed to write staged file {path}: no space left")
        return real_write(path, content, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime_module, "_write_staged_bytes", _fail_checkpoint_publish)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f012=install_write_failed" in exc_info.value.message
    assert not (output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update").exists()
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert payload["checkpoints"] == []
    assert payload["recovery_outcomes"] == {"12": "install_write_failed"}


def test_run_shud_recovery_refuses_a_symlinked_scratch_entry_without_following_it(tmp_path: Path) -> None:
    """#1315 r3-audit: a symlink in the scratch dir is refused, not resolved.

    Sibling of the sub-directory refusal: a symlink is rejected one step
    earlier, by the no-follow stat itself (`SafeFilesystemError`, not a
    "not a regular file" verdict). Both the unlink pass and the rerun are
    skipped for that hour, so a link planted in a reused workspace can neither
    be followed to a file outside the workspace nor make that file disappear.
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    outside_state = tmp_path / "outside_state.cfg.ic.update"
    outside_state.write_text(_STALE_SCRATCH_STATE, encoding="utf-8")
    scratch = workspace / "state_checkpoint_recovery" / "f012"
    scratch.mkdir(parents=True)
    (scratch / "demo.cfg.ic.update").symlink_to(outside_state)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f012=skipped_scratch_unclean" in exc_info.value.message
    # Neither followed (no checkpoint installed from the link target) nor
    # unlinked (the link and its target both survive for diagnosis).
    assert not (output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update").exists()
    assert (scratch / "demo.cfg.ic.update").is_symlink()
    assert outside_state.read_text(encoding="utf-8") == _STALE_SCRATCH_STATE
    refusal_log = (log_dir / "state_checkpoint_recovery_f012.err.log").read_text(encoding="utf-8")
    assert "not safely inspectable" in refusal_log


def test_run_shud_recovery_refuses_when_a_scratch_entry_cannot_be_removed(tmp_path: Path) -> None:
    """#1315 r3-audit: a scratch entry that survives the clear blocks the rerun.

    The clear is what makes a fixed-filename install safe. If an entry cannot be
    unlinked (busy/immutable/read-only mount) the dir is NOT proven empty, so
    the rerun must not start — otherwise a rerun that exits 0 without writing
    would publish the surviving stale state as this cycle's checkpoint.
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    scratch = workspace / "state_checkpoint_recovery" / "f012"
    scratch.mkdir(parents=True)
    stale_state = scratch / "demo.cfg.ic.update"
    stale_state.write_text(_STALE_SCRATCH_STATE, encoding="utf-8")

    real_unlink = runtime_module.unlink_no_follow

    def _fail_scratch_unlink(path: Path, **kwargs: Any) -> None:
        if "state_checkpoint_recovery" in Path(path).parts:
            raise SafeFilesystemError(f"Failed to unlink {path}: device busy", kind="io")
        real_unlink(path, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime_module, "unlink_no_follow", _fail_scratch_unlink)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f012=skipped_scratch_unclean" in exc_info.value.message
    assert stale_state.read_text(encoding="utf-8") == _STALE_SCRATCH_STATE
    assert not (output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update").exists()
    refusal_log = (log_dir / "state_checkpoint_recovery_f012.err.log").read_text(encoding="utf-8")
    assert "could not be removed" in refusal_log


def test_run_shud_recovery_refuses_a_scratch_dir_above_the_entry_bound(tmp_path: Path) -> None:
    """#1315 r3-audit: an implausibly large scratch dir is refused, not walked.

    A SHUD run leaves a few dozen files in an output dir. A directory holding
    orders of magnitude more is not one this runtime produced, so the bound
    stops it before the clear turns into an unbounded mass unlink inside the
    workspace.
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    scratch = workspace / "state_checkpoint_recovery" / "f012"
    scratch.mkdir(parents=True)
    over_bound = runtime_module.MAX_RECOVERY_SCRATCH_ENTRIES + 1
    for index in range(over_bound):
        (scratch / f"residue_{index:05d}.txt").write_text("x\n", encoding="utf-8")

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f012=skipped_scratch_unclean" in exc_info.value.message
    # Nothing was unlinked and no rerun was spawned.
    assert len(list(scratch.iterdir())) == over_bound
    assert not (log_dir / "state_checkpoint_recovery_f012.out.log").exists()
    refusal_log = (log_dir / "state_checkpoint_recovery_f012.err.log").read_text(encoding="utf-8")
    assert f"{over_bound} entries" in refusal_log
    assert f"above the {runtime_module.MAX_RECOVERY_SCRATCH_ENTRIES} bound" in refusal_log


def test_run_shud_recovery_refuses_an_unreadable_scratch_dir(tmp_path: Path) -> None:
    """#1315 r3-audit: a scratch dir that cannot be listed is refused, not assumed empty.

    If the listing fails there is no evidence about what the dir holds, and the
    install step reads a fixed filename out of it — so treating the failure as
    "nothing to clear" would let unknown residue become the checkpoint.
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    scratch = workspace / "state_checkpoint_recovery" / "f012"
    scratch.mkdir(parents=True)
    (scratch / "demo.cfg.ic.update").write_text(_STALE_SCRATCH_STATE, encoding="utf-8")

    real_listdir = os.listdir

    def _unreadable_scratch_listdir(path: Any, *args: Any, **kwargs: Any) -> list[str]:
        if Path(path) == scratch:
            raise PermissionError(13, "Permission denied")
        return real_listdir(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "listdir", _unreadable_scratch_listdir)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    assert exc_info.value.error_code == "STATE_CHECKPOINTS_MISSING"
    assert "f012=skipped_scratch_unclean" in exc_info.value.message
    assert not (output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update").exists()
    refusal_log = (log_dir / "state_checkpoint_recovery_f012.err.log").read_text(encoding="utf-8")
    assert "scratch dir is unreadable" in refusal_log


def test_run_shud_recovery_tolerates_a_scratch_entry_that_vanishes_mid_scan(tmp_path: Path) -> None:
    """#1315 r3-audit: an entry disappearing between listing and stat is not a refusal.

    The scan is two-pass, so an entry can be gone by the time it is stat'ed (a
    temp file the previous attempt's engine was still cleaning up). Refusing on
    that race would turn a self-healed workspace into a permanent hard failure
    for the hour; the entry is simply skipped and the rest of the clear — plus
    the rerun — proceeds.
    """

    stub = tmp_path / "fast_solver.py"
    stub.write_text(_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    workspace, output_dir, log_dir, cfg_path = _run_shud_dirs(tmp_path)
    manifest = _manifest()
    manifest["runtime"]["state_checkpoint_hours"] = [12]
    scratch = workspace / "state_checkpoint_recovery" / "f012"
    scratch.mkdir(parents=True)
    (scratch / "demo.cfg.ic.update").write_text(_STALE_SCRATCH_STATE, encoding="utf-8")
    vanishing = scratch / "vanishing.tmp"
    vanishing.write_text("half-written\n", encoding="utf-8")

    real_stat = runtime_module.stat_no_follow

    def _vanishing_stat(path: Path, **kwargs: Any) -> Any:
        if Path(path).name == "vanishing.tmp":
            raise FileNotFoundError(2, "No such file or directory")
        return real_stat(path, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runtime_module, "stat_no_follow", _vanishing_stat)
        runtime.run_shud(manifest, cfg_path, workspace, output_dir, log_dir)

    checkpoint = output_dir / "state_checkpoints" / "demo.f012.cfg.ic.update"
    # Fresh rerun content: the stale sibling was still cleared and replaced.
    assert checkpoint.read_text(encoding="utf-8") == "2 2 720.000000\n1 0.1\n2 0.2\n1 0\n2 0\n"
    payload = json.loads(
        (output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8")
    )
    assert payload["recovery_outcomes"] == {"12": "recovered"}
    # Only the entry that could be stat'ed was unlinked.
    assert vanishing.read_text(encoding="utf-8") == "half-written\n"


def test_prepare_workspace_blocks_missing_project_inputs(tmp_path: Path) -> None:
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    input_dir = tmp_path / "workspace" / "runs" / "demo" / "input"
    input_dir.mkdir(parents=True)
    # Stage nothing -> required *.mesh/*.para/*.calib/*.tsd.forc are absent.
    manifest = _manifest()
    manifest["model"]["model_package_uri"] = "s3://nhms/models/absent/package/"

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.prepare_workspace(manifest, input_dir)

    assert exc_info.value.error_code in {"ARTIFACT_NOT_FOUND", "WORKSPACE_INCOMPLETE"}


def test_shared_preflight_passes_for_valid_binary_and_redacts(tmp_path: Path) -> None:
    from packages.common.shud_preflight import check_shud_executable

    binary = _write_real_shud_behavior_binary(tmp_path / "shud_omp")

    result = check_shud_executable(str(binary))

    assert result.ok is True
    assert result.blockers == []


def test_shared_preflight_accepts_real_shud_no_arg_only_banner(tmp_path: Path) -> None:
    """Regression for the node-22 real-binary finding.

    The genuine SHUD binary rejects --version/--help ("Unknown option") and only
    prints its identity banner when run with no arguments. The preflight must NOT
    mark such a binary as missing a version signal.
    """

    import packages.common.shud_preflight as preflight

    binary = _write_real_shud_behavior_binary(tmp_path / "shud")

    # ldd is unavailable on macOS dev hosts; skip the library probe so the test
    # exercises the version-signal path deterministically across platforms.
    result = preflight.check_shud_executable(str(binary), probe_libraries=False)

    assert result.ok is True
    assert result.checks["version_signal"] == "present"
    assert not any(
        b["error_code"] == "SHUD_EXECUTABLE_VERSION_SIGNAL_MISSING" for b in result.blockers
    )


def test_shared_preflight_no_arg_probe_runs_in_isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-argument probe must not run inside a real project directory."""

    import packages.common.shud_preflight as preflight

    binary = _write_real_shud_behavior_binary(tmp_path / "shud")
    seen_cwds: list[str | None] = []
    real_run = preflight.subprocess.run

    def _capturing_run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        if command == [str(binary)]:
            seen_cwds.append(kwargs.get("cwd"))
            assert kwargs.get("stdin") is preflight.subprocess.DEVNULL
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(preflight.subprocess, "run", _capturing_run)

    preflight.check_shud_executable(str(binary), probe_libraries=False)

    assert seen_cwds, "no-argument probe must execute the binary"
    # cwd is a dedicated temp dir, never the caller's working directory.
    assert all(cwd is not None and Path(cwd) != Path.cwd() for cwd in seen_cwds)


# Variant binary: a no-argument call prints a non-empty banner that contains NONE
# of the recognized SHUD identity tokens (no 'shud', no 'simulator for hydrologic',
# no 'hydrologic unstructured domains'), exits 0; flags report "Unknown option".
# This models a real/variant solver whose banner wording is simply not in the token
# list -- it must NOT be falsely rejected (never-break-userspace).
_UNRECOGNIZED_BANNER_SCRIPT = (
    "#!/bin/sh\n"
    'if [ "$#" -gt 0 ]; then\n'
    '  echo "Unknown option: $1" >&2\n'
    "  exit 1\n"
    "fi\n"
    'echo "Hydro Solver build 2024"\n'
    "exit 0\n"
)


# Silent stub: no-argument call produces NO output and exits 0 (a renamed /bin/true).
_SILENT_STUB_SCRIPT = "#!/bin/sh\nexit 0\n"


def test_shared_preflight_tolerates_unrecognized_nonempty_banner(tmp_path: Path) -> None:
    """never-break-userspace lock: a binary that runs and prints a non-empty banner
    we simply do not recognize must be tolerated (inconclusive), never blocked.
    """

    import packages.common.shud_preflight as preflight

    binary = tmp_path / "shud"
    binary.write_text(_UNRECOGNIZED_BANNER_SCRIPT, encoding="utf-8")
    binary.chmod(0o755)

    result = preflight.check_shud_executable(str(binary), probe_libraries=False)

    assert result.ok is True
    assert result.checks["version_signal"] == "inconclusive"
    assert result.blockers == []


def test_shared_preflight_rejects_silent_stub(tmp_path: Path) -> None:
    """A no-argument call that produces NO output (renamed /bin/true) is positive
    stub evidence and must be blocked with SHUD_EXECUTABLE_SILENT_STUB.
    """

    import packages.common.shud_preflight as preflight

    binary = tmp_path / "shud"
    binary.write_text(_SILENT_STUB_SCRIPT, encoding="utf-8")
    binary.chmod(0o755)

    result = preflight.check_shud_executable(str(binary), probe_libraries=False)

    assert result.ok is False
    assert result.checks["version_signal"] == "silent"
    assert any(b["error_code"] == "SHUD_EXECUTABLE_SILENT_STUB" for b in result.blockers)


def test_shared_preflight_rejects_realpath_stub_symlink(tmp_path: Path) -> None:
    """A symlink named ``shud`` pointing at /bin/true is rejected by the
    basename/realpath stub branch (independent of the version probe).
    """

    import packages.common.shud_preflight as preflight

    link = tmp_path / "shud"
    try:
        link.symlink_to("/bin/true")
    except OSError as exc:  # pragma: no cover - platform without symlink support
        pytest.skip(f"symlink creation is not supported: {exc}")

    result = preflight.check_shud_executable(str(link))

    assert result.ok is False
    assert any(b["error_code"] == "SHUD_EXECUTABLE_STUB_REJECTED" for b in result.blockers)


def test_shared_preflight_unknown_signal_when_probes_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If every subprocess probe errors, the signal is ``unknown`` and the
    preflight does not fabricate a version blocker.
    """

    import packages.common.shud_preflight as preflight

    binary = _write_real_shud_behavior_binary(tmp_path / "shud")

    def _always_fail(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("probe cannot run")

    monkeypatch.setattr(preflight.subprocess, "run", _always_fail)

    result = preflight.check_shud_executable(str(binary), probe_libraries=False)

    assert result.checks["version_signal"] == "unknown"
    assert not any(
        b["error_code"] in {"SHUD_EXECUTABLE_SILENT_STUB", "SHUD_EXECUTABLE_VERSION_SIGNAL_MISSING"}
        for b in result.blockers
    )
    assert result.ok is True


def test_shared_preflight_reports_missing_libraries_safely(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import packages.common.shud_preflight as preflight

    binary = tmp_path / "shud_omp"
    binary.write_text('#!/bin/sh\necho "SHUD"\n', encoding="utf-8")
    binary.chmod(0o755)

    monkeypatch.setattr(preflight, "_missing_shared_libraries", lambda _resolved: ["libsecret-token.so.1"])

    result = preflight.check_shud_executable(str(binary), probe_version=False)

    assert result.ok is False
    library_blockers = [b for b in result.blockers if b["error_code"] == "SHUD_EXECUTABLE_LIBRARY_MISSING"]
    assert library_blockers
    assert library_blockers[0]["library"] == "libsecret-token.so.1"
    import json as _json

    assert "password=" not in _json.dumps(result.blockers)


# ---------------------------------------------------------------------------
# #1164: packaged-IC consumption is fail-closed.
#
# A manifest declaring ``quality=packaged_calibrated_state`` MUST consume the
# packaged ``*.cfg.ic`` staged from the model package or raise
# ``PACKAGED_IC_CONSUMPTION_FAILED``.  Two declaration forms exist:
#   * scheduler-produced: no ``state_id``, ``packaged_ic_checksum`` recorded;
#   * legacy manual manifest (``scripts/create_qhh_shud_manifest.py``):
#     a ``state_id`` and NO recorded packaged digest.
#
# Deliberate non-goal (design D4): packaged ICs are timeless calibration
# products, so warm-start three-way time-consistency verification and IC time
# shifting are NOT applied here — there is no snapshot ``valid_time`` to agree
# with.  (In ``shud_project`` mode the pre-existing project-forcing preparation
# still re-stamps the header to the forcing start; that is untouched.)
# ---------------------------------------------------------------------------


PACKAGED_IC_CONTENT = b"2\t1\t29626560.000000\n1\t0.1\t0.2\t0.3\t0.4\n2\t0.1\t0.2\t0.3\t0.4\n1\t0.0\n"


def _cfg_init_mode(cfg_path: Path) -> str | None:
    """Return the INIT_MODE token from a generated ``.cfg.para``.

    ``shud_project`` mode writes ``INIT_MODE\t<value>`` while the legacy style
    writes ``INIT_MODE = <value>``; parse tokens so the assertion is style-blind.
    """
    for line in cfg_path.read_text(encoding="utf-8").splitlines():
        tokens = [token for token in line.replace("=", " ").split() if token]
        if tokens and tokens[0] == "INIT_MODE" and len(tokens) >= 2:
            return tokens[1]
    return None


def _write_packaged_ic(
    object_root: Path,
    content: bytes = PACKAGED_IC_CONTENT,
    *,
    name: str = "alias-a.cfg.ic",
) -> str:
    package = object_root / "models" / "basins_basin_a_shud" / "vbasins-test" / "package"
    (package / name).write_bytes(content)
    return sha256_bytes(content)


def _packaged_manifest(
    checksums: dict[str, str],
    *,
    state_id: str | None = None,
    packaged_ic_checksum: str | None = None,
) -> dict[str, Any]:
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    initial_state: dict[str, Any] = {
        "state_id": state_id,
        "ic_file_uri": None,
        "valid_time": None,
        "checksum": None,
        "quality": "packaged_calibrated_state",
    }
    if packaged_ic_checksum is not None:
        initial_state["packaged_ic_checksum"] = packaged_ic_checksum
    manifest["initial_state"] = initial_state
    manifest["runtime"]["init_mode"] = 3
    return manifest


def test_packaged_ic_is_consumed_with_init_mode_3_when_checksum_matches(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    ic_sha256 = _write_packaged_ic(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _packaged_manifest(checksums, packaged_ic_checksum=ic_sha256)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    output_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)
    cfg_path = runtime.generate_cfg_para(manifest, input_dir, output_dir)

    assert (input_dir / "alias-a" / "alias-a.cfg.ic").is_file()
    assert _cfg_init_mode(cfg_path) == "3"
    assert manifest["runtime"]["init_mode"] == 3
    assert manifest["initial_state"]["quality"] == "packaged_calibrated_state"
    assert manifest["initial_state"]["checksum"] == ic_sha256
    packaged_evidence = manifest["runtime"]["packaged_initial_state"]
    assert packaged_evidence["checksum_verified"] is True
    assert packaged_evidence["relative_path"].endswith("alias-a.cfg.ic")
    assert packaged_evidence.get("warnings") in (None, [])


def test_packaged_ic_legacy_manifest_form_records_skipped_checksum_warning(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    _write_packaged_ic(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    # ``create_qhh_shud_manifest.py`` shape: state_id set, checksum None.
    manifest = _packaged_manifest(checksums, state_id="qhh_packaged_calibrated_state")
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    output_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)
    cfg_path = runtime.generate_cfg_para(manifest, input_dir, output_dir)

    assert _cfg_init_mode(cfg_path) == "3"
    packaged_evidence = manifest["runtime"]["packaged_initial_state"]
    assert packaged_evidence["checksum_verified"] is False
    assert any(
        "checksum" in str(warning).lower() for warning in packaged_evidence.get("warnings") or []
    )


def test_packaged_ic_negative_residuals_are_normalized_before_execution(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    # Bounded residuals: the domain-mean unsat correction must stay under the
    # 0.2 mm ceiling, otherwise the shared policy refuses the state outright.
    residual_content = (
        b"2\t1\t29626560.000000\n1\t0.1\t0.2\t0.3\t-0.0001\n2\t0.1\t0.2\t0.3\t-0.0002\n1\t0.0\n"
    )
    ic_sha256 = _write_packaged_ic(object_root, residual_content)
    checksums = _write_standard_shud_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _packaged_manifest(checksums, packaged_ic_checksum=ic_sha256)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    normalization = manifest["runtime"]["initial_state_normalization"]
    assert normalization["accepted"] is True
    assert normalization["normalized_value_count"] == 2
    materialized = (input_dir / "alias-a" / "alias-a.cfg.ic").read_text(encoding="utf-8")
    assert "-0.0001" not in materialized
    assert "-0.0002" not in materialized


@pytest.mark.parametrize("state_id", [None, "qhh_packaged_calibrated_state"], ids=["scheduler_form", "legacy_form"])
def test_packaged_ic_missing_file_fails_closed_without_init_mode_1(
    tmp_path: Path, state_id: str | None
) -> None:
    """Behavioral negative lock: BOTH declaration forms must raise, never cold-start."""
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)  # NOTE: no packaged *.cfg.ic in the package.
    checksums = _write_standard_shud_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    packaged_ic_checksum = sha256_bytes(PACKAGED_IC_CONTENT) if state_id is None else None
    manifest = _packaged_manifest(
        checksums, state_id=state_id, packaged_ic_checksum=packaged_ic_checksum
    )
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    output_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as excinfo:
        runtime.prepare_workspace(manifest, input_dir)

    assert excinfo.value.error_code == "PACKAGED_IC_CONSUMPTION_FAILED"
    # Behavioral lock: no INIT_MODE 1 execution is reachable after the failure.
    assert manifest["runtime"]["init_mode"] == 3
    assert manifest["initial_state"]["quality"] == "packaged_calibrated_state"
    cfg_path = runtime.generate_cfg_para(manifest, input_dir, output_dir)
    assert _cfg_init_mode(cfg_path) != "1"


def test_packaged_ic_zero_byte_file_fails_closed(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    _write_packaged_ic(object_root, b"")
    checksums = _write_standard_shud_forcing(object_root)
    runtime = _runtime(tmp_path, FakeHydroRunRepository())
    manifest = _packaged_manifest(checksums, packaged_ic_checksum=sha256_bytes(b""))
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as excinfo:
        runtime.prepare_workspace(manifest, input_dir)

    assert excinfo.value.error_code == "PACKAGED_IC_CONSUMPTION_FAILED"
    assert manifest["runtime"]["init_mode"] == 3


def test_packaged_ic_checksum_mismatch_fails_closed(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    _write_packaged_ic(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    runtime = _runtime(tmp_path, FakeHydroRunRepository())
    manifest = _packaged_manifest(checksums, packaged_ic_checksum="a" * 64)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as excinfo:
        runtime.prepare_workspace(manifest, input_dir)

    assert excinfo.value.error_code == "PACKAGED_IC_CONSUMPTION_FAILED"
    assert manifest["runtime"]["init_mode"] == 3


def test_packaged_ic_unparseable_header_fails_closed(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    bad_header = b"mesh\triver\n1\t0.1\n"
    ic_sha256 = _write_packaged_ic(object_root, bad_header)
    checksums = _write_standard_shud_forcing(object_root)
    runtime = _runtime(tmp_path, FakeHydroRunRepository())
    manifest = _packaged_manifest(checksums, packaged_ic_checksum=ic_sha256)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as excinfo:
        runtime.prepare_workspace(manifest, input_dir)

    assert excinfo.value.error_code == "PACKAGED_IC_CONSUMPTION_FAILED"


@pytest.mark.parametrize(
    "content",
    [
        b"\xff\xfe\x00\x01 2 1 29626560.0\n1\t0.1\t0.2\t0.3\t0.4\n",
        bytes(range(256)) * 4,
    ],
    ids=["binary_header", "fully_binary"],
)
def test_packaged_ic_non_utf8_file_fails_closed(tmp_path: Path, content: bytes) -> None:
    """A binary packaged IC is a typed consumption failure, not an untyped RUNTIME_ERROR."""
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    ic_sha256 = _write_packaged_ic(object_root, content)
    checksums = _write_standard_shud_forcing(object_root)
    runtime = _runtime(tmp_path, FakeHydroRunRepository())
    manifest = _packaged_manifest(checksums, packaged_ic_checksum=ic_sha256)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as excinfo:
        runtime.prepare_workspace(manifest, input_dir)

    assert excinfo.value.error_code == "PACKAGED_IC_CONSUMPTION_FAILED"
    assert manifest["runtime"]["init_mode"] == 3


def test_packaged_ic_reprepare_is_idempotent_when_package_ic_is_renamed(tmp_path: Path) -> None:
    """A re-prepared cycle-stable workspace must converge, not wedge (#1164 idempotence).

    The package ships its IC under a NON-canonical basename, so attempt 1
    materializes ``<project>.cfg.ic`` and unlinks the staged source.  Attempt 2
    clears every staged ``*.cfg.ic`` BEFORE re-staging, so the candidate set is
    again exactly what this staging produced — one file — instead of the two
    (previous materialization + restored source) that would wedge the run.
    """
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    ic_sha256 = _write_packaged_ic(object_root, name="calibrated.cfg.ic")
    checksums = _write_standard_shud_forcing(object_root)
    runtime = _runtime(tmp_path, FakeHydroRunRepository())
    first_manifest = _packaged_manifest(checksums, packaged_ic_checksum=ic_sha256)
    input_dir = tmp_path / "workspace" / "runs" / first_manifest["run_id"] / "input"
    output_dir = tmp_path / "workspace" / "runs" / first_manifest["run_id"] / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    runtime.prepare_workspace(first_manifest, input_dir)
    materialized = input_dir / "alias-a" / "alias-a.cfg.ic"
    first_bytes = materialized.read_bytes()
    first_initial_state = deepcopy(first_manifest["initial_state"])
    first_evidence = deepcopy(first_manifest["runtime"]["packaged_initial_state"])
    assert (input_dir / "alias-a" / "calibrated.cfg.ic").exists() is False

    second_manifest = _packaged_manifest(checksums, packaged_ic_checksum=ic_sha256)
    runtime.prepare_workspace(second_manifest, input_dir)
    cfg_path = runtime.generate_cfg_para(second_manifest, input_dir, output_dir)

    assert second_manifest["initial_state"] == first_initial_state
    assert second_manifest["runtime"]["packaged_initial_state"] == first_evidence
    assert materialized.read_bytes() == first_bytes
    assert sorted(path.name for path in (input_dir / "alias-a").glob("*.cfg.ic")) == ["alias-a.cfg.ic"]
    assert _cfg_init_mode(cfg_path) == "3"


def test_packaged_ic_subdirectory_candidate_still_fails_the_exactly_one_check(tmp_path: Path) -> None:
    """A package that STAGES two ICs is ambiguous and must fail closed.

    The clear-before-stage invariant only removes what a PREVIOUS preparation
    left behind; a single staging that produces two candidates is genuinely
    ambiguous input and still raises.
    """
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    ic_sha256 = _write_packaged_ic(object_root)
    package = object_root / "models" / "basins_basin_a_shud" / "vbasins-test" / "package"
    (package / "CALIB").mkdir(parents=True, exist_ok=True)
    (package / "CALIB" / "stray.cfg.ic").write_bytes(PACKAGED_IC_CONTENT)
    checksums = _write_standard_shud_forcing(object_root)
    runtime = _runtime(tmp_path, FakeHydroRunRepository())
    manifest = _packaged_manifest(checksums, packaged_ic_checksum=ic_sha256)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as excinfo:
        runtime.prepare_workspace(manifest, input_dir)

    assert excinfo.value.error_code == "PACKAGED_IC_CONSUMPTION_FAILED"
    assert "exactly one is required" in excinfo.value.message


def test_packaged_ic_reprepare_converges_when_the_package_ic_lives_in_a_subdirectory(
    tmp_path: Path,
) -> None:
    """C2: re-preparing a subdirectory-IC package converges instead of wedging.

    Attempt 1 materializes ``CALIB/calibrated.cfg.ic`` to the top-level
    ``<project>.cfg.ic``; attempt 2 re-stages the source from the package.  The
    convergence requirement is unconditional — it must not depend on the two
    candidates happening to be siblings.
    """
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    package = object_root / "models" / "basins_basin_a_shud" / "vbasins-test" / "package"
    (package / "CALIB").mkdir(parents=True, exist_ok=True)
    (package / "CALIB" / "calibrated.cfg.ic").write_bytes(PACKAGED_IC_CONTENT)
    ic_sha256 = sha256_bytes(PACKAGED_IC_CONTENT)
    checksums = _write_standard_shud_forcing(object_root)
    runtime = _runtime(tmp_path, FakeHydroRunRepository())
    first_manifest = _packaged_manifest(checksums, packaged_ic_checksum=ic_sha256)
    input_dir = tmp_path / "workspace" / "runs" / first_manifest["run_id"] / "input"
    output_dir = tmp_path / "workspace" / "runs" / first_manifest["run_id"] / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    runtime.prepare_workspace(first_manifest, input_dir)
    materialized = input_dir / "alias-a" / "alias-a.cfg.ic"
    first_initial_state = deepcopy(first_manifest["initial_state"])
    first_evidence = deepcopy(first_manifest["runtime"]["packaged_initial_state"])

    second_manifest = _packaged_manifest(checksums, packaged_ic_checksum=ic_sha256)
    runtime.prepare_workspace(second_manifest, input_dir)
    cfg_path = runtime.generate_cfg_para(second_manifest, input_dir, output_dir)

    assert second_manifest["initial_state"] == first_initial_state
    assert second_manifest["runtime"]["packaged_initial_state"] == first_evidence
    assert materialized.read_bytes() == PACKAGED_IC_CONTENT
    assert sorted(str(path.relative_to(input_dir)) for path in input_dir.rglob("*.cfg.ic")) == [
        "alias-a/alias-a.cfg.ic"
    ]
    assert _cfg_init_mode(cfg_path) == "3"


def _write_two_top_level_packaged_ics(object_root: Path) -> tuple[str, bytes]:
    """Publish a package holding TWO top-level ``*.cfg.ic`` files.

    Returns the canonical file's digest and the stray file's content, so a test
    can prove which one (if any) the runtime consumed.
    """
    canonical_sha256 = _write_packaged_ic(object_root)
    stray_content = b"9\t9\t11111111.000000\n1\t9.9\t9.9\t9.9\t9.9\n"
    _write_packaged_ic(object_root, stray_content, name="stray.cfg.ic")
    return canonical_sha256, stray_content


def test_packaged_ic_two_top_level_siblings_fail_closed_without_deleting_the_canonical(
    tmp_path: Path,
) -> None:
    """C1: a package that legitimately ships two top-level ICs is ambiguous.

    The round-1 sibling-drop heuristic deleted the canonical file and consumed
    the stray one here.  Exactly-one must fire instead, and the staged canonical
    IC must still be on disk afterwards.
    """
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    canonical_sha256, _stray = _write_two_top_level_packaged_ics(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    runtime = _runtime(tmp_path, FakeHydroRunRepository())
    manifest = _packaged_manifest(checksums, packaged_ic_checksum=canonical_sha256)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as excinfo:
        runtime.prepare_workspace(manifest, input_dir)

    assert excinfo.value.error_code == "PACKAGED_IC_CONSUMPTION_FAILED"
    assert "exactly one is required" in excinfo.value.message
    # The canonical staged IC must survive the refusal — nothing may be dropped
    # on a guess about which candidate is stale.
    assert (input_dir / "alias-a" / "alias-a.cfg.ic").is_file()
    assert (input_dir / "alias-a" / "stray.cfg.ic").is_file()


def test_packaged_ic_legacy_form_with_two_siblings_raises_instead_of_consuming_a_stray(
    tmp_path: Path,
) -> None:
    """C1, legacy form: without a recorded digest nothing catches a wrong pick."""
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    _canonical_sha256, stray_content = _write_two_top_level_packaged_ics(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    runtime = _runtime(tmp_path, FakeHydroRunRepository())
    manifest = _packaged_manifest(checksums, state_id="qhh_packaged_calibrated_state")
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    with pytest.raises(SHUDRuntimeError) as excinfo:
        runtime.prepare_workspace(manifest, input_dir)

    assert excinfo.value.error_code == "PACKAGED_IC_CONSUMPTION_FAILED"
    assert "exactly one is required" in excinfo.value.message
    assert (input_dir / "alias-a" / "alias-a.cfg.ic").read_bytes() == PACKAGED_IC_CONTENT
    assert (input_dir / "alias-a" / "stray.cfg.ic").read_bytes() == stray_content
    assert "packaged_initial_state" not in manifest.get("runtime", {})


def test_packaged_ic_unclearable_staged_states_are_a_typed_consumption_failure(
    tmp_path: Path,
) -> None:
    """The pre-staging clear fails closed under the packaged-IC code, not silently.

    A symlink inside the staged model input makes the recursive clear refuse
    (``ARTIFACT_UNSAFE``).  "The clear could not be completed" must never be read
    as "there was nothing to clear", because that is exactly what would let a
    previous attempt's materialization survive into the exactly-one search.

    This also pins the reason ``_consume_packaged_initial_state``'s pre-tail
    candidate search needs no wrapper of its own: the clear walks the SAME
    directory first, with the same no-follow enumerator and under the same
    declaration predicate, so a staged tree the search could not enumerate is
    already refused here — under the packaged-IC error code.
    """
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    ic_sha256 = _write_packaged_ic(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    runtime = _runtime(tmp_path, FakeHydroRunRepository())
    manifest = _packaged_manifest(checksums, packaged_ic_checksum=ic_sha256)
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    model_input_dir = input_dir / "alias-a"
    model_input_dir.mkdir(parents=True)
    (model_input_dir / "stale-link.cfg.ic").symlink_to(tmp_path / "object-store")

    with pytest.raises(SHUDRuntimeError) as excinfo:
        runtime.prepare_workspace(manifest, input_dir)

    assert excinfo.value.error_code == "PACKAGED_IC_CONSUMPTION_FAILED"
    assert "could not be cleared" in excinfo.value.message


def test_undeclared_packaged_ic_path_keeps_cold_start_regression(tmp_path: Path) -> None:
    """Runs that do NOT declare packaged-IC bootstrap keep byte-identical behavior."""
    object_root = tmp_path / "object-store"
    _write_basins_package(object_root)
    _write_packaged_ic(object_root)
    checksums = _write_standard_shud_forcing(object_root)
    runtime = _runtime(tmp_path, FakeHydroRunRepository())
    manifest = _shud_project_manifest_with_forcing_checksums(checksums)
    manifest["initial_state"] = {"state_id": None, "ic_file_uri": None}
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    output_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "output"
    input_dir.mkdir(parents=True)
    output_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)
    cfg_path = runtime.generate_cfg_para(manifest, input_dir, output_dir)

    assert manifest["initial_state"]["quality"] == "cold_start_no_state"
    assert manifest["runtime"]["init_mode"] == 1
    assert _cfg_init_mode(cfg_path) == "1"


# ---------------------------------------------------------------------------
# #1330: attempt-start output-dir hygiene.
#
# `execute()` reuses the run's deterministic workspace on every retry, so a
# killed attempt's `output` tree used to still be there when the next attempt
# wrote its witness manifest — and the #1325 two-exact-paths writer would bless
# the stale `<project>.cfg.ic.update` with THIS attempt's provenance. The hook
# renames a pre-existing non-empty tree aside to `output_residue/previous`
# before anything else runs inside `execute()`'s `try`.
#
# The two new `safe_fs` primitives are unit-covered here rather than in a file
# of their own so they stay inside this change's evidence floor, next to their
# only consumer.
# ---------------------------------------------------------------------------


_STALE_ATTEMPT_FINAL_IC = "2 2 4320.000000\n1 0.91\n2 0.92\n1 0\n2 0\n"
_STALE_ATTEMPT_RIVQDOWN = "time,seg_0001,seg_0002\n2026-04-30T00:00:00+00:00,1.000,2.000\n"


_EXECUTE_NO_FINAL_IC_SOLVER_STUB = '''
import sys
from datetime import datetime, timedelta
from pathlib import Path

def _read_cfg(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values

def _parse(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

cfg = _read_cfg(sys.argv[1])
output_dir = Path(cfg["OUTPUT_DIR"])
output_dir.mkdir(parents=True, exist_ok=True)
start = _parse(cfg["START_TIME"])
end = _parse(cfg["END_TIME"])
interval = int(cfg.get("MODEL_OUTPUT_INTERVAL", "1440"))
segments = int(cfg.get("SEGMENT_COUNT", "1"))
steps = int((end - start).total_seconds() // (interval * 60))
rows = [",".join(["time"] + ["seg_%04d" % index for index in range(1, segments + 1)])]
for step in range(steps):
    stamp = (start + timedelta(minutes=step * interval)).isoformat()
    rows.append(",".join([stamp] + ["%.3f" % (100.0 + index + step) for index in range(1, segments + 1)]))
(output_dir / "demo.rivqdown").write_text("\\n".join(rows) + "\\n", encoding="utf-8")
# The zero-hour lane this change is about: a solve that writes the deliverable
# and NO final IC at either candidate path.
print("The successful end.")
'''


def _stale_attempt_manifest_payload(run_id: str) -> dict[str, Any]:
    """A previous attempt's witness manifest, blessing its own final IC."""

    return {
        "checkpoints": [],
        "observed_header_minutes": [4320.0],
        "provenance": {
            "run_id": run_id,
            "generated_at": "2026-04-30T00:00:00Z",
            "slurm_job_id": "111000",
            "array_task_id": 3,
            "requested_checkpoint_hours": [],
        },
        "recovery_outcomes": {},
        "final_ic": {
            "relative_path": "demo.cfg.ic.update",
            "original_shud_filename": "demo.cfg.ic.update",
            "checksum": sha256_bytes(_STALE_ATTEMPT_FINAL_IC.encode("utf-8")),
        },
    }


def _seed_previous_attempt_output(tmp_path: Path, run_id: str, *, symlink_target: Path | None = None) -> Path:
    """The killed-attempt geometry: a complete output tree of a PREVIOUS attempt."""

    output_dir = tmp_path / "workspace" / "runs" / run_id / "output"
    (output_dir / "state_checkpoints").mkdir(parents=True)
    (output_dir / "demo.cfg.ic.update").write_text(_STALE_ATTEMPT_FINAL_IC, encoding="utf-8")
    (output_dir / "demo.rivqdown").write_text(_STALE_ATTEMPT_RIVQDOWN, encoding="utf-8")
    # A name no attempt of this run ever writes again: the marker that tells a
    # quarantined tree from one merely overwritten in place.
    (output_dir / "stale-attempt-marker.txt").write_text("previous attempt\n", encoding="utf-8")
    (output_dir / "state_checkpoints" / "state_checkpoints.json").write_text(
        json.dumps(_stale_attempt_manifest_payload(run_id), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if symlink_target is not None:
        (output_dir / "stale-link").symlink_to(symlink_target)
    return output_dir


def _assert_attempt_failure_accounting(
    tmp_path: Path,
    repository: FakeHydroRunRepository,
    run_id: str,
    *,
    error_code: str,
) -> None:
    """The three-piece collateral every in-`try` attempt failure must produce.

    The task-outcome receipt is load-bearing: it is array accounting's only
    channel to the retry classifier, which otherwise back-fills a generic
    (transient) `NODE_FAILURE` for this attempt.
    """

    assert repository.statuses == ["created", "failed"]
    assert repository.failures[0][0] == error_code
    log_dir = tmp_path / "workspace" / "runs" / run_id / "logs"
    assert error_code in (log_dir / "runtime_error.log").read_text(encoding="utf-8")
    receipt = json.loads((log_dir / "task_outcome.json").read_text(encoding="utf-8"))
    assert receipt["run_id"] == run_id
    assert receipt["error_code"] == error_code


def test_rename_entry_no_follow_moves_a_symlink_without_following_it(tmp_path: Path) -> None:
    """#1330 D2.1: `renameat` acts on the final-component LINK, never its target."""

    root = tmp_path / "root"
    source_parent = root / "src"
    dest_parent = root / "dst"
    source_parent.mkdir(parents=True)
    dest_parent.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "victim.txt").write_text("external must remain\n", encoding="utf-8")
    (source_parent / "link").symlink_to(external, target_is_directory=True)

    moved = safe_fs.rename_entry_no_follow(source_parent, "link", dest_parent, "previous", containment_root=root)

    assert moved == dest_parent / "previous"
    assert not (source_parent / "link").exists()
    assert (dest_parent / "previous").is_symlink()
    assert os.readlink(dest_parent / "previous") == str(external)
    assert (external / "victim.txt").read_text(encoding="utf-8") == "external must remain\n"


def test_rename_entry_no_follow_reports_a_missing_source_entry(tmp_path: Path) -> None:
    """#1330 D2.1: a missing source is a typed refusal, not a silent no-op."""

    root = tmp_path / "root"
    source_parent = root / "src"
    dest_parent = root / "dst"
    source_parent.mkdir(parents=True)
    dest_parent.mkdir(parents=True)

    with pytest.raises(SafeFilesystemError) as exc_info:
        safe_fs.rename_entry_no_follow(source_parent, "absent", dest_parent, "previous", containment_root=root)

    assert not (dest_parent / "previous").exists()
    assert "Failed to rename" in str(exc_info.value)


def test_rename_entry_no_follow_refuses_to_escape_the_containment_root(tmp_path: Path) -> None:
    """#1330 D2.1: walk-stage refusals stay `unsafe` (permanent), never `io`."""

    root = tmp_path / "root"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "entry").write_text("payload\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(SafeFilesystemError) as exc_info:
        safe_fs.rename_entry_no_follow(root / "src", "entry", outside, "previous", containment_root=root)

    assert exc_info.value.kind == "unsafe"
    assert not (outside / "previous").exists()
    assert (root / "src" / "entry").read_text(encoding="utf-8") == "payload\n"


def test_rename_entry_no_follow_refuses_a_symlinked_parent_directory(tmp_path: Path) -> None:
    """#1330 D2.1: both parents are walked `O_NOFOLLOW`, so a symlinked one is refused."""

    root = tmp_path / "root"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "entry").write_text("payload\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (root / "dst").symlink_to(external, target_is_directory=True)

    with pytest.raises(SafeFilesystemError) as exc_info:
        safe_fs.rename_entry_no_follow(root / "src", "entry", root / "dst", "previous", containment_root=root)

    assert exc_info.value.kind == "unsafe"
    assert sorted(path.name for path in external.iterdir()) == []
    assert (root / "src" / "entry").read_text(encoding="utf-8") == "payload\n"


def test_rename_entry_no_follow_wraps_an_os_level_failure_as_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1330 D2.1 error contract: `kind` is the permanent/transient discriminator.

    The class default is `unsafe`, so a house-style wrap of the `renameat`
    `OSError` would silently make an ENOSPC/ESTALE quarantine rename PERMANENT
    at the hook's dispatch. Forced at the os level so the primitive's real wrap
    is what is observed.
    """

    import errno as errno_module

    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    (root / "dst").mkdir(parents=True)
    (root / "src" / "entry").write_text("payload\n", encoding="utf-8")

    def _no_space(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno_module.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "rename", _no_space)

    with pytest.raises(SafeFilesystemError) as exc_info:
        safe_fs.rename_entry_no_follow(root / "src", "entry", root / "dst", "previous", containment_root=root)

    assert exc_info.value.kind == "io"


def test_remove_tree_allow_symlinks_removes_links_and_subdirs_without_following(tmp_path: Path) -> None:
    """#1330 D2.2: the quarantine tree is removable even when it holds symlinks.

    `rmtree_no_follow` REFUSES symlink entries, which would permanently lock a
    run_id at the hygiene hook once a residue tree containing one is retained.
    """

    root = tmp_path / "root"
    residue = root / "output_residue"
    tree = residue / "previous"
    (tree / "state_checkpoints").mkdir(parents=True)
    (tree / "demo.cfg.ic.update").write_text("stale\n", encoding="utf-8")
    (tree / "state_checkpoints" / "state_checkpoints.json").write_text("{}\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (external / "victim.txt").write_text("external must remain\n", encoding="utf-8")
    (tree / "dir-link").symlink_to(external, target_is_directory=True)
    (tree / "state_checkpoints" / "file-link").symlink_to(external / "victim.txt")
    os.mkfifo(tree / "fifo")

    safe_fs.remove_tree_allow_symlinks(residue, "previous", containment_root=root)

    assert not tree.exists()
    assert residue.is_dir()
    # The links were unlinked as links: nothing behind them was touched.
    assert sorted(path.name for path in external.iterdir()) == ["victim.txt"]
    assert (external / "victim.txt").read_text(encoding="utf-8") == "external must remain\n"


def test_remove_tree_allow_symlinks_unlinks_a_symlinked_root_entry(tmp_path: Path) -> None:
    """#1330 D2.2: a root entry that is itself a link is unlinked, never traversed."""

    root = tmp_path / "root"
    residue = root / "output_residue"
    residue.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "victim.txt").write_text("external must remain\n", encoding="utf-8")
    (residue / "previous").symlink_to(external, target_is_directory=True)

    safe_fs.remove_tree_allow_symlinks(residue, "previous", containment_root=root)

    # `lexists`: the entry must be gone even as a DANGLING link (a `Path.exists`
    # would also pass if the link survived with a deleted target). Written this
    # way rather than `Path.exists(follow_symlinks=False)`, which is 3.12+ only.
    assert not os.path.lexists(residue / "previous")
    assert (external / "victim.txt").read_text(encoding="utf-8") == "external must remain\n"


def test_remove_tree_allow_symlinks_missing_entry_is_a_no_op(tmp_path: Path) -> None:
    """#1330 D2.2: the first retry has no residue to clear."""

    root = tmp_path / "root"
    residue = root / "output_residue"
    residue.mkdir(parents=True)

    safe_fs.remove_tree_allow_symlinks(residue, "previous", containment_root=root, missing_ok=True)
    safe_fs.remove_tree_allow_symlinks(residue, "previous", containment_root=root)

    assert sorted(path.name for path in residue.iterdir()) == []


def test_remove_tree_allow_symlinks_wraps_an_os_level_failure_as_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1330 D2.2 error contract: removal `OSError`s are `io`, not the class default."""

    import errno as errno_module

    root = tmp_path / "root"
    residue = root / "output_residue"
    (residue / "previous").mkdir(parents=True)
    (residue / "previous" / "stale.txt").write_text("stale\n", encoding="utf-8")

    def _io_error(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(errno_module.EIO, "Input/output error")

    monkeypatch.setattr(os, "unlink", _io_error)

    with pytest.raises(SafeFilesystemError) as exc_info:
        safe_fs.remove_tree_allow_symlinks(residue, "previous", containment_root=root)

    assert exc_info.value.kind == "io"


def test_remove_tree_allow_symlinks_refuses_to_escape_the_containment_root(tmp_path: Path) -> None:
    """#1330 D2.2: relaxing the symlink policy must not relax CONTAINMENT.

    The twin (`rename_entry_no_follow`) has this tooth; without it here the
    permissive primitive is the one place where an out-of-root argument could
    delete a tree wholesale, and the refusal must stay `unsafe` (permanent) so
    the hook never auto-retries it.
    """

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    (outside / "previous").mkdir(parents=True)
    (outside / "previous" / "payload.txt").write_text("outside must remain\n", encoding="utf-8")

    with pytest.raises(SafeFilesystemError) as exc_info:
        safe_fs.remove_tree_allow_symlinks(outside, "previous", containment_root=root)

    assert exc_info.value.kind == "unsafe"
    assert (outside / "previous" / "payload.txt").read_text(encoding="utf-8") == "outside must remain\n"


def test_remove_tree_allow_symlinks_refuses_a_symlinked_parent_directory(tmp_path: Path) -> None:
    """#1330 D2.2: entries may be links; the PARENT walk stays `O_NOFOLLOW`.

    Symlinks are tolerated inside the quarantine tree only. A symlinked
    `output_residue` is tampered geometry, and the refusal must be `unsafe` —
    `io` here would auto-retry a tamper forever at the hook.
    """

    root = tmp_path / "root"
    root.mkdir()
    external = tmp_path / "external"
    (external / "previous").mkdir(parents=True)
    (external / "previous" / "payload.txt").write_text("external must remain\n", encoding="utf-8")
    (root / "output_residue").symlink_to(external, target_is_directory=True)

    with pytest.raises(SafeFilesystemError) as exc_info:
        safe_fs.remove_tree_allow_symlinks(root / "output_residue", "previous", containment_root=root)

    assert exc_info.value.kind == "unsafe"
    assert (external / "previous" / "payload.txt").read_text(encoding="utf-8") == "external must remain\n"


@pytest.mark.parametrize("unsafe_name", ["..", "nested/previous"], ids=["parent-traversal", "path-separator"])
def test_new_no_follow_primitives_reject_unsafe_entry_names(tmp_path: Path, unsafe_name: str) -> None:
    """#1330 D2.1/D2.2: an entry NAME is one component, never a path.

    Both primitives take names that are resolved against a pinned dir-fd, so a
    `..` or a separator would silently re-target the operation outside the
    directory the caller pinned — `..` would rename or DELETE the parent itself.
    `_reject_unsafe_entry_name` must fire on the source name, the destination
    name and the removal name alike, and before `missing_ok` can swallow it.
    """

    root = tmp_path / "root"
    source_parent = root / "src"
    dest_parent = root / "dst"
    (source_parent / "nested").mkdir(parents=True)
    (dest_parent / "nested").mkdir(parents=True)
    (source_parent / "previous").write_text("payload\n", encoding="utf-8")
    (source_parent / "nested" / "previous").write_text("nested payload\n", encoding="utf-8")

    with pytest.raises(SafeFilesystemError) as source_refusal:
        safe_fs.rename_entry_no_follow(source_parent, unsafe_name, dest_parent, "previous", containment_root=root)
    with pytest.raises(SafeFilesystemError) as dest_refusal:
        safe_fs.rename_entry_no_follow(source_parent, "previous", dest_parent, unsafe_name, containment_root=root)
    with pytest.raises(SafeFilesystemError) as removal_refusal:
        safe_fs.remove_tree_allow_symlinks(source_parent, unsafe_name, containment_root=root, missing_ok=True)

    assert source_refusal.value.kind == "unsafe"
    assert dest_refusal.value.kind == "unsafe"
    assert removal_refusal.value.kind == "unsafe"
    # Nothing was renamed, nothing was removed, in either direction.
    assert (source_parent / "previous").read_text(encoding="utf-8") == "payload\n"
    assert (source_parent / "nested" / "previous").read_text(encoding="utf-8") == "nested payload\n"
    assert sorted(path.name for path in dest_parent.iterdir()) == ["nested"]
    assert source_parent.is_dir()


@pytest.mark.parametrize(
    ("kind", "expected_code"),
    [
        ("io", "STORAGE_WRITE_FAILED"),
        ("unsafe", "WORKSPACE_PATH_UNSAFE"),
        ("indeterminate", "WORKSPACE_PATH_UNSAFE"),
        ("weird-future-kind", "WORKSPACE_PATH_UNSAFE"),
        (None, "STORAGE_WRITE_FAILED"),
    ],
    ids=["io", "unsafe", "indeterminate", "kind-added-later", "bare-oserror"],
)
def test_output_hygiene_error_dispatch_is_total(tmp_path: Path, kind: str | None, expected_code: str) -> None:
    """#1330 D2 error contract: the hook's dispatch is TOTAL over failure shapes.

    The `execute()`-level anchors reach `io`, `unsafe` and the bare-`OSError`
    lanes, but `indeterminate` and any kind a later change adds to `safe_fs` are
    unreachable from there — a `kind == "unsafe"` dispatch would pass every one
    of those anchors while silently routing an UNPROVEN geometry to the
    auto-retry lane. Driven directly so the fail-closed default is pinned.
    """

    import errno as errno_module

    from services.orchestrator.retry import is_retryable_failure

    output_dir = tmp_path / "workspace" / "runs" / "run-x" / "output"
    error: OSError | SafeFilesystemError = (
        OSError(errno_module.ESTALE, "Stale file handle")
        if kind is None
        else SafeFilesystemError("refused", kind=kind)
    )

    result = runtime_module._output_hygiene_error(output_dir, error)

    assert isinstance(result, SHUDRuntimeError)
    assert result.error_code == expected_code
    assert str(output_dir) in result.message
    assert str(error) in result.message
    # The code IS the retry verdict: transient only for the I/O lane.
    assert is_retryable_failure(result.error_code) is (expected_code == "STORAGE_WRITE_FAILED")


def test_execute_does_not_bless_a_previous_attempts_final_ic_residue(tmp_path: Path) -> None:
    """#1330 A1: the harm — a stale final IC published as this attempt's state.

    Zero requested checkpoint hours plus a solve that writes no final IC is the
    exact production drift (`chain_manifests` sets `state_checkpoint_hours` and
    `Update_IC_STEP` together). On master the previous attempt's
    `demo.cfg.ic.update` survives in the reused tree, and the two-exact-paths
    writer names it in `final_ic` with THIS attempt's provenance and checksum:
    wrong state, self-consistent evidence chain.
    """

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    stub = tmp_path / "no_final_ic_solver.py"
    stub.write_text(_EXECUTE_NO_FINAL_IC_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    manifest = _manifest()
    assert "state_checkpoint_hours" not in manifest["runtime"]
    output_dir = _seed_previous_attempt_output(tmp_path, manifest["run_id"])

    result = runtime.execute(manifest)

    assert result.status == "succeeded"
    payload = json.loads((output_dir / "state_checkpoints" / "state_checkpoints.json").read_text(encoding="utf-8"))
    assert "final_ic" not in payload
    assert not (output_dir / "demo.cfg.ic.update").exists()
    assert not (output_dir / "demo.cfg.ic").exists()
    assert not (output_dir / "stale-attempt-marker.txt").exists()
    # The tree the solve ran into carries only this attempt's bytes.
    assert (output_dir / "demo.rivqdown").read_text(encoding="utf-8") != _STALE_ATTEMPT_RIVQDOWN


def test_execute_quarantines_the_previous_attempt_tree_intact(tmp_path: Path) -> None:
    """#1330 A2: the stale tree is locatable and complete, not silently deleted.

    Rename-aside moves the tree as an opaque unit, so a subdirectory and a
    symlink survive the move un-followed — the operator debugging the current
    retry gets the whole thing.
    """

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    stub = tmp_path / "execute_fast_solver.py"
    stub.write_text(_EXECUTE_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    manifest = _manifest()
    run_id = manifest["run_id"]
    external = tmp_path / "external"
    external.mkdir()
    (external / "victim.txt").write_text("external must remain\n", encoding="utf-8")
    output_dir = _seed_previous_attempt_output(tmp_path, run_id, symlink_target=external / "victim.txt")
    stale_manifest_bytes = (output_dir / "state_checkpoints" / "state_checkpoints.json").read_bytes()

    result = runtime.execute(manifest)

    assert result.status == "succeeded"
    residue = tmp_path / "workspace" / "runs" / run_id / "output_residue" / "previous"
    assert (residue / "demo.cfg.ic.update").read_text(encoding="utf-8") == _STALE_ATTEMPT_FINAL_IC
    assert (residue / "demo.rivqdown").read_text(encoding="utf-8") == _STALE_ATTEMPT_RIVQDOWN
    assert (residue / "state_checkpoints" / "state_checkpoints.json").read_bytes() == stale_manifest_bytes
    assert (residue / "stale-attempt-marker.txt").read_text(encoding="utf-8") == "previous attempt\n"
    # The symlink moved as a LINK; its target was never read or rewritten.
    assert (residue / "stale-link").is_symlink()
    assert os.readlink(residue / "stale-link") == str(external / "victim.txt")
    assert (external / "victim.txt").read_text(encoding="utf-8") == "external must remain\n"
    # ... and this attempt's own final IC is its own.
    assert (output_dir / "demo.cfg.ic.update").read_text(encoding="utf-8") != _STALE_ATTEMPT_FINAL_IC


def test_execute_fresh_first_attempt_creates_no_output_residue(tmp_path: Path) -> None:
    """#1330 A3(b): an absent `output` is simply created — no quarantine sibling."""

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()

    result = runtime.execute(manifest)

    workspace = tmp_path / "workspace" / "runs" / manifest["run_id"]
    assert result.status == "succeeded"
    assert (workspace / "output" / "demo.rivqdown").is_file()
    assert not (workspace / "output_residue").exists()


def test_execute_reuses_an_empty_output_dir_without_evicting_retained_residue(tmp_path: Path) -> None:
    """#1330 A3(c): an empty `output` must not evict the only real residue.

    An early-failing retry (a staging failure, say) leaves `output` empty behind
    it. Quarantining unconditionally would replace the retained residue tree
    with an empty husk — a retention regression against master, and exactly the
    evidence an operator came for.
    """

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    workspace = tmp_path / "workspace" / "runs" / manifest["run_id"]
    output_dir = workspace / "output"
    output_dir.mkdir(parents=True)
    empty_output_inode = output_dir.stat().st_ino
    residue = workspace / "output_residue" / "previous"
    residue.mkdir(parents=True)
    (residue / "demo.cfg.ic.update").write_text(_STALE_ATTEMPT_FINAL_IC, encoding="utf-8")

    result = runtime.execute(manifest)

    assert result.status == "succeeded"
    # The empty directory itself was reused, not renamed away and recreated.
    assert output_dir.stat().st_ino == empty_output_inode
    assert sorted(path.name for path in (workspace / "output_residue").iterdir()) == ["previous"]
    assert (residue / "demo.cfg.ic.update").read_text(encoding="utf-8") == _STALE_ATTEMPT_FINAL_IC


def test_execute_refuses_a_symlinked_output_without_following_or_quarantining(tmp_path: Path) -> None:
    """#1330 A4: a non-directory `output` is tampered geometry — fail before mutating.

    The typed error itself is green on master too (`ensure_directory_no_follow`
    already refuses the `O_NOFOLLOW` open), but master dies at the pre-`try`
    directory loop: no failure log, no task-outcome receipt, no `mark_failed`.
    The guard also runs BEFORE any quarantine mutation, so no `output_residue`
    may appear.
    """

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    run_id = manifest["run_id"]
    workspace = tmp_path / "workspace" / "runs" / run_id
    external = tmp_path / "external-output"
    external.mkdir()
    (external / "victim.txt").write_text("external must remain\n", encoding="utf-8")
    workspace.mkdir(parents=True)
    (workspace / "output").symlink_to(external, target_is_directory=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    assert exc_info.value.error_code == "WORKSPACE_PATH_UNSAFE"
    assert (workspace / "output").is_symlink()
    assert sorted(path.name for path in external.iterdir()) == ["victim.txt"]
    assert (external / "victim.txt").read_text(encoding="utf-8") == "external must remain\n"
    assert not (workspace / "output_residue").exists()
    _assert_attempt_failure_accounting(tmp_path, repository, run_id, error_code="WORKSPACE_PATH_UNSAFE")


def test_execute_refuses_a_plain_file_at_the_output_path(tmp_path: Path) -> None:
    """#1330 A4 sibling: a REGULAR FILE at `output` is tampered geometry too.

    The symlink anchor is refused inside `stat_no_follow` (its own symlink
    check), so the hook's `S_ISDIR` guard is never the deciding branch there.
    A plain file passes the probe and is judged only by that guard: without it
    the run would rename a file to `output_residue/previous` and then solve into
    a freshly created `output`, laundering tampered geometry into a green run.
    """

    from services.orchestrator.retry import is_retryable_failure

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    run_id = manifest["run_id"]
    workspace = tmp_path / "workspace" / "runs" / run_id
    workspace.mkdir(parents=True)
    (workspace / "output").write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    assert exc_info.value.error_code == "WORKSPACE_PATH_UNSAFE"
    assert is_retryable_failure("WORKSPACE_PATH_UNSAFE") is False
    # Refused BEFORE any mutation: the bytes are untouched and nothing moved.
    assert (workspace / "output").is_file()
    assert (workspace / "output").read_text(encoding="utf-8") == "not a directory\n"
    assert not (workspace / "output_residue").exists()
    _assert_attempt_failure_accounting(tmp_path, repository, run_id, error_code="WORKSPACE_PATH_UNSAFE")


def test_execute_retains_only_the_latest_residue_tree_across_two_retries(tmp_path: Path) -> None:
    """#1330 A5: retention is exactly one tree, even when the evicted one holds a link.

    The evicted residue is what `rmtree_no_follow` would refuse outright, so this
    is also the pin against the P1-1 lockout: a run whose first residue contains
    a symlink must stay retryable forever.
    """

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    stub = tmp_path / "execute_fast_solver.py"
    stub.write_text(_EXECUTE_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    manifest = _manifest()
    run_id = manifest["run_id"]
    external = tmp_path / "external"
    external.mkdir()
    (external / "victim.txt").write_text("external must remain\n", encoding="utf-8")
    _seed_previous_attempt_output(tmp_path, run_id, symlink_target=external / "victim.txt")

    assert runtime.execute(manifest).status == "succeeded"
    residue_root = tmp_path / "workspace" / "runs" / run_id / "output_residue"
    assert (residue_root / "previous" / "stale-link").is_symlink()

    # Second retry: the first attempt's own (non-empty) output is quarantined and
    # the symlink-bearing residue must be removed rather than refused.
    assert runtime.execute(deepcopy(manifest)).status == "succeeded"

    assert sorted(path.name for path in residue_root.iterdir()) == ["previous"]
    # `lexists`, not `Path.exists(follow_symlinks=False)` (3.12+): the link must
    # be gone as a LINK, dangling or not.
    assert not os.path.lexists(residue_root / "previous" / "stale-link")
    retained_ic = (residue_root / "previous" / "demo.cfg.ic.update").read_text(encoding="utf-8")
    assert retained_ic != _STALE_ATTEMPT_FINAL_IC
    assert not (residue_root / "previous" / "stale-attempt-marker.txt").exists()
    assert (residue_root / "previous" / "state_checkpoints" / "state_checkpoints.json").is_file()
    assert (external / "victim.txt").read_text(encoding="utf-8") == "external must remain\n"


def test_quarantining_attempt_uploads_exactly_the_fresh_output_tree(tmp_path: Path) -> None:
    """#1330 A6(a): the quarantine sibling lives outside `output`, so upload ignores it."""

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    stub = tmp_path / "execute_fast_solver.py"
    stub.write_text(_EXECUTE_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    manifest = _manifest()
    run_id = manifest["run_id"]
    output_dir = _seed_previous_attempt_output(tmp_path, run_id)

    assert runtime.execute(manifest).status == "succeeded"

    uploaded_root = object_root / "runs" / run_id / "output"
    uploaded = sorted(
        path.relative_to(uploaded_root).as_posix() for path in uploaded_root.rglob("*") if path.is_file()
    )
    produced = sorted(path.relative_to(output_dir).as_posix() for path in output_dir.rglob("*") if path.is_file())
    assert uploaded == produced
    # ... and that tree is exactly what THIS attempt wrote: the previous
    # attempt's marker file is at the quarantine sibling, so it can neither be
    # walked nor uploaded.
    assert uploaded == ["demo.cfg.ic.update", "demo.rivqdown", "state_checkpoints/state_checkpoints.json"]
    assert (
        tmp_path / "workspace" / "runs" / run_id / "output_residue" / "previous" / "stale-attempt-marker.txt"
    ).is_file()
    # The run's object-store prefixes are exactly the staged manifest, the logs
    # and the fresh output tree: quarantined residue is never uploaded.
    assert sorted(path.name for path in (object_root / "runs" / run_id).iterdir()) == ["input", "logs", "output"]


def test_quarantining_attempt_publishes_only_the_fresh_attempts_state(tmp_path: Path) -> None:
    """#1330 A6(b): the publish gate's root set is unchanged and sees fresh bytes.

    `save_state_for_run` probes the literal `runs/<run_id>/output`; after the
    hook that tree holds only this attempt's artifacts, so the state it mints is
    the one this attempt actually computed.
    """

    from packages.common.state_cli import save_state_for_run

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    stub = tmp_path / "execute_fast_solver.py"
    stub.write_text(_EXECUTE_FAST_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    manifest = _manifest()
    run_id = manifest["run_id"]
    output_dir = _seed_previous_attempt_output(tmp_path, run_id)

    assert runtime.execute(manifest).status == "succeeded"

    manager = _RoundTripStateManager(LocalObjectStore(object_root, "s3://nhms"))
    save_state_for_run(
        run_id,
        manager=manager,
        run_context=_round_trip_run_context(),
        workspace_root=tmp_path / "workspace",
    )

    from datetime import UTC, datetime

    assert len(manager.saved) == 1
    assert manager.saved[0]["valid_time"] == datetime(2026, 5, 4, tzinfo=UTC)
    assert manager.saved[0]["original_shud_filename"] == "demo.cfg.ic.update"
    # The published body is this attempt's solve (0.1/0.2), never the residue's
    # (0.91/0.92); the gate publishes a header-normalized copy, so the body
    # values are what discriminate the two.
    published = Path(manager.saved[0]["ic_file_path"]).read_text(encoding="utf-8")
    assert "1 0.1\n2 0.2\n" in published
    assert "0.91" not in published
    assert (output_dir / "demo.cfg.ic.update").read_text(encoding="utf-8").startswith("2 2 4320.000000")


def test_execute_hygiene_rename_failure_is_transient_with_full_accounting(tmp_path: Path) -> None:
    """#1330 A7(a)(i): an ENOSPC on the quarantine rename keeps automatic retry.

    Injected at the os level so the primitive's own `kind="io"` wrap is on the
    assertion path: a wrong kind inside `rename_entry_no_follow` would otherwise
    stay invisible and make an NFS glitch permanent.
    """

    import errno as errno_module

    from services.orchestrator.retry import is_retryable_failure

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    run_id = manifest["run_id"]
    _seed_previous_attempt_output(tmp_path, run_id)
    real_rename = os.rename
    fired = False

    def _fail_the_quarantine_rename(source: Any, dest: Any, **kwargs: Any) -> None:
        nonlocal fired
        if dest == "previous":
            fired = True
            raise OSError(errno_module.ENOSPC, "No space left on device")
        return real_rename(source, dest, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "rename", _fail_the_quarantine_rename)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.execute(manifest)

    assert fired
    assert exc_info.value.error_code == "STORAGE_WRITE_FAILED"
    assert is_retryable_failure("STORAGE_WRITE_FAILED") is True
    # Terminated before the solve was ever spawned.
    assert not (tmp_path / "workspace" / "runs" / run_id / "logs" / "shud_stdout.log").exists()
    _assert_attempt_failure_accounting(tmp_path, repository, run_id, error_code="STORAGE_WRITE_FAILED")


def test_execute_hygiene_recreate_failure_is_transient_after_quarantine(tmp_path: Path) -> None:
    """#1330 A7(a)(ii): the recreate lane must not be pre-typed permanent.

    `_ensure_directory` types EVERY `SafeFilesystemError` as
    `WORKSPACE_PATH_UNSAFE`, including `kind="io"` mkdir failures, so the hook
    calls the raw primitive. The `os.mkdir` patch is discriminated to the
    `output` target — an undiscriminated one fires at the `output_residue` mkdir
    two steps earlier and leaves this lane silently untested.
    """

    import errno as errno_module

    from services.orchestrator.retry import is_retryable_failure

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    run_id = manifest["run_id"]
    _seed_previous_attempt_output(tmp_path, run_id)
    real_mkdir = os.mkdir
    fired = False

    def _fail_only_the_output_mkdir(path: Any, *args: Any, **kwargs: Any) -> None:
        nonlocal fired
        if path == "output":
            fired = True
            raise OSError(errno_module.ENOSPC, "No space left on device")
        return real_mkdir(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "mkdir", _fail_only_the_output_mkdir)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.execute(manifest)

    assert fired
    assert exc_info.value.error_code == "STORAGE_WRITE_FAILED"
    assert is_retryable_failure("STORAGE_WRITE_FAILED") is True
    # Steps 2-4 completed: the residue is already quarantined when recreate dies.
    residue = tmp_path / "workspace" / "runs" / run_id / "output_residue" / "previous"
    assert (residue / "demo.cfg.ic.update").read_text(encoding="utf-8") == _STALE_ATTEMPT_FINAL_IC
    assert not (tmp_path / "workspace" / "runs" / run_id / "output").exists()
    _assert_attempt_failure_accounting(tmp_path, repository, run_id, error_code="STORAGE_WRITE_FAILED")


def test_execute_hygiene_probe_failure_is_transient_with_full_accounting(tmp_path: Path) -> None:
    """#1330 A7(a)(iii): an ESTALE while LISTING `output` is as transient as one while renaming.

    This is the lane a step-index split loses: the probe/emptiness listing sits
    before any mutation, so a shape-blind implementation lets it escape to a
    permanent (or generic) code.
    """

    import errno as errno_module

    from services.orchestrator.retry import is_retryable_failure

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    run_id = manifest["run_id"]
    _seed_previous_attempt_output(tmp_path, run_id)
    real_scandir = os.scandir
    fired = False

    def _fail_the_dir_fd_listing(*args: Any, **kwargs: Any) -> Any:
        nonlocal fired
        if args and isinstance(args[0], int):
            fired = True
            raise OSError(errno_module.ESTALE, "Stale file handle")
        return real_scandir(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "scandir", _fail_the_dir_fd_listing)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.execute(manifest)

    assert fired
    assert exc_info.value.error_code == "STORAGE_WRITE_FAILED"
    assert is_retryable_failure("STORAGE_WRITE_FAILED") is True
    # Nothing was mutated: the failure precedes the quarantine entirely.
    assert not (tmp_path / "workspace" / "runs" / run_id / "output_residue").exists()
    assert (tmp_path / "workspace" / "runs" / run_id / "output" / "demo.cfg.ic.update").read_text(
        encoding="utf-8"
    ) == _STALE_ATTEMPT_FINAL_IC
    _assert_attempt_failure_accounting(tmp_path, repository, run_id, error_code="STORAGE_WRITE_FAILED")


def test_execute_hygiene_post_probe_disappearance_is_transient_with_full_accounting(tmp_path: Path) -> None:
    """#1330 A7(a)(iii) sibling: the BARE-`OSError` lane is reachable, and transient.

    `safe_fs` re-raises `FileNotFoundError` BARE (no `kind`), so a concurrent
    cleaner unlinking `output` between the hook's probe and its emptiness
    listing lands at the dispatch as a plain `OSError`. That is a race, not
    tampering: it must stay in the auto-retry lane. The hook's own
    `except FileNotFoundError` covers the PROBE only, so this injection is aimed
    at the listing — a dispatch that made bare `OSError`s permanent would strip
    a self-healing race of its retry, and every `SafeFilesystemError` anchor
    would stay green.
    """

    import errno as errno_module

    from services.orchestrator.retry import is_retryable_failure

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    run_id = manifest["run_id"]
    _seed_previous_attempt_output(tmp_path, run_id)
    real_scandir = os.scandir
    fired = False

    def _lose_the_dir_fd_listing_once(*args: Any, **kwargs: Any) -> Any:
        # Only the dir-fd listing: the probe is an `os.stat`, so by the time this
        # fires the probe has already succeeded and its `FileNotFoundError`
        # handler is out of scope. One-shot, so the failure-accounting path that
        # follows is not itself disturbed.
        nonlocal fired
        if args and isinstance(args[0], int) and not fired:
            fired = True
            raise FileNotFoundError(errno_module.ENOENT, "simulated post-probe unlink")
        return real_scandir(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "scandir", _lose_the_dir_fd_listing_once)
        with pytest.raises(SHUDRuntimeError) as exc_info:
            runtime.execute(manifest)

    assert fired
    assert exc_info.value.error_code == "STORAGE_WRITE_FAILED"
    assert is_retryable_failure("STORAGE_WRITE_FAILED") is True
    # The dispatch really saw a BARE `OSError`: had `safe_fs` wrapped it, the
    # message would carry the primitive's own text and this would be the
    # already-anchored `kind="io"` lane instead.
    assert "simulated post-probe unlink" in exc_info.value.message
    assert "Failed to list directory" not in exc_info.value.message
    # Nothing was mutated: the listing precedes the quarantine entirely.
    assert not (tmp_path / "workspace" / "runs" / run_id / "output_residue").exists()
    assert (tmp_path / "workspace" / "runs" / run_id / "output" / "demo.cfg.ic.update").read_text(
        encoding="utf-8"
    ) == _STALE_ATTEMPT_FINAL_IC
    _assert_attempt_failure_accounting(tmp_path, repository, run_id, error_code="STORAGE_WRITE_FAILED")


def test_execute_hygiene_tamper_failure_is_permanent_with_full_accounting(tmp_path: Path) -> None:
    """#1330 A7(b): tampered geometry is permanent — retry can never fix it.

    Master's no-receipt death outside the `try` let the array reader back-fill a
    transient `NODE_FAILURE`, so this class was auto-retried forever. The honest
    verdict is terminal, and it now arrives with full accounting.
    """

    from services.orchestrator.retry import is_retryable_failure

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    run_id = manifest["run_id"]
    workspace = tmp_path / "workspace" / "runs" / run_id
    workspace.mkdir(parents=True)
    (workspace / "output").symlink_to(tmp_path / "object-store", target_is_directory=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    assert exc_info.value.error_code == "WORKSPACE_PATH_UNSAFE"
    assert is_retryable_failure("WORKSPACE_PATH_UNSAFE") is False
    _assert_attempt_failure_accounting(tmp_path, repository, run_id, error_code="WORKSPACE_PATH_UNSAFE")


def test_execute_symlinked_residue_sibling_lands_in_the_permanent_lane(tmp_path: Path) -> None:
    """#1330 A7(b) sibling tooth: an `unsafe` refusal from a LATER step is permanent too.

    The quarantine sibling is a geometry this change newly creates. A
    steps-2-5-are-transient implementation would blanket-type this as
    `STORAGE_WRITE_FAILED` and auto-retry a tamper forever.
    """

    from services.orchestrator.retry import is_retryable_failure

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository)
    manifest = _manifest()
    run_id = manifest["run_id"]
    _seed_previous_attempt_output(tmp_path, run_id)
    external = tmp_path / "external-residue"
    external.mkdir()
    (tmp_path / "workspace" / "runs" / run_id / "output_residue").symlink_to(external, target_is_directory=True)

    with pytest.raises(SHUDRuntimeError) as exc_info:
        runtime.execute(manifest)

    assert exc_info.value.error_code == "WORKSPACE_PATH_UNSAFE"
    assert is_retryable_failure("WORKSPACE_PATH_UNSAFE") is False
    # The link was not followed and the residue tree was never moved into it.
    assert sorted(path.name for path in external.iterdir()) == []
    assert (tmp_path / "workspace" / "runs" / run_id / "output" / "demo.cfg.ic.update").read_text(
        encoding="utf-8"
    ) == _STALE_ATTEMPT_FINAL_IC
    _assert_attempt_failure_accounting(tmp_path, repository, run_id, error_code="WORKSPACE_PATH_UNSAFE")


def test_quarantined_residue_final_ic_is_unpublishable_by_the_state_gate(tmp_path: Path) -> None:
    """#1330 A6(c): the A1 harm as the PUBLISH side sees it — nothing is admissible.

    A1 pins the witness manifest (no `final_ic` key); A6(b) pins the happy lane
    (fresh bytes published). Neither composes the two halves in the zero-hour
    geometry that actually bit: on master the residue's `demo.cfg.ic.update`
    survives in the reused tree, the writer names it, and the #1325 gate — which
    verifies only that the manifest is self-consistent — happily publishes a
    PREVIOUS attempt's state as this run's successor.

    With the hook, the same run leaves the gate with no admissible state at all:
    `STATE_SAVE_SOURCE_FINAL_IC_MISSING`, zero snapshots, zero QC. Refusing to
    publish is the correct answer for a solve that computed no final state — and
    it is the only assertion that can distinguish "not published" from "not
    written", because the stale bytes still exist, quarantined one level up.
    """

    from packages.common.state_cli import save_state_for_run
    from packages.common.state_manager import StateManagerError

    object_root = tmp_path / "object-store"
    _write_package(object_root)
    _write_forcing(object_root)
    stub = tmp_path / "no_final_ic_solver.py"
    stub.write_text(_EXECUTE_NO_FINAL_IC_SOLVER_STUB, encoding="utf-8")
    repository = FakeHydroRunRepository()
    runtime = _runtime(tmp_path, repository, shud_executable=stub)
    manifest = _manifest()
    run_id = manifest["run_id"]
    assert "state_checkpoint_hours" not in manifest["runtime"]
    _seed_previous_attempt_output(tmp_path, run_id)

    assert runtime.execute(manifest).status == "succeeded"

    manager = _RoundTripStateManager(LocalObjectStore(object_root, "s3://nhms"))
    with pytest.raises(StateManagerError) as exc_info:
        save_state_for_run(
            run_id,
            manager=manager,
            run_context=_round_trip_run_context(),
            workspace_root=tmp_path / "workspace",
        )

    assert str(exc_info.value).startswith("STATE_SAVE_SOURCE_FINAL_IC_MISSING")
    assert manager.saved == []
    assert manager.qc_runs == []
    # The stale IC was never deleted — it is simply outside every publishable
    # root, so "nothing to publish" is a containment result, not data loss.
    residue = tmp_path / "workspace" / "runs" / run_id / "output_residue" / "previous"
    assert (residue / "demo.cfg.ic.update").read_text(encoding="utf-8") == _STALE_ATTEMPT_FINAL_IC
    assert not (tmp_path / "workspace" / "runs" / run_id / "output" / "demo.cfg.ic.update").exists()
