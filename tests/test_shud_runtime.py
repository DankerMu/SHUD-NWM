from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes
from workers.shud_runtime.runtime import (
    DbFreeHydroRunRepository,
    SHUDRuntime,
    SHUDRuntimeConfig,
    SHUDRuntimeError,
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


def _runtime(tmp_path: Path, repository: FakeHydroRunRepository, shud_executable: Path | None = None) -> SHUDRuntime:
    config = SHUDRuntimeConfig(
        workspace_root=tmp_path / "workspace",
        object_store_root=tmp_path / "object-store",
        object_store_prefix="s3://nhms",
        shud_executable=str(shud_executable or Path("tests/mock_shud_omp.py").resolve()),
        output_interval_minutes=1440,
        timeout_seconds=30,
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


def test_state_checkpoint_tracker_captures_t6_t12_from_long_run_update(tmp_path: Path) -> None:
    manifest = _manifest()
    manifest["end_time"] = "2026-05-08T00:00:00Z"
    manifest["forecast_horizon_hours"] = 168
    manifest["runtime"]["state_checkpoint_hours"] = [6, 12]
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    update_file = output_dir / "demo.cfg.ic.update"
    tracker = _StateCheckpointTracker(manifest, output_dir)

    update_file.write_text("2 1 29626920.000000\n1 0.1\n2 0.2\n1 0\n", encoding="utf-8")
    tracker.capture_available()
    update_file.write_text("2 1 29627280.000000\n1 0.3\n2 0.4\n1 0\n", encoding="utf-8")
    tracker.capture_available()
    tracker.write_manifest()

    checkpoint_dir = output_dir / "state_checkpoints"
    f006 = checkpoint_dir / "demo.f006.cfg.ic.update"
    f012 = checkpoint_dir / "demo.f012.cfg.ic.update"
    payload = json.loads((checkpoint_dir / "state_checkpoints.json").read_text(encoding="utf-8"))

    assert f006.read_text(encoding="utf-8").startswith("2 1 29626920.000000")
    assert f012.read_text(encoding="utf-8").startswith("2 1 29627280.000000")
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

    update_file.write_text("2 1 360.000000\n1 0.1\n2 0.2\n1 0\n", encoding="utf-8")
    tracker.capture_available()
    update_file.write_text("2 1 720.000000\n1 0.3\n2 0.4\n1 0\n", encoding="utf-8")
    tracker.capture_available()
    tracker.write_manifest()

    checkpoint_dir = output_dir / "state_checkpoints"
    assert (checkpoint_dir / "demo.f006.cfg.ic.update").read_text(encoding="utf-8").startswith("2 1 360.000000")
    assert (checkpoint_dir / "demo.f012.cfg.ic.update").read_text(encoding="utf-8").startswith("2 1 720.000000")


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
