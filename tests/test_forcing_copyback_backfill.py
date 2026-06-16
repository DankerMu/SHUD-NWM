from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from packages.common.object_store import LocalObjectStore, ObjectStoreError
from services.tile_publisher import forcing_copyback_backfill as backfill_module
from services.tile_publisher.forcing_copyback_backfill import BackfillConfig, run_backfill

REPO_ROOT = Path(__file__).resolve().parents[1]
CYCLE_TIME = datetime(2024, 6, 1, 12, tzinfo=UTC)
CYCLE_TIME_2 = datetime(2024, 6, 2, 12, tzinfo=UTC)
FORCING_KEY = "forcing/gfs/2024060112/basin-1/model-1"
FORCING_KEY_2 = "forcing/gfs/2024060212/basin-1/model-1"


def _init_db(tmp_path: Path) -> tuple[Engine, Path]:
    db_path = tmp_path / "backfill.sqlite"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE hydro_run (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    model_id TEXT,
                    basin_version_id TEXT,
                    forcing_version_id TEXT,
                    source_id TEXT,
                    cycle_time DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE river_timeseries (
                    run_id TEXT NOT NULL,
                    river_segment_id TEXT NOT NULL,
                    valid_time DATETIME NOT NULL,
                    variable TEXT NOT NULL,
                    value REAL
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE forcing_version (
                    forcing_version_id TEXT PRIMARY KEY,
                    forcing_package_uri TEXT,
                    checksum TEXT,
                    lineage_json TEXT
                )
                """
            )
        )
    return engine, db_path


def _insert_run(
    engine: Engine,
    *,
    run_id: str,
    status: str = "parsed",
    forcing_version_id: str = "forcing-1",
    variable: str = "q_down",
    value: float | None = 1.0,
    source_id: str = "gfs",
    cycle_time: datetime = CYCLE_TIME,
    basin_version_id: str = "basin-1",
    model_id: str = "model-1",
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO hydro_run (
                    run_id, status, model_id, basin_version_id,
                    forcing_version_id, source_id, cycle_time
                ) VALUES (
                    :run_id, :status, :model_id, :basin_version_id,
                    :forcing_version_id, :source_id, :cycle_time
                )
                """
            ),
            {
                "run_id": run_id,
                "status": status,
                "model_id": model_id,
                "basin_version_id": basin_version_id,
                "forcing_version_id": forcing_version_id,
                "source_id": source_id,
                "cycle_time": cycle_time,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO river_timeseries (
                    run_id, river_segment_id, valid_time, variable, value
                ) VALUES (
                    :run_id, 'seg-1', :valid_time, :variable, :value
                )
                """
            ),
            {
                "run_id": run_id,
                "valid_time": cycle_time,
                "variable": variable,
                "value": value,
            },
        )


def _insert_forcing_version(
    engine: Engine,
    *,
    forcing_version_id: str = "forcing-1",
    package_uri: str = f"{FORCING_KEY}/",
    checksum: str,
    lineage_json: Any | None = None,
) -> None:
    lineage = (
        lineage_json
        if lineage_json is not None
        else {
            "forcing_package_manifest_uri": f"{package_uri.rstrip('/')}/forcing_package.json",
            "forcing_package_manifest_checksum": checksum,
            "output_files": [{"uri": f"{package_uri.rstrip('/')}/forcing.tsd.forc"}],
        }
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO forcing_version (
                    forcing_version_id, forcing_package_uri, checksum, lineage_json
                ) VALUES (
                    :forcing_version_id, :forcing_package_uri, :checksum, :lineage_json
                )
                """
            ),
            {
                "forcing_version_id": forcing_version_id,
                "forcing_package_uri": package_uri,
                "checksum": checksum,
                "lineage_json": lineage if isinstance(lineage, str) else json.dumps(lineage),
            },
        )


def _write_forcing_package(
    root: Path,
    *,
    key: str = FORCING_KEY,
    forcing_version_id: str = "forcing-1",
    output_bytes: bytes = b"forcing-bytes\n",
    manifest_payload: dict[str, Any] | None = None,
) -> tuple[str, bytes]:
    package_root = root / key
    package_root.mkdir(parents=True, exist_ok=True)
    output_key = f"{key}/forcing.tsd.forc"
    payload = manifest_payload or {
        "forcing_version_id": forcing_version_id,
        "files": [{"role": "tsd_forc", "uri": output_key}],
    }
    manifest_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    (package_root / "forcing.tsd.forc").write_bytes(output_bytes)
    (package_root / "forcing_package.json").write_bytes(manifest_bytes)
    return sha256(manifest_bytes).hexdigest(), manifest_bytes


