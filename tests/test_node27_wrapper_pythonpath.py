"""Execution-level regression tests for issue #1067 wrapper import paths."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_AUDIT_WRAPPER = _ROOT / "scripts/node27_storage_inventory_audit_once.sh"
_CAPTURE_SCRIPT = """#!/bin/sh
{
  printf '%s\\n' "$PYTHONPATH"
  printf '%s\\n' "$1"
  shift
  printf '%s\\n' "$@"
} > "$WRAPPER_CAPTURE"
exit "${WRAPPER_EXIT_CODE:-0}"
"""


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)


def _shell_tools(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "stat", "#!/bin/sh\nprintf '600\\n'\n")
    _write_executable(bin_dir / "flock", "#!/bin/sh\nexit 0\n")
    return bin_dir


def _env_file(tmp_path: Path, text: str = "") -> Path:
    path = tmp_path / "wrapper.env"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def _audit_harness(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    bin_dir = _shell_tools(tmp_path)
    python_bin = tmp_path / "python"
    _write_executable(python_bin, _CAPTURE_SCRIPT)
    entrypoint = tmp_path / "audit.py"
    entrypoint.write_text("raise SystemExit(99)\n", encoding="utf-8")
    capture = tmp_path / "capture.txt"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "NODE27_STORAGE_INVENTORY_AUDIT_ENV_FILE": str(_env_file(tmp_path)),
        "NODE27_STORAGE_INVENTORY_AUDIT_PYTHON": str(python_bin),
        "NODE27_STORAGE_INVENTORY_AUDIT_SCRIPT": str(entrypoint),
        "WRAPPER_CAPTURE": str(capture),
    }
    return env, capture, entrypoint


@pytest.mark.parametrize("root_state", ["unset", "empty"])
def test_audit_wrapper_defaults_unset_or_empty_repo_root(
    tmp_path: Path, root_state: str
) -> None:
    env, capture, _ = _audit_harness(tmp_path)
    env["PYTHONPATH"] = ""
    if root_state == "empty":
        env["NODE27_STORAGE_INVENTORY_AUDIT_REPO_ROOT"] = ""
    else:
        env.pop("NODE27_STORAGE_INVENTORY_AUDIT_REPO_ROOT", None)

    result = subprocess.run(
        ["/bin/sh", str(_AUDIT_WRAPPER)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert capture.read_text(encoding="utf-8").splitlines()[0] == "/home/nwm/NWM"


def test_audit_wrapper_prepends_custom_root_preserves_path_args_and_exit(
    tmp_path: Path,
) -> None:
    env, capture, entrypoint = _audit_harness(tmp_path)
    custom_root = tmp_path / "custom repo"
    inherited = "/legacy path/one:/legacy/path/two"
    env_file = Path(env["NODE27_STORAGE_INVENTORY_AUDIT_ENV_FILE"])
    env_file.write_text(
        f"NODE27_STORAGE_INVENTORY_AUDIT_REPO_ROOT={shlex.quote(str(custom_root))}\n",
        encoding="utf-8",
    )
    env.update(
        {
            "PYTHONPATH": inherited,
            "WRAPPER_EXIT_CODE": "37",
        }
    )
    env.pop("NODE27_STORAGE_INVENTORY_AUDIT_REPO_ROOT", None)

    result = subprocess.run(
        ["/bin/sh", str(_AUDIT_WRAPPER), "--probe", "value with spaces"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 37
    assert capture.read_text(encoding="utf-8").splitlines() == [
        f"{custom_root}:{inherited}",
        str(entrypoint),
        "--probe",
        "value with spaces",
    ]


def test_audit_wrapper_refuses_relative_root_before_python_launch(tmp_path: Path) -> None:
    env, capture, _ = _audit_harness(tmp_path)
    env["NODE27_STORAGE_INVENTORY_AUDIT_REPO_ROOT"] = "relative/repo"

    result = subprocess.run(
        ["/bin/sh", str(_AUDIT_WRAPPER)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert not capture.exists()
    assert result.stdout == ""
    assert result.stderr.strip() == (
        '{"status":"failed","reason":"repository root must be absolute"}'
    )


def test_audit_wrapper_repo_root_enables_real_scripts_namespace_import(
    tmp_path: Path,
) -> None:
    bin_dir = _shell_tools(tmp_path)
    entrypoint = tmp_path / "import_probe.py"
    entrypoint.write_text(
        "from scripts import node27_product_archive\n"
        "print(node27_product_archive.__name__)\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONPATH": "",
        "NODE27_STORAGE_INVENTORY_AUDIT_REPO_ROOT": str(_ROOT),
        "NODE27_STORAGE_INVENTORY_AUDIT_ENV_FILE": str(_env_file(tmp_path)),
        "NODE27_STORAGE_INVENTORY_AUDIT_PYTHON": sys.executable,
        "NODE27_STORAGE_INVENTORY_AUDIT_SCRIPT": str(entrypoint),
    }

    result = subprocess.run(
        ["/bin/sh", str(_AUDIT_WRAPPER)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "scripts.node27_product_archive"


def _runtime_env(case: str, tmp_path: Path, zstd: Path) -> str:
    if case == "product_archive":
        return "\n".join(
            [
                f"NODE27_PRODUCT_ARCHIVE_OBJECT_STORE_ROOT={tmp_path / 'object-store'}",
                f"NODE27_PRODUCT_ARCHIVE_ARCHIVE_ROOT={tmp_path / 'archive'}",
                f"NODE27_PRODUCT_ARCHIVE_RECEIPT={tmp_path / 'receipt.json'}",
                f"NODE27_PRODUCT_ARCHIVE_LOCK_FILE={tmp_path / 'archive.lock'}",
                f"NODE27_PRODUCT_ARCHIVE_ZSTD={zstd}",
                "OBJECT_STORE_PREFIX=s3://nhms",
            ]
        )
    if case == "raw_retention":
        return f"NODE27_RAW_RETENTION_OBJECT_STORE_ROOT={tmp_path / 'object-store'}"
    if case == "archive_rebuild_drill":
        return "\n".join(
            [
                "PROD_DATABASE_URL_RO=postgresql://prod",
                "STAGING_DATABASE_URL=postgresql://staging",
                "POSTGRES_ADMIN_URL=postgresql://admin",
                f"NHMS_ARCHIVE_ROOT={tmp_path / 'archive'}",
                f"NHMS_ARCHIVE_REBUILD_DRILL_WORKSPACE={tmp_path / 'workspace'}",
                f"NHMS_ARCHIVE_REBUILD_DRILL_RECEIPT_PATH={tmp_path / 'drill.json'}",
                "NHMS_ARCHIVE_REBUILD_DRILL_INSTANCE_ID=node27-test",
                f"NHMS_ZSTD_BIN={zstd}",
            ]
        )
    return ""


@pytest.mark.parametrize(
    ("case", "wrapper_name", "root_env", "env_file_env", "python_env", "script_env"),
    [
        (
            "product_archive",
            "node27_product_archive_once.sh",
            "NODE27_PRODUCT_ARCHIVE_REPO_ROOT",
            "NODE27_PRODUCT_ARCHIVE_ENV_FILE",
            "NODE27_PRODUCT_ARCHIVE_PYTHON",
            "NODE27_PRODUCT_ARCHIVE_SCRIPT",
        ),
        (
            "timeseries_compression",
            "node27_timeseries_compression_once.sh",
            "NODE27_TIMESERIES_COMPRESSION_REPO_ROOT",
            "NODE27_TIMESERIES_COMPRESSION_ENV_FILE",
            "NODE27_TIMESERIES_COMPRESSION_PYTHON",
            "NODE27_TIMESERIES_COMPRESSION_SCRIPT",
        ),
        (
            "timeseries_retention",
            "node27_timeseries_retention_once.sh",
            "NODE27_TIMESERIES_RETENTION_REPO",
            "NODE27_TIMESERIES_RETENTION_ENV_FILE",
            "NODE27_TIMESERIES_RETENTION_PYTHON",
            "NODE27_TIMESERIES_RETENTION_SCRIPT",
        ),
        (
            "db_export_salvage",
            "node27_db_export_salvage_once.sh",
            "NODE27_DB_EXPORT_SALVAGE_REPO_ROOT",
            "NODE27_DB_EXPORT_SALVAGE_ENV_FILE",
            "NODE27_DB_EXPORT_SALVAGE_PYTHON",
            "NODE27_DB_EXPORT_SALVAGE_SCRIPT",
        ),
        (
            "archive_rebuild_drill",
            "node27_archive_rebuild_drill_once.sh",
            "NODE27_ARCHIVE_REBUILD_DRILL_REPO_ROOT",
            "NODE27_ARCHIVE_REBUILD_DRILL_ENV_FILE",
            "NODE27_ARCHIVE_REBUILD_DRILL_PYTHON",
            "NODE27_ARCHIVE_REBUILD_DRILL_SCRIPT",
        ),
        (
            "raw_retention",
            "node27_raw_retention_once.sh",
            "NODE27_RAW_RETENTION_REPO",
            "NODE27_RAW_RETENTION_ENV_FILE",
            None,
            None,
        ),
    ],
)
def test_sibling_wrappers_share_pythonpath_and_preserve_launch_contract(
    tmp_path: Path,
    case: str,
    wrapper_name: str,
    root_env: str,
    env_file_env: str,
    python_env: str | None,
    script_env: str | None,
) -> None:
    bin_dir = _shell_tools(tmp_path)
    zstd = tmp_path / "zstd"
    _write_executable(zstd, "#!/bin/sh\nexit 0\n")
    repo_root = tmp_path / "repo root"
    python_bin = repo_root / ".venv/bin/python"
    _write_executable(python_bin, _CAPTURE_SCRIPT)
    entrypoint = repo_root / "scripts" / f"node27_{case}.py"
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    entrypoint.write_text("raise SystemExit(99)\n", encoding="utf-8")
    env_file = _env_file(tmp_path, _runtime_env(case, tmp_path, zstd) + "\n")
    capture = tmp_path / "capture.txt"
    inherited = "/first inherited:/second-inherited"
    log_root = tmp_path / "logs"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONPATH": inherited,
        root_env: str(repo_root),
        env_file_env: str(env_file),
        "WRAPPER_CAPTURE": str(capture),
        "WRAPPER_EXIT_CODE": "37",
        "NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "retention-bootstrap.log"),
        "NODE27_TIMESERIES_RETENTION_LOG_ROOT": str(log_root),
        "NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOCK": str(tmp_path / "retention.lock"),
        "NODE27_RAW_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "raw-bootstrap.log"),
        "NODE27_RAW_RETENTION_LOG_ROOT": str(log_root),
        "NODE27_RAW_RETENTION_LOCK_PATH": str(tmp_path / "raw.lock"),
    }
    if python_env is not None and script_env is not None:
        env[python_env] = str(python_bin)
        env[script_env] = str(entrypoint)

    wrapper = _ROOT / "scripts" / wrapper_name
    result = subprocess.run(
        [str(wrapper), "--probe", "value with spaces"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 37, result.stderr
    captured = capture.read_text(encoding="utf-8").splitlines()
    assert captured[:2] == [f"{repo_root}:{inherited}", str(entrypoint)]
    if case == "raw_retention":
        assert captured[2] == "--summary-path"
        assert Path(captured[3]).parent == log_root
        assert len(captured) == 4
    else:
        assert captured[2:] == ["--probe", "value with spaces"]
