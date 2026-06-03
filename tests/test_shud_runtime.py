from __future__ import annotations

import io
import subprocess
import sys
import tarfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore, ObjectStoreError, sha256_bytes
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


def _write_forcing(object_root: Path) -> None:
    forcing = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    forcing.mkdir(parents=True)
    (forcing / "forcing.tsd.forc").write_text("forcing\n", encoding="utf-8")


def _write_standard_shud_forcing(
    object_root: Path, *, units: dict[str, str] | None = None
) -> dict[str, str]:
    forcing = object_root / "forcing" / "gfs" / "2026050100" / "basin_v01" / "demo_model"
    shud_dir = forcing / "shud"
    shud_dir.mkdir(parents=True)
    tsd_content = "1 20260501\n/data\nID\tLon\tLat\tX\tY\tZ\tFilename\n1\t100\t30\t1\t1\t1\tforcing.csv\n"
    csv_content = "2\t6\t20260501\t20260501\nTime_Day\tPrecip\tTemp\tRH\tWind\tRN\n0\t1\t2\t3\t4\t5\n"
    (shud_dir / "qhh.tsd.forc").write_text(tsd_content, encoding="utf-8")
    (shud_dir / "forcing.csv").write_text(csv_content, encoding="utf-8")
    manifest_payload: dict[str, Any] = {
        "station_count": 1,
        "files": [
            {
                "role": "shud_forcing",
                "relative_path": "shud/qhh.tsd.forc",
                "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/qhh.tsd.forc",
                "checksum": sha256_bytes(tsd_content.encode("utf-8")),
            },
            {
                "role": "shud_forcing_csv",
                "relative_path": "shud/forcing.csv",
                "uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/shud/forcing.csv",
                "checksum": sha256_bytes(csv_content.encode("utf-8")),
            },
        ],
    }
    if units is not None:
        manifest_payload["units"] = units
    manifest_content = json_bytes(manifest_payload)
    (forcing / "forcing_package.json").write_bytes(manifest_content)
    return {
        "manifest_uri": "s3://nhms/forcing/gfs/2026050100/basin_v01/demo_model/forcing_package.json",
        "manifest_checksum": sha256_bytes(manifest_content),
        "tsd_checksum": sha256_bytes(tsd_content.encode("utf-8")),
        "csv_checksum": sha256_bytes(csv_content.encode("utf-8")),
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


def test_runtime_staging_tolerates_unreadable_package_manifest(tmp_path: Path) -> None:
    """#270 (best-effort): an unreadable / over-size-cap package manifest must NOT
    hard-fail the run. The unit peek is optional; a read failure (e.g. a multi-station
    manifest exceeding the read cap, or a transient object-store error) tolerate-skips.
    Content integrity is already guaranteed by the checksum verified before this call.
    """
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
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    assert (input_dir / "alias-a" / "alias-a.tsd.forc").exists()


def test_runtime_staging_tolerates_invalid_json_package_manifest(tmp_path: Path) -> None:
    """#270 (best-effort): a package manifest that is not valid JSON must NOT hard-fail.

    The checksum still matches the (malformed) bytes, so checksum verification passes;
    the unit peek then fails to parse and tolerate-skips instead of breaking the run.
    """
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
    input_dir = tmp_path / "workspace" / "runs" / manifest["run_id"] / "input"
    input_dir.mkdir(parents=True)

    runtime.prepare_workspace(manifest, input_dir)

    assert (input_dir / "alias-a" / "alias-a.tsd.forc").exists()


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

    with pytest.raises(SHUDRuntimeError) as exc_info:
        SHUDRuntime.from_env()

    assert exc_info.value.error_code == "DATABASE_URL_MISSING"


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
    cfg_path.write_text("START_TIME = 2026-05-01T00:00:00Z\n", encoding="utf-8")

    def raise_timeout(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.TimeoutExpired(["shud"], 1, output=b"stdout bytes", stderr=b"stderr bytes")

    monkeypatch.setattr("workers.shud_runtime.runtime.subprocess.run", raise_timeout)

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
    cfg_path.write_text("START_TIME = 2026-05-01T00:00:00Z\n", encoding="utf-8")
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
    calls: list[list[str]] = []

    def _ok(command: list[str], *_a: Any, **_k: Any) -> SimpleNamespace:
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr("workers.shud_runtime.runtime.subprocess.run", _ok)

    runtime.run_shud(_manifest(), cfg_path, workspace, output_dir, log_dir)

    assert calls, "valid python runtime script must reach subprocess"


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