def _base_config(
    *,
    db_path: Path,
    object_store_root: Path,
    copyback_root: Path,
    apply: bool = False,
) -> BackfillConfig:
    return BackfillConfig(
        database_url=f"sqlite:///{db_path}",
        object_store_root=object_store_root,
        copyback_root=copyback_root,
        apply=apply,
    )


def _seed_valid_candidate(
    tmp_path: Path,
    *,
    key: str = FORCING_KEY,
    run_id: str = "run-a",
    forcing_version_id: str = "forcing-1",
    cycle_time: datetime = CYCLE_TIME,
) -> tuple[Engine, Path, Path, Path, str, bytes]:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    checksum, manifest_bytes = _write_forcing_package(
        object_store_root,
        key=key,
        forcing_version_id=forcing_version_id,
    )
    _insert_run(
        engine,
        run_id=run_id,
        forcing_version_id=forcing_version_id,
        cycle_time=cycle_time,
    )
    _insert_forcing_version(
        engine,
        forcing_version_id=forcing_version_id,
        package_uri=f"{key}/",
        checksum=checksum,
    )
    return engine, db_path, object_store_root, copyback_root, checksum, manifest_bytes


def _run_module(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "services.tile_publisher.forcing_copyback_backfill", *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _env(db_path: Path, object_store_root: Path, copyback_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_path}",
            "OBJECT_STORE_ROOT": str(object_store_root),
            "NHMS_OBJECT_STORE_COPYBACK_ROOT": str(copyback_root),
        }
    )
    return env


def test_db_discovery_filters_eligible_qdown_runs_and_counts_joined_forcing_versions(
    tmp_path: Path,
) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    checksum, _manifest_bytes = _write_forcing_package(object_store_root)
    _insert_forcing_version(engine, forcing_version_id="forcing-1", checksum=checksum)
    _insert_forcing_version(engine, forcing_version_id="forcing-2", checksum=checksum)
    _insert_forcing_version(engine, forcing_version_id="forcing-excluded", checksum=checksum)
    _insert_forcing_version(engine, forcing_version_id="forcing-non-qdown", checksum=checksum)

    _insert_run(engine, run_id="run-parsed", status="parsed", forcing_version_id="forcing-1")
    _insert_run(engine, run_id="run-frequency", status="frequency_done", forcing_version_id="forcing-2")
    _insert_run(engine, run_id="run-published", status="published", forcing_version_id="forcing-1")
    _insert_run(engine, run_id="run-missing-met", status="parsed", forcing_version_id="forcing-missing")
    _insert_run(engine, run_id="run-running", status="running", forcing_version_id="forcing-excluded")
    _insert_run(engine, run_id="run-stage", status="parsed", forcing_version_id="forcing-non-qdown", variable="stage")

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root)
    )

    assert report["total_run_count"] == 4
    assert report["forcing_version_count"] == 2
    assert report["copyable_package_count"] == 1
    assert report["failure_count"] == 1
    assert report["failures"][0]["run_id"] == "run-missing-met"
    assert len(report["packages"]) == 1
    assert report["packages"][0]["run_ids"] == ["run-frequency", "run-parsed", "run-published"]
    assert report["packages"][0]["forcing_version_ids"] == ["forcing-1", "forcing-2"]


def test_discovery_sql_drives_from_hydro_run_with_correlated_qdown_exists() -> None:
    sql = backfill_module._DISCOVER_BACKFILL_RUNS_SQL

    assert "FROM hydro.hydro_run h" in sql
    assert "EXISTS (" in sql
    assert "rt.run_id = h.run_id" in sql
    assert "rt.variable = 'q_down'" in sql
    assert "SELECT DISTINCT" not in sql
    assert "JOIN (\n" not in sql


def test_cli_dry_run_emits_json_and_writes_nothing(tmp_path: Path) -> None:
    _engine, db_path, object_store_root, copyback_root, _checksum, _manifest_bytes = _seed_valid_candidate(tmp_path)

    result = _run_module(_env(db_path, object_store_root, copyback_root))

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mode"] == "dry_run"
    assert report["copyable_package_count"] == 1
    assert report["copied_count"] == 0
    assert not copyback_root.exists()


