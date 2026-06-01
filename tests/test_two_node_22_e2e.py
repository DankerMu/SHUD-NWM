from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LATEST_RUN_ID = Path("/ghdc/data/nwm/evidence/latest-22-e2e-run-id")
DEFAULT_EVIDENCE_PARENT = Path("/ghdc/data/nwm/evidence")
DEFAULT_RUN_BASE = Path("/ghdc/data/nwm/workspace/22-e2e")
DEFAULT_COMPUTE_CONTAINER = "nhms-compute-compute-api-1"


@pytest.fixture(scope="module")
def live_22_context() -> dict[str, Any]:
    if os.getenv("NHMS_RUN_22_NODE_E2E") != "1":
        pytest.skip("set NHMS_RUN_22_NODE_E2E=1 to run 22-node live Docker/Slurm evidence checks")

    evidence_root_env = os.getenv("NHMS_22_E2E_EVIDENCE_ROOT", "").strip()
    if evidence_root_env:
        evidence_root = Path(evidence_root_env).expanduser()
        run_id = os.getenv("NHMS_22_E2E_RUN_ID", evidence_root.name)
    else:
        assert DEFAULT_LATEST_RUN_ID.is_file(), f"missing latest run id pointer: {DEFAULT_LATEST_RUN_ID}"
        run_id = DEFAULT_LATEST_RUN_ID.read_text(encoding="utf-8").strip()
        assert run_id, f"empty latest run id pointer: {DEFAULT_LATEST_RUN_ID}"
        evidence_root = DEFAULT_EVIDENCE_PARENT / run_id

    run_base = Path(os.getenv("NHMS_22_E2E_RUN_BASE", str(DEFAULT_RUN_BASE))).expanduser()
    container = os.getenv("NHMS_22_E2E_API_CONTAINER", DEFAULT_COMPUTE_CONTAINER)
    assert evidence_root.is_dir(), f"missing 22-node evidence root: {evidence_root}"
    assert run_base.is_dir(), f"missing 22-node run base: {run_base}"
    return {"run_id": run_id, "evidence_root": evidence_root, "run_base": run_base, "container": container}


def test_22_compute_container_health_and_storage_permissions(live_22_context: dict[str, Any]) -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker CLI is not available")

    container = live_22_context["container"]
    probe = json.loads(
        _run(
            ["docker", "exec", "-i", container, "python", "-"],
            input_text="""
import json
import urllib.request

payload = {}
for path in ("/health", "/api/v1/runtime/config", "/api/v1/slurm/health"):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=8) as response:
        body = response.read().decode()
        try:
            body = json.loads(body)
        except json.JSONDecodeError:
            pass
        payload[path] = {"status": response.status, "body": body}
print(json.dumps(payload, sort_keys=True))
""",
        )
    )

    assert probe["/health"]["status"] == 200
    assert probe["/health"]["body"]["status"] == "ok"
    runtime = probe["/api/v1/runtime/config"]["body"]["data"]
    assert runtime["service_role"] == "compute_control"
    assert runtime["control_mutations_enabled"] is True
    assert runtime["display_readonly"] is False

    slurm_health = probe["/api/v1/slurm/health"]["body"]
    assert slurm_health["backend"] == "slurm"
    if slurm_health["status"] != "healthy":
        assert "sinfo was not found" in slurm_health.get("error", "")

    paths = _run(
        [
            "docker",
            "exec",
            container,
            "sh",
            "-lc",
            (
                "id; "
                'test -w "$WORKSPACE_ROOT"; echo workspace=$?; '
                'test -w "$OBJECT_STORE_ROOT"; echo object_store=$?; '
                'test -r "$NHMS_BASINS_ROOT/qhh"; echo basins_qhh=$?; '
                'test "$UV_CACHE_DIR" = /tmp/nhms-uv-cache; echo uv_cache=$?'
            ),
        ]
    )
    assert "1107" in paths
    assert "workspace=0" in paths
    assert "object_store=0" in paths
    assert "basins_qhh=0" in paths
    assert "uv_cache=0" in paths


def test_22_database_and_shud_dry_run_evidence(live_22_context: dict[str, Any]) -> None:
    evidence_root = live_22_context["evidence_root"]
    run_base = live_22_context["run_base"]

    migrations = _read_text(evidence_root / "db" / "migrate.log")
    assert "Migrations complete: 26 applied, 0 skipped, 26 total." in migrations
    extensions = set(_read_text(evidence_root / "db" / "extensions.txt").splitlines())
    assert {"pgcrypto", "postgis", "timescaledb", "timescaledb_toolkit"} <= extensions

    shud = _read_json(evidence_root / "shud" / "shud_dry_run_stdout.json")
    assert shud["status"] == "succeeded"
    rivqdown = Path(shud["rivqdown_file"])
    assert rivqdown.is_file()
    assert run_base in rivqdown.parents
    object_files = _read_text(evidence_root / "shud" / "shud_dry_run_object_files.txt")
    assert "/object-store/runs/" in object_files
    assert "/output/demo.rivqdown" in object_files
    assert "/logs/shud_stdout.log" in object_files