def test_cli_apply_copies_valid_missing_package(tmp_path: Path) -> None:
    _engine, db_path, object_store_root, copyback_root, _checksum, manifest_bytes = _seed_valid_candidate(tmp_path)

    result = _run_module(_env(db_path, object_store_root, copyback_root), "--apply")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["mode"] == "apply"
    assert report["copyable_package_count"] == 1
    assert report["copied_count"] == 1
    assert (copyback_root / f"{FORCING_KEY}/forcing_package.json").read_bytes() == manifest_bytes
    assert (copyback_root / f"{FORCING_KEY}/forcing.tsd.forc").read_bytes() == b"forcing-bytes\n"


@pytest.mark.parametrize(
    "missing_env",
    ["DATABASE_URL", "OBJECT_STORE_ROOT", "NHMS_OBJECT_STORE_COPYBACK_ROOT"],
)
def test_cli_missing_env_fails_stably_without_target_writes(tmp_path: Path, missing_env: str) -> None:
    _engine, db_path, object_store_root, copyback_root, _checksum, _manifest_bytes = _seed_valid_candidate(tmp_path)
    env = _env(db_path, object_store_root, copyback_root)
    env.pop(missing_env)

    result = _run_module(env)

    assert result.returncode == 2
    payload = json.loads(result.stderr)
    assert payload["error_code"] == "CONFIG_MISSING"
    assert payload["details"]["missing"] == [missing_env]
    assert "Traceback" not in result.stderr
    assert not copyback_root.exists()


@pytest.mark.parametrize("args", [(), ("--apply",)])
def test_cli_rejects_copyback_root_equal_object_store_root_without_already_present(
    tmp_path: Path,
    args: tuple[str, ...],
) -> None:
    _engine, db_path, object_store_root, _copyback_root, _checksum, _manifest_bytes = _seed_valid_candidate(tmp_path)
    before = {
        path.relative_to(object_store_root).as_posix(): path.read_bytes()
        for path in object_store_root.rglob("*")
        if path.is_file()
    }

    result = _run_module(_env(db_path, object_store_root, object_store_root), *args)

    assert result.returncode == 1
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error_code"] == "COPYBACK_ROOT_SAME_AS_OBJECT_STORE_ROOT"
    assert payload["details"]["reason"] == "copyback_root_matches_object_store_root"
    assert "already_present" not in result.stderr
    assert "Traceback" not in result.stderr
    after = {
        path.relative_to(object_store_root).as_posix(): path.read_bytes()
        for path in object_store_root.rglob("*")
        if path.is_file()
    }
    assert after == before


@pytest.mark.parametrize("args", [(), ("--apply",)])
def test_cli_rejects_copyback_root_equal_object_store_root_with_zero_eligible_runs(
    tmp_path: Path,
    args: tuple[str, ...],
) -> None:
    _engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    object_store_root.mkdir()

    result = _run_module(_env(db_path, object_store_root, object_store_root), *args)

    assert result.returncode == 1
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error_code"] == "COPYBACK_ROOT_SAME_AS_OBJECT_STORE_ROOT"
    assert payload["details"]["reason"] == "copyback_root_matches_object_store_root"
    assert "already_present" not in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_apply_prepare_publish_error_emits_json_without_traceback_or_writes(tmp_path: Path) -> None:
    _engine, db_path, object_store_root, copyback_root, _checksum, _manifest_bytes = _seed_valid_candidate(tmp_path)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    link_parent = tmp_path / "link-parent"
    link_parent.symlink_to(real_parent, target_is_directory=True)
    unsafe_copyback_root = link_parent / "shared-object-store"

    result = _run_module(_env(db_path, object_store_root, unsafe_copyback_root), "--apply")

    assert result.returncode == 1
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error_code"] == "OBJECT_STORE_COPYBACK_FAILED"
    assert payload["message"] == "Failed to prepare object-store copyback root."
    assert "Traceback" not in result.stderr
    assert not (real_parent / "shared-object-store").exists()
    assert not copyback_root.exists()


def test_already_present_checksum_consistent_target_is_not_copied(tmp_path: Path) -> None:
    _engine, db_path, object_store_root, copyback_root, _checksum, manifest_bytes = _seed_valid_candidate(tmp_path)
    _write_forcing_package(copyback_root)

    report = run_backfill(
        _base_config(
            db_path=db_path,
            object_store_root=object_store_root,
            copyback_root=copyback_root,
            apply=True,
        )
    )

    assert report["already_present_checksum_consistent_count"] == 1
    assert report["copyable_package_count"] == 0
    assert report["copied_count"] == 0
    assert report["packages"][0]["status"] == "already_present"
    assert (copyback_root / f"{FORCING_KEY}/forcing_package.json").read_bytes() == manifest_bytes


def test_dry_run_already_present_checksum_consistent_target_is_not_mutated(tmp_path: Path) -> None:
    _engine, db_path, object_store_root, copyback_root, _checksum, _manifest_bytes = _seed_valid_candidate(tmp_path)
    _write_forcing_package(copyback_root)
    before = {
        path.relative_to(copyback_root).as_posix(): path.read_bytes()
        for path in copyback_root.rglob("*")
        if path.is_file()
    }

    report = run_backfill(
        _base_config(
            db_path=db_path,
            object_store_root=object_store_root,
            copyback_root=copyback_root,
            apply=False,
        )
    )

    after = {
        path.relative_to(copyback_root).as_posix(): path.read_bytes()
        for path in copyback_root.rglob("*")
        if path.is_file()
    }
    assert report["already_present_checksum_consistent_count"] == 1
    assert report["copyable_package_count"] == 0
    assert report["copied_count"] == 0
    assert report["packages"][0]["status"] == "already_present"
    assert after == before


def test_missing_source_reports_failure_with_required_identity_fields(tmp_path: Path) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    _insert_run(engine, run_id="run-a")
    _insert_forcing_version(engine, checksum="0" * 64)

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root)
    )

    assert report["missing_source_count"] == 1
    assert report["failure_count"] == 1
    assert report["copied_count"] == 0
    failure = report["failures"][0]
    assert failure["run_id"] == "run-a"
    assert failure["forcing_version_id"] == "forcing-1"
    assert failure["forcing_package_uri"] == f"{FORCING_KEY}/"
    assert failure["reason"]
    assert not copyback_root.exists()


def test_source_manifest_checksum_mismatch_is_not_copied(tmp_path: Path) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    _write_forcing_package(object_store_root)
    _insert_run(engine, run_id="run-a")
    _insert_forcing_version(engine, checksum="0" * 64)

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root)
    )

    assert report["checksum_mismatch_count"] == 1
    assert report["failure_count"] == 1
    assert report["copied_count"] == 0
    assert "checksum" in report["failures"][0]["reason"].lower()


def test_legacy_forcing_version_key_is_rejected_for_manual_handling(tmp_path: Path) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    _insert_run(engine, run_id="run-a")
    _insert_forcing_version(engine, package_uri="forcing/forcing-1/", checksum="0" * 64)

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root)
    )

    assert report["legacy_key_rejected_count"] == 1
    assert report["failure_count"] == 1
    assert report["failures"][0]["forcing_package_uri"] == "forcing/forcing-1/"
    assert report["copied_count"] == 0


def test_absolute_forcing_package_uri_is_rejected_without_target_write(tmp_path: Path) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    _insert_run(engine, run_id="run-a")
    _insert_forcing_version(engine, package_uri="/forcing/gfs/2024060112/basin-1/model-1/", checksum="0" * 64)

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root)
    )

    assert report["failure_count"] == 1
    assert report["legacy_key_rejected_count"] == 0
    assert "absolute" in report["failures"][0]["reason"]
    assert not copyback_root.exists()


def test_traversal_wrong_prefix_and_empty_segment_uris_are_rejected_without_target_write(
    tmp_path: Path,
) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    cases = [
        ("run-traversal", "forcing-traversal", "forcing/gfs/2024060112/basin-1/../model-1/"),
        ("run-prefix", "forcing-prefix", "runs/gfs/2024060112/basin-1/model-1/"),
        ("run-empty", "forcing-empty", "forcing/gfs//basin-1/model-1/"),
        ("run-segment-count", "forcing-segment-count", "forcing/gfs/2024060112/basin-1/"),
    ]
    for run_id, forcing_version_id, package_uri in cases:
        _insert_run(engine, run_id=run_id, forcing_version_id=forcing_version_id)
        _insert_forcing_version(
            engine,
            forcing_version_id=forcing_version_id,
            package_uri=package_uri,
            checksum="0" * 64,
        )

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root)
    )

    assert report["failure_count"] == 4
    assert report["copied_count"] == 0
    reasons = {failure["run_id"]: failure["reason"] for failure in report["failures"]}
    assert "must not contain '..'" in reasons["run-traversal"]
    assert "start with forcing/" in reasons["run-prefix"]
    assert "empty segments" in reasons["run-empty"]
    assert "forcing/<source>/<cycle>/<basin>/<model>" in reasons["run-segment-count"]
    assert not copyback_root.exists()