def test_22_slurm_shud_dry_run_and_filesystem_boundary_evidence(live_22_context: dict[str, Any]) -> None:
    evidence_root = live_22_context["evidence_root"]

    preflight = _read_text(evidence_root / "slurm" / "slurm_preflight.log")
    for command in ("sinfo", "squeue", "sbatch", "sacct", "scancel"):
        assert f"/{command}" in preflight
    assert "CPU" in preflight

    slurm_diag = _read_text(evidence_root / "slurm" / "slurm_diag_sacct.log")
    assert "COMPLETED" in slurm_diag
    slurm_shud = _read_text(evidence_root / "slurm" / "slurm_shud_dry_run_sacct.log")
    assert "nhms22shud" in slurm_shud
    assert "COMPLETED" in slurm_shud

    boundary = _read_text(evidence_root / "slurm" / "compute-node-filesystem-boundary.txt")
    assert "cn09" in boundary
    assert "PATH /ghdc: missing" in boundary
    assert "Publish/copyback is required" in boundary


def test_22_gfs_download_and_canonical_database_evidence(live_22_context: dict[str, Any]) -> None:
    evidence_root = live_22_context["evidence_root"]
    run_base = live_22_context["run_base"]

    download = _read_json(evidence_root / "data" / "gfs_download_2026060100_f003_after_split_root_fix.stdout.json")
    assert download["status"] == "raw_complete"
    assert download["files"] == 7
    assert download["total_bytes_written"] > 0

    raw_files = _read_text(evidence_root / "data" / "gfs_download_2026060100_f003_after_split_root_fix.files.txt")
    assert raw_files.count(".grib2") == 7
    assert f"{run_base}/object-store/raw/gfs/2026060100" in raw_files

    canonical = _read_json(evidence_root / "data" / "canonical_convert_2026060100_f003_after_ld_fix.stdout.json")
    assert canonical == {"products": 7, "status": "canonical_ready"}
    canonical_files = sorted((run_base / "object-store" / "canonical" / "gfs" / "2026060100").glob("*/*.nc"))
    assert len(canonical_files) == 7
    assert all(path.stat().st_size > 1_000_000 for path in canonical_files)

    canonical_stderr = _read_text(evidence_root / "data" / "canonical_convert_2026060100_f003_after_ld_fix.stderr.log")
    assert "GLIBCXX_3.4.32" not in canonical_stderr
    db_summary = _read_text(evidence_root / "data" / "canonical_convert_2026060100_f003_after_ld_fix.db.txt")
    assert "cycle ('canonical_ready', '', '')" in db_summary
    for variable in (
        "air_temperature_2m",
        "prcp_rate_or_amount",
        "pressure_surface",
        "relative_humidity_2m",
        "shortwave_down",
        "wind_u_10m",
        "wind_v_10m",
    ):
        assert f"product ('{variable}', 1, 3, 3)" in db_summary

    container_canonical = _read_json(
        evidence_root / "data" / "canonical_convert_2026060100_f003_container.stdout.json"
    )
    assert container_canonical == {"products": 7, "status": "canonical_ready"}
    container_stderr = _read_text(
        evidence_root / "data" / "canonical_convert_2026060100_f003_container.stderr.log"
    )
    assert "No module named 'cfgrib'" not in container_stderr
    assert "could not translate host name" not in container_stderr
    container_db_summary = _read_text(
        evidence_root / "data" / "canonical_convert_2026060100_f003_container.db.txt"
    )
    assert "cycle ('canonical_ready', '', '')" in container_db_summary
    for variable in (
        "air_temperature_2m",
        "prcp_rate_or_amount",
        "pressure_surface",
        "relative_humidity_2m",
        "shortwave_down",
        "wind_u_10m",
        "wind_v_10m",
    ):
        assert f"product ('{variable}', 1, 3, 3)" in container_db_summary


def _run(command: list[str], *, input_text: str | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return completed.stdout


def _read_text(path: Path) -> str:
    assert path.is_file(), f"missing evidence file: {path}"
    return path.read_text(encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(_read_text(path))