def test_forcing_uri_source_identity_mismatch_fails_without_copy(tmp_path: Path) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    checksum, _manifest_bytes = _write_forcing_package(object_store_root, key=FORCING_KEY)
    _insert_run(engine, run_id="run-a", source_id="ifs")
    _insert_forcing_version(engine, checksum=checksum)

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root)
    )

    assert report["failure_count"] == 1
    assert report["copied_count"] == 0
    assert "mismatched fields" in report["failures"][0]["reason"]
    assert not copyback_root.exists()


def test_forcing_uri_cycle_basin_model_identity_mismatches_fail_without_copy(tmp_path: Path) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    cases = [
        ("run-cycle", "forcing-cycle", "forcing/gfs/2024060212/basin-1/model-1/"),
        ("run-basin", "forcing-basin", "forcing/gfs/2024060112/basin-other/model-1/"),
        ("run-model", "forcing-model", "forcing/gfs/2024060112/basin-1/model-other/"),
    ]
    for run_id, forcing_version_id, package_uri in cases:
        _insert_run(engine, run_id=run_id, forcing_version_id=forcing_version_id)
        _insert_forcing_version(
            engine,
            forcing_version_id=forcing_version_id,
            package_uri=package_uri,
            checksum="0" * 64,
        )

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root)
    )

    assert report["failure_count"] == 3
    assert report["copied_count"] == 0
    reasons = {failure["run_id"]: failure["reason"] for failure in report["failures"]}
    assert "cycle_time" in reasons["run-cycle"]
    assert "basin_version_id" in reasons["run-basin"]
    assert "model_id" in reasons["run-model"]
    assert not copyback_root.exists()


def test_lineage_manifest_checksum_mismatch_fails_before_copy(tmp_path: Path) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    checksum, _manifest_bytes = _write_forcing_package(object_store_root)
    _insert_run(engine, run_id="run-a")
    _insert_forcing_version(
        engine,
        checksum=checksum,
        lineage_json={
            "forcing_package_manifest_uri": f"{FORCING_KEY}/forcing_package.json",
            "forcing_package_manifest_checksum": "bad-checksum",
            "output_files": [{"uri": f"{FORCING_KEY}/forcing.tsd.forc"}],
        },
    )

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root)
    )

    assert report["checksum_mismatch_count"] == 1
    assert report["failure_count"] == 1
    assert "lineage manifest checksum" in report["failures"][0]["reason"]
    assert not copyback_root.exists()


def test_symlink_source_package_is_rejected_without_copy(tmp_path: Path) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    real_package_root = tmp_path / "real-package"
    real_package_root.mkdir()
    (object_store_root / "forcing/gfs/2024060112/basin-1").mkdir(parents=True)
    (object_store_root / FORCING_KEY).symlink_to(real_package_root, target_is_directory=True)
    _insert_run(engine, run_id="run-a")
    _insert_forcing_version(engine, checksum="0" * 64)

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root)
    )

    assert report["failure_count"] == 1
    assert report["copied_count"] == 0
    assert "symlink" in report["failures"][0]["reason"]
    assert not copyback_root.exists()


def test_regular_file_source_package_is_rejected_without_copy(tmp_path: Path) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    (object_store_root / "forcing/gfs/2024060112/basin-1").mkdir(parents=True)
    (object_store_root / FORCING_KEY).write_text("not a directory\n", encoding="utf-8")
    _insert_run(engine, run_id="run-a")
    _insert_forcing_version(engine, checksum="0" * 64)

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root)
    )

    assert report["failure_count"] == 1
    assert report["copied_count"] == 0
    assert "not a directory" in report["failures"][0]["reason"]
    assert not copyback_root.exists()


def test_missing_source_manifest_reports_missing_source_without_copy(tmp_path: Path) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    package_root = object_store_root / FORCING_KEY
    package_root.mkdir(parents=True)
    (package_root / "forcing.tsd.forc").write_bytes(b"forcing-bytes\n")
    _insert_run(engine, run_id="run-a")
    _insert_forcing_version(engine, checksum="0" * 64)

    report = run_backfill(
        _base_config(db_path=db_path, object_store_root=object_store_root, copyback_root=copyback_root)
    )

    assert report["missing_source_count"] == 1
    assert report["copied_count"] == 0
    assert "manifest is missing" in report["failures"][0]["reason"]
    assert not copyback_root.exists()


def test_apply_with_existing_target_regular_file_reports_failure_without_partial_package(
    tmp_path: Path,
) -> None:
    _engine, db_path, object_store_root, copyback_root, _checksum, _manifest_bytes = _seed_valid_candidate(tmp_path)
    target_parent = copyback_root / "forcing/gfs/2024060112/basin-1"
    target_parent.mkdir(parents=True)
    (target_parent / "model-1").write_text("unsafe target\n", encoding="utf-8")

    report = run_backfill(
        _base_config(
            db_path=db_path,
            object_store_root=object_store_root,
            copyback_root=copyback_root,
            apply=True,
        )
    )

    assert report["failure_count"] == 1
    assert report["copied_count"] == 0
    assert "directory" in report["failures"][0]["reason"]
    assert (target_parent / "model-1").read_text(encoding="utf-8") == "unsafe target\n"
    assert not list(copyback_root.rglob("*.copyback.*"))


def test_apply_with_existing_target_directory_missing_manifest_fails_without_overwrite(
    tmp_path: Path,
) -> None:
    _engine, db_path, object_store_root, copyback_root, _checksum, _manifest_bytes = _seed_valid_candidate(tmp_path)
    target_root = copyback_root / FORCING_KEY
    target_root.mkdir(parents=True)
    (target_root / "forcing.tsd.forc").write_bytes(b"stale-target-output\n")
    (target_root / "operator-note.txt").write_text("preserve me\n", encoding="utf-8")

    report = run_backfill(
        _base_config(
            db_path=db_path,
            object_store_root=object_store_root,
            copyback_root=copyback_root,
            apply=True,
        )
    )

    assert report["failure_count"] == 1
    assert report["copied_count"] == 0
    assert report["copyable_package_count"] == 0
    assert report["packages"][0]["status"] == "failed"
    assert "manifest is missing" in report["failures"][0]["reason"]
    assert not (target_root / "forcing_package.json").exists()
    assert (target_root / "forcing.tsd.forc").read_bytes() == b"stale-target-output\n"
    assert (target_root / "operator-note.txt").read_text(encoding="utf-8") == "preserve me\n"
    assert not list(copyback_root.rglob("*.copyback.*"))


def test_apply_with_existing_target_symlink_reports_failure_without_partial_package(
    tmp_path: Path,
) -> None:
    _engine, db_path, object_store_root, copyback_root, _checksum, _manifest_bytes = _seed_valid_candidate(tmp_path)
    target_parent = copyback_root / "forcing/gfs/2024060112/basin-1"
    target_parent.mkdir(parents=True)
    target_parent.joinpath("model-1").symlink_to(tmp_path, target_is_directory=True)

    report = run_backfill(
        _base_config(
            db_path=db_path,
            object_store_root=object_store_root,
            copyback_root=copyback_root,
            apply=True,
        )
    )

    assert report["failure_count"] == 1
    assert report["copied_count"] == 0
    assert "symlink" in report["failures"][0]["reason"]
    assert (target_parent / "model-1").is_symlink()
    assert not list(copyback_root.rglob("*.copyback.*"))


def test_apply_write_failure_leaves_no_partial_package_and_preserves_prior_valid_target(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    checksum_1, manifest_1 = _write_forcing_package(object_store_root, key=FORCING_KEY)
    checksum_2, _manifest_2 = _write_forcing_package(
        object_store_root,
        key=FORCING_KEY_2,
        forcing_version_id="forcing-2",
    )
    _write_forcing_package(copyback_root, key=FORCING_KEY)
    _insert_run(engine, run_id="run-a", forcing_version_id="forcing-1", cycle_time=CYCLE_TIME)
    _insert_run(engine, run_id="run-b", forcing_version_id="forcing-2", cycle_time=CYCLE_TIME_2)
    _insert_forcing_version(
        engine,
        forcing_version_id="forcing-1",
        package_uri=f"{FORCING_KEY}/",
        checksum=checksum_1,
    )
    _insert_forcing_version(
        engine,
        forcing_version_id="forcing-2",
        package_uri=f"{FORCING_KEY_2}/",
        checksum=checksum_2,
    )

    original_write = LocalObjectStore.write_bytes_atomic

    def fail_second_package_write(self: LocalObjectStore, key_or_uri: str, content: bytes) -> str:
        if Path(self.root).resolve() == copyback_root.resolve() and str(key_or_uri).startswith(
            "forcing/gfs/2024060212/"
        ):
            raise ObjectStoreError(f"blocked target write for {key_or_uri}")
        return original_write(self, key_or_uri, content)

    monkeypatch.setattr(LocalObjectStore, "write_bytes_atomic", fail_second_package_write)

    report = run_backfill(
        _base_config(
            db_path=db_path,
            object_store_root=object_store_root,
            copyback_root=copyback_root,
            apply=True,
        )
    )

    assert report["already_present_checksum_consistent_count"] == 1
    assert report["failure_count"] == 1
    assert report["copied_count"] == 0
    assert "blocked target write" in report["failures"][0]["reason"]
    assert (copyback_root / f"{FORCING_KEY}/forcing_package.json").read_bytes() == manifest_1
    assert not (copyback_root / FORCING_KEY_2).exists()
    assert not list(copyback_root.rglob("*.copyback.*"))


def test_cli_apply_redacts_credential_forcing_uri_while_copying_by_object_key(tmp_path: Path) -> None:
    engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store"
    copyback_root = tmp_path / "shared-object-store"
    object_store_root.mkdir()
    checksum, manifest_bytes = _write_forcing_package(object_store_root)
    credential_uri = (
        f"s3://user:pass@bucket/{FORCING_KEY}/"
        "?token=secret&X-Amz-Signature=presigned-secret"
    )
    _insert_run(engine, run_id="run-a")
    _insert_forcing_version(
        engine,
        package_uri=credential_uri,
        checksum=checksum,
        lineage_json={"forcing_package_manifest_checksum": checksum, "output_files": []},
    )

    result = _run_module(_env(db_path, object_store_root, copyback_root), "--apply")

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["packages"][0]["object_key"] == FORCING_KEY
    assert report["packages"][0]["references"][0]["forcing_package_uri"] == f"s3://bucket/{FORCING_KEY}/"
    assert report["copied_count"] == 1
    assert (copyback_root / f"{FORCING_KEY}/forcing_package.json").read_bytes() == manifest_bytes
    for raw_secret in ("user:pass", "token=secret", "X-Amz-Signature", "presigned-secret"):
        assert raw_secret not in result.stdout


def test_failure_reason_redacts_secret_shaped_exception_text(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    _engine, db_path, object_store_root, copyback_root, _checksum, _manifest_bytes = _seed_valid_candidate(tmp_path)
    original_write = LocalObjectStore.write_bytes_atomic

    def fail_target_write(self: LocalObjectStore, key_or_uri: str, content: bytes) -> str:
        if Path(self.root).resolve() == copyback_root.resolve():
            raise ObjectStoreError(
                "copy failed token=secret password=hunter2 "
                "uri=s3://user:pass@bucket/forcing/path?X-Amz-Signature=presigned-secret"
            )
        return original_write(self, key_or_uri, content)

    monkeypatch.setattr(LocalObjectStore, "write_bytes_atomic", fail_target_write)

    report = run_backfill(
        _base_config(
            db_path=db_path,
            object_store_root=object_store_root,
            copyback_root=copyback_root,
            apply=True,
        )
    )

    body = json.dumps(report, sort_keys=True)
    assert report["failure_count"] == 1
    assert report["copied_count"] == 0
    assert "[redacted]" in report["failures"][0]["reason"]
    for raw_secret in ("token=secret", "password=hunter2", "user:pass", "X-Amz-Signature", "presigned-secret"):
        assert raw_secret not in body
    assert not list(copyback_root.rglob("*.copyback.*"))


def test_cli_error_stderr_redacts_secret_shaped_details(tmp_path: Path) -> None:
    _engine, db_path = _init_db(tmp_path)
    object_store_root = tmp_path / "object-store-token=secret"
    copyback_root = tmp_path / "shared-object-store"

    result = _run_module(_env(db_path, object_store_root, copyback_root))

    assert result.returncode == 1
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["error_code"] == "OBJECT_STORE_ROOT_UNSAFE"
    assert "token=secret" not in result.stderr
    assert "[redacted]" in result.stderr
    assert "Traceback" not in result.stderr
    assert not copyback_root.exists()
