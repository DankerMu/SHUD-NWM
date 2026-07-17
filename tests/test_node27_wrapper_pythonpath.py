"""Execution-level regression tests for issue #1067 wrapper import paths."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_REPO_ROOT = "/home/nwm/NWM"
_AUDIT_WRAPPER = _ROOT / "scripts/node27_storage_inventory_audit_once.sh"
_CAPTURE_SCRIPT = """#!/bin/sh
if [ "${1:-}" = "-c" ]; then
  exit 0
fi
{
  printf '%s\\n' "$PYTHONPATH"
  printf '%s\\n' "$1"
  shift
  printf '%s\\n' "$@"
} > "$WRAPPER_CAPTURE"
exit "${WRAPPER_EXIT_CODE:-0}"
"""


_PINNED_LAUNCHER = "/usr/bin/timeout"
_PINNED_LAUNCHER_GUARD = f"[ -x {_PINNED_LAUNCHER} ] || {{"
_PINNED_LAUNCHER_EXEC = (
    f'exec {_PINNED_LAUNCHER} --signal=TERM --kill-after=30s 900s "$PYTHON_BIN" "$SCRIPT" "$@"'
)


def _write_executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)


def _launcher_available() -> bool:
    return os.access(_PINNED_LAUNCHER, os.X_OK)


def _relaunched_wrapper(tmp_path: Path, wrapper_name: str, launcher: str) -> Path:
    """Copy a wrapper with its pinned launcher re-pointed, anchored to the production text."""
    source = (_ROOT / "scripts" / wrapper_name).read_text(encoding="utf-8")
    assert source.count(_PINNED_LAUNCHER_GUARD) == 1
    assert source.count(_PINNED_LAUNCHER_EXEC) == 1
    harness = tmp_path / wrapper_name
    _write_executable(harness, source.replace(_PINNED_LAUNCHER, launcher))
    return harness


def _wrapper_under_test(tmp_path: Path, wrapper_name: str) -> Path:
    """Keep the production wrapper under test, substituting only a launcher this host lacks.

    The compression wrapper pins an absolute `/usr/bin/timeout` deliberately: resolving the
    launcher through PATH would let a caller supply their own.  A host without that exact
    path therefore cannot reach anything the wrapper does after its launcher check --
    including the import-origin guard -- so a copy re-points the pinned launcher at an
    equivalent local one and everything downstream is exercised for real.  Hosts that do
    have the launcher (Linux CI, node-27) run the production file verbatim, and the
    substitution is anchored to the production launcher text so drift breaks these tests
    rather than silently diverging from the wrapper they claim to cover.
    """
    source = (_ROOT / "scripts" / wrapper_name).read_text(encoding="utf-8")
    if _PINNED_LAUNCHER_GUARD not in source or _launcher_available():
        return _ROOT / "scripts" / wrapper_name
    launcher = tmp_path / "pinned-launcher"
    _write_executable(launcher, '#!/bin/sh\nshift 3\nexec "$@"\n')
    return _relaunched_wrapper(tmp_path, wrapper_name, str(launcher))


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


@pytest.mark.parametrize("env_pythonpath", ["", "/env-only/one:/env path/two"])
def test_audit_wrapper_prepends_custom_root_preserves_path_args_and_exit(
    tmp_path: Path, env_pythonpath: str
) -> None:
    env, capture, entrypoint = _audit_harness(tmp_path)
    custom_root = tmp_path / "custom repo"
    inherited = "/legacy path/one:/legacy/path/two"
    env_file = Path(env["NODE27_STORAGE_INVENTORY_AUDIT_ENV_FILE"])
    env_file.write_text(
        (
            f"NODE27_STORAGE_INVENTORY_AUDIT_REPO_ROOT={shlex.quote(str(custom_root))}\n"
            f"PYTHONPATH={shlex.quote(env_pythonpath)}\n"
        ),
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


_WRAPPER_CASES = [
    (
        "storage_inventory_audit",
        "node27_storage_inventory_audit_once.sh",
        "NODE27_STORAGE_INVENTORY_AUDIT_REPO_ROOT",
        "NODE27_STORAGE_INVENTORY_AUDIT_ENV_FILE",
        "NODE27_STORAGE_INVENTORY_AUDIT_PYTHON",
        "NODE27_STORAGE_INVENTORY_AUDIT_SCRIPT",
    ),
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
]


def _default_root_wrapper_harness(
    tmp_path: Path,
    case: str,
    wrapper_name: str,
    python_bin: Path,
    entrypoint: Path,
) -> Path:
    """Keep the production root resolution while redirecting unavailable launch I/O."""
    wrapper = _ROOT / "scripts" / wrapper_name
    source = wrapper.read_text(encoding="utf-8")
    shell_root = "REPO" if case in {"timeseries_retention", "raw_retention"} else "REPO_ROOT"
    assert f"${shell_root}/.venv/bin/python" in source
    assert f"${shell_root}/scripts/node27_{case}.py" in source

    if case not in {"timeseries_retention", "raw_retention"}:
        return _wrapper_under_test(tmp_path, wrapper_name)

    probe_prefix = 'if ! (cd "$REPO" && "$PYTHON_BIN" -c \'\n'
    assert source.count(probe_prefix) == 1
    source = source.replace(
        probe_prefix,
        f"if ! (cd {shlex.quote(str(tmp_path))} && \"$PYTHON_BIN\" -c '\n",
    )

    cd_command = 'cd "$REPO" || blocked "REPO_UNAVAILABLE"'
    assert source.count(cd_command) == 1
    source = source.replace(
        cd_command,
        f"cd {shlex.quote(str(tmp_path))} || blocked \"REPO_UNAVAILABLE\"",
    )

    if case == "raw_retention":
        python_assignment = 'PYTHON_BIN="$REPO/.venv/bin/python"'
        script_assignment = 'SCRIPT="$REPO/scripts/node27_raw_retention.py"'
        assert source.count(python_assignment) == 1
        assert source.count(script_assignment) == 1
        source = source.replace(
            python_assignment, f"PYTHON_BIN={shlex.quote(str(python_bin))}"
        ).replace(script_assignment, f"SCRIPT={shlex.quote(str(entrypoint))}")

    harness = tmp_path / wrapper_name
    _write_executable(harness, source)
    return harness


@pytest.mark.parametrize(
    ("case", "wrapper_name", "root_env", "env_file_env", "python_env", "script_env"),
    _WRAPPER_CASES,
)
@pytest.mark.parametrize("root_state", ["unset", "empty"])
def test_all_wrappers_default_unset_or_empty_root_to_governed_checkout(
    tmp_path: Path,
    case: str,
    wrapper_name: str,
    root_env: str,
    env_file_env: str,
    python_env: str | None,
    script_env: str | None,
    root_state: str,
) -> None:
    bin_dir = _shell_tools(tmp_path)
    zstd = tmp_path / "zstd"
    _write_executable(zstd, "#!/bin/sh\nexit 0\n")
    python_bin = tmp_path / "python"
    _write_executable(python_bin, _CAPTURE_SCRIPT)
    entrypoint = tmp_path / "entrypoint.py"
    entrypoint.write_text("raise SystemExit(99)\n", encoding="utf-8")
    capture = tmp_path / "capture.txt"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONPATH": "",
        env_file_env: str(
            _env_file(tmp_path, _runtime_env(case, tmp_path, zstd) + "\n")
        ),
        "WRAPPER_CAPTURE": str(capture),
        "NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "retention.log"),
        "NODE27_TIMESERIES_RETENTION_LOG_ROOT": str(tmp_path / "retention-logs"),
        "NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOCK": str(tmp_path / "retention.lock"),
        "NODE27_RAW_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "raw.log"),
        "NODE27_RAW_RETENTION_LOG_ROOT": str(tmp_path / "raw-logs"),
        "NODE27_RAW_RETENTION_LOCK_PATH": str(tmp_path / "raw.lock"),
    }
    if root_state == "empty":
        env[root_env] = ""
    else:
        env.pop(root_env, None)
    if python_env is not None and script_env is not None:
        env[python_env] = str(python_bin)
        env[script_env] = str(entrypoint)

    production_wrapper = _ROOT / "scripts" / wrapper_name
    production_source = production_wrapper.read_text(encoding="utf-8")
    assert f"${{{root_env}:-{_DEFAULT_REPO_ROOT}}}" in production_source
    wrapper = _default_root_wrapper_harness(
        tmp_path, case, wrapper_name, python_bin, entrypoint
    )
    result = subprocess.run(
        [str(wrapper)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    captured = capture.read_text(encoding="utf-8").splitlines()
    assert captured[:2] == [_DEFAULT_REPO_ROOT, str(entrypoint)]


@pytest.mark.parametrize(
    ("case", "wrapper_name", "root_env", "env_file_env", "python_env", "script_env"),
    _WRAPPER_CASES,
)
@pytest.mark.parametrize(
    ("bad_root", "expected_reason"),
    [
        ("relative/repo", "REPOSITORY_ROOT_NOT_ABSOLUTE"),
        ("/absolute/repo:foreign", "REPOSITORY_ROOT_PATH_LIST_DELIMITER"),
    ],
)
def test_all_wrappers_refuse_unsafe_root_loaded_from_env_file_before_python_launch(
    tmp_path: Path,
    case: str,
    wrapper_name: str,
    root_env: str,
    env_file_env: str,
    python_env: str | None,
    script_env: str | None,
    bad_root: str,
    expected_reason: str,
) -> None:
    bin_dir = _shell_tools(tmp_path)
    zstd = tmp_path / "zstd"
    _write_executable(zstd, "#!/bin/sh\nexit 0\n")
    python_bin = tmp_path / "python"
    _write_executable(python_bin, _CAPTURE_SCRIPT)
    entrypoint = tmp_path / "entrypoint.py"
    entrypoint.write_text("raise SystemExit(99)\n", encoding="utf-8")
    capture = tmp_path / "capture.txt"
    env_text = "\n".join(
        [
            _runtime_env(case, tmp_path, zstd),
            f"{root_env}={shlex.quote(bad_root)}",
        ]
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONPATH": "/caller/one:/caller/two",
        env_file_env: str(_env_file(tmp_path, env_text + "\n")),
        "WRAPPER_CAPTURE": str(capture),
        "NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "retention.log"),
        "NODE27_RAW_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "raw.log"),
    }
    env.pop(root_env, None)
    if python_env is not None and script_env is not None:
        env[python_env] = str(python_bin)
        env[script_env] = str(entrypoint)

    result = subprocess.run(
        [str(_ROOT / "scripts" / wrapper_name)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.stdout == ""
    assert not capture.exists()
    if case == "timeseries_retention":
        assert result.returncode == 2
        assert result.stderr == (
            f'{{"status":"failed","reason":"{expected_reason}"}}\n'
        )
    elif case == "raw_retention":
        assert result.returncode == 2
        assert result.stderr.endswith(f"reason={expected_reason}\n")
        assert len(result.stderr.splitlines()) == 1
    else:
        expected_json_reason = {
            "REPOSITORY_ROOT_NOT_ABSOLUTE": "repository root must be absolute",
            "REPOSITORY_ROOT_PATH_LIST_DELIMITER": (
                "repository root must not contain a path-list delimiter"
            ),
        }[expected_reason]
        assert result.returncode == 1
        assert result.stderr == (
            f'{{"status":"failed","reason":"{expected_json_reason}"}}\n'
        )


@pytest.mark.parametrize(
    ("case", "wrapper_name", "root_env", "env_file_env", "python_env", "script_env"),
    _WRAPPER_CASES,
)
@pytest.mark.parametrize("caller_pythonpath", ["", "/first inherited:/second inherited"])
@pytest.mark.parametrize("env_pythonpath", ["", "/env-only/one:/env path/two"])
@pytest.mark.parametrize("root_source", ["process", "env-file"])
def test_sibling_wrappers_share_pythonpath_and_preserve_launch_contract(
    tmp_path: Path,
    case: str,
    wrapper_name: str,
    root_env: str,
    env_file_env: str,
    python_env: str | None,
    script_env: str | None,
    caller_pythonpath: str,
    env_pythonpath: str,
    root_source: str,
) -> None:
    bin_dir = _shell_tools(tmp_path)
    zstd = tmp_path / "zstd"
    _write_executable(zstd, "#!/bin/sh\nexit 0\n")
    repo_root = tmp_path / "repo root $safe;literal"
    python_bin = repo_root / ".venv/bin/python"
    _write_executable(python_bin, _CAPTURE_SCRIPT)
    entrypoint = repo_root / "scripts" / f"node27_{case}.py"
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    entrypoint.write_text("raise SystemExit(99)\n", encoding="utf-8")
    env_lines = [_runtime_env(case, tmp_path, zstd)]
    if root_source == "env-file":
        env_lines.append(f"{root_env}={shlex.quote(str(repo_root))}")
    env_lines.append(f"PYTHONPATH={shlex.quote(env_pythonpath)}")
    env_file = _env_file(
        tmp_path,
        "\n".join(env_lines) + "\n",
    )
    capture = tmp_path / "capture.txt"
    log_root = tmp_path / "logs"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONPATH": caller_pythonpath,
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
    if root_source == "process":
        env[root_env] = str(repo_root)
    wrapper = _wrapper_under_test(tmp_path, wrapper_name)
    result = subprocess.run(
        [str(wrapper), "--probe", "value with spaces"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 37, result.stderr
    captured = capture.read_text(encoding="utf-8").splitlines()
    expected_pythonpath = (
        f"{repo_root}:{caller_pythonpath}" if caller_pythonpath else str(repo_root)
    )
    assert captured[:2] == [expected_pythonpath, str(entrypoint)]
    if case == "raw_retention":
        assert captured[2] == "--summary-path"
        assert Path(captured[3]).parent == log_root
        assert len(captured) == 4
    else:
        assert captured[2:] == ["--probe", "value with spaces"]


@pytest.mark.parametrize(
    ("case", "wrapper_name", "root_env", "env_file_env", "python_env", "script_env"),
    [wrapper_case for wrapper_case in _WRAPPER_CASES if wrapper_case[4] is not None],
)
def test_wrapper_explicit_interpreter_and_entrypoint_overrides_remain_supported(
    tmp_path: Path,
    case: str,
    wrapper_name: str,
    root_env: str,
    env_file_env: str,
    python_env: str,
    script_env: str,
) -> None:
    bin_dir = _shell_tools(tmp_path)
    zstd = tmp_path / "zstd"
    _write_executable(zstd, "#!/bin/sh\nexit 0\n")
    repo_root = tmp_path / "governed checkout"
    (repo_root / "scripts").mkdir(parents=True)
    python_bin = tmp_path / "explicit python"
    _write_executable(python_bin, _CAPTURE_SCRIPT)
    entrypoint = tmp_path / "explicit entrypoint.py"
    entrypoint.write_text("raise SystemExit(99)\n", encoding="utf-8")
    capture = tmp_path / "capture.txt"
    env_text = _runtime_env(case, tmp_path, zstd)
    if case in {
        "storage_inventory_audit",
        "product_archive",
        "timeseries_compression",
        "db_export_salvage",
    }:
        env_text += (
            f"\n{python_env}={tmp_path / 'env-file-python-must-not-win'}"
            f"\n{script_env}={tmp_path / 'env-file-script-must-not-win'}"
        )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONPATH": "/caller/one:/caller/two",
        root_env: str(repo_root),
        env_file_env: str(_env_file(tmp_path, env_text + "\n")),
        python_env: str(python_bin),
        script_env: str(entrypoint),
        "WRAPPER_CAPTURE": str(capture),
        "WRAPPER_EXIT_CODE": "23",
        "NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "retention.log"),
        "NODE27_TIMESERIES_RETENTION_LOG_ROOT": str(tmp_path / "retention-logs"),
        "NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOCK": str(tmp_path / "retention.lock"),
    }

    result = subprocess.run(
        [str(_wrapper_under_test(tmp_path, wrapper_name)), "--explicit-probe"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 23, result.stderr
    assert capture.read_text(encoding="utf-8").splitlines() == [
        f"{repo_root}:/caller/one:/caller/two",
        str(entrypoint),
        "--explicit-probe",
    ]


@pytest.mark.parametrize(
    ("case", "wrapper_name", "root_env", "env_file_env", "python_env", "script_env"),
    _WRAPPER_CASES,
)
@pytest.mark.parametrize(
    ("bad_root", "expected_reason"),
    [
        ("relative/repo", "absolute"),
        ("/absolute/repo:foreign", "delimiter"),
    ],
)
def test_all_wrappers_refuse_unsafe_root_before_python_launch(
    tmp_path: Path,
    case: str,
    wrapper_name: str,
    root_env: str,
    env_file_env: str,
    python_env: str | None,
    script_env: str | None,
    bad_root: str,
    expected_reason: str,
) -> None:
    bin_dir = _shell_tools(tmp_path)
    zstd = tmp_path / "zstd"
    _write_executable(zstd, "#!/bin/sh\nexit 0\n")
    python_bin = tmp_path / "python"
    _write_executable(python_bin, _CAPTURE_SCRIPT)
    entrypoint = tmp_path / "entrypoint.py"
    entrypoint.write_text("raise SystemExit(99)\n", encoding="utf-8")
    capture = tmp_path / "capture.txt"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONPATH": "/caller/one:/caller/two",
        root_env: bad_root,
        env_file_env: str(
            _env_file(tmp_path, _runtime_env(case, tmp_path, zstd) + "\n")
        ),
        "WRAPPER_CAPTURE": str(capture),
        "NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "retention.log"),
        "NODE27_RAW_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "raw.log"),
    }
    if python_env is not None and script_env is not None:
        env[python_env] = str(python_bin)
        env[script_env] = str(entrypoint)

    result = subprocess.run(
        [str(_ROOT / "scripts" / wrapper_name)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode in {1, 2}
    assert expected_reason.lower() in result.stderr.lower()
    assert not capture.exists()


@pytest.mark.parametrize(
    ("case", "wrapper_name", "root_env", "env_file_env", "python_env", "script_env"),
    _WRAPPER_CASES,
)
def test_all_wrappers_refuse_conflicting_regular_scripts_package_before_entrypoint(
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
    repo_root = tmp_path / "governed checkout"
    python_bin = repo_root / ".venv/bin/python"
    python_bin.parent.mkdir(parents=True)
    python_bin.symlink_to(sys.executable)
    entrypoint = repo_root / "scripts" / f"node27_{case}.py"
    marker = tmp_path / "entrypoint-ran"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    if case == "storage_inventory_audit":
        (repo_root / "scripts/node27_product_archive.py").write_text(
            "# governed module\n", encoding="utf-8"
        )
    conflict_root = tmp_path / "foreign checkout"
    conflict_init = conflict_root / "scripts/__init__.py"
    conflict_init.parent.mkdir(parents=True)
    conflict_init.write_text("raise RuntimeError('must not execute')\n", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONPATH": str(conflict_root),
        root_env: str(repo_root),
        env_file_env: str(
            _env_file(tmp_path, _runtime_env(case, tmp_path, zstd) + "\n")
        ),
        "NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "retention.log"),
        "NODE27_TIMESERIES_RETENTION_LOG_ROOT": str(tmp_path / "retention-logs"),
        "NODE27_RAW_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "raw.log"),
        "NODE27_RAW_RETENTION_LOG_ROOT": str(tmp_path / "raw-logs"),
    }

    result = subprocess.run(
        [str(_wrapper_under_test(tmp_path, wrapper_name))],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode in {1, 2}
    assert "IMPORT_ORIGIN" in result.stderr.upper() or "import origin" in result.stderr
    assert not marker.exists()


def _real_python_wrapper_harness(
    tmp_path: Path,
    case: str,
    root_env: str,
    env_file_env: str,
) -> tuple[dict[str, str], Path, Path, Path]:
    bin_dir = _shell_tools(tmp_path)
    zstd = tmp_path / "zstd"
    _write_executable(zstd, "#!/bin/sh\nexit 0\n")
    repo_root = tmp_path / "governed checkout"
    python_bin = repo_root / ".venv/bin/python"
    python_bin.parent.mkdir(parents=True)
    python_bin.symlink_to(sys.executable)
    entrypoint = repo_root / "scripts" / f"node27_{case}.py"
    entrypoint.parent.mkdir(parents=True)
    marker = tmp_path / "entrypoint-ran.json"
    entrypoint.write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        "raise SystemExit(37)\n",
        encoding="utf-8",
    )
    if case == "storage_inventory_audit":
        (repo_root / "scripts/node27_product_archive.py").write_text(
            "# governed archive module\n", encoding="utf-8"
        )
    log_root = tmp_path / "logs"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        root_env: str(repo_root),
        env_file_env: str(
            _env_file(tmp_path, _runtime_env(case, tmp_path, zstd) + "\n")
        ),
        "NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOG": str(
            tmp_path / "retention-bootstrap.log"
        ),
        "NODE27_TIMESERIES_RETENTION_LOG_ROOT": str(log_root),
        "NODE27_TIMESERIES_RETENTION_BOOTSTRAP_LOCK": str(
            tmp_path / "retention.lock"
        ),
        "NODE27_RAW_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "raw-bootstrap.log"),
        "NODE27_RAW_RETENTION_LOG_ROOT": str(log_root),
        "NODE27_RAW_RETENTION_LOCK_PATH": str(tmp_path / "raw.lock"),
    }
    return env, repo_root, entrypoint, marker


@pytest.mark.parametrize(
    ("case", "wrapper_name", "root_env", "env_file_env", "python_env", "script_env"),
    _WRAPPER_CASES,
)
def test_all_wrappers_safe_path_uses_interpreter_search_path_and_runs_entrypoint(
    tmp_path: Path,
    case: str,
    wrapper_name: str,
    root_env: str,
    env_file_env: str,
    python_env: str | None,
    script_env: str | None,
) -> None:
    env, _, _, marker = _real_python_wrapper_harness(
        tmp_path, case, root_env, env_file_env
    )
    env.update({"PYTHONPATH": "", "PYTHONSAFEPATH": "1"})

    result = subprocess.run(
        [str(_wrapper_under_test(tmp_path, wrapper_name)), "--probe", "value with spaces"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 37, result.stderr
    captured_args = json.loads(marker.read_text(encoding="utf-8"))
    if case == "raw_retention":
        assert captured_args[0] == "--summary-path"
        assert Path(captured_args[1]).parent == tmp_path / "logs"
        assert len(captured_args) == 2
    else:
        assert captured_args == ["--probe", "value with spaces"]


@pytest.mark.parametrize(
    ("case", "wrapper_name", "root_env", "env_file_env", "python_env", "script_env"),
    _WRAPPER_CASES,
)
def test_all_wrappers_refuse_regular_scripts_package_in_entrypoint_directory(
    tmp_path: Path,
    case: str,
    wrapper_name: str,
    root_env: str,
    env_file_env: str,
    python_env: str | None,
    script_env: str | None,
) -> None:
    env, _, entrypoint, marker = _real_python_wrapper_harness(
        tmp_path, case, root_env, env_file_env
    )
    shadow = entrypoint.parent / "scripts/__init__.py"
    shadow.parent.mkdir()
    shadow.write_text("# script-directory shadow\n", encoding="utf-8")
    env["PYTHONPATH"] = ""
    env.pop("PYTHONSAFEPATH", None)

    result = subprocess.run(
        [str(_wrapper_under_test(tmp_path, wrapper_name))],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode in {1, 2}
    assert "IMPORT_ORIGIN" in result.stderr.upper() or "import origin" in result.stderr
    assert not marker.exists()


def test_audit_refuses_script_directory_shadow_even_with_governed_archive_module(
    tmp_path: Path,
) -> None:
    case = _WRAPPER_CASES[0]
    env, repo_root, entrypoint, marker = _real_python_wrapper_harness(
        tmp_path, case[0], case[2], case[3]
    )
    assert (repo_root / "scripts/node27_product_archive.py").is_file()
    shadow = entrypoint.parent / "scripts/__init__.py"
    shadow.parent.mkdir()
    shadow.write_text("# shadow wins before governed module lookup\n", encoding="utf-8")
    env["PYTHONPATH"] = ""
    env.pop("PYTHONSAFEPATH", None)

    result = subprocess.run(
        [str(_AUDIT_WRAPPER)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "import origin" in result.stderr
    assert not marker.exists()


@pytest.mark.parametrize(
    ("case", "wrapper_name", "root_env", "env_file_env", "python_env", "script_env"),
    [wrapper_case for wrapper_case in _WRAPPER_CASES if wrapper_case[5] is not None],
)
def test_explicit_entrypoint_outside_root_refuses_its_scripts_shadow(
    tmp_path: Path,
    case: str,
    wrapper_name: str,
    root_env: str,
    env_file_env: str,
    python_env: str,
    script_env: str,
) -> None:
    env, _, _, governed_marker = _real_python_wrapper_harness(
        tmp_path, case, root_env, env_file_env
    )
    outside_dir = tmp_path / "supported explicit entrypoint"
    outside_script = outside_dir / "entrypoint.py"
    outside_marker = tmp_path / "outside-entrypoint-ran"
    outside_script.parent.mkdir()
    outside_script.write_text(
        "from pathlib import Path\n"
        f"Path({str(outside_marker)!r}).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    shadow = outside_dir / "scripts/__init__.py"
    shadow.parent.mkdir()
    shadow.write_text("# outside shadow\n", encoding="utf-8")
    env.update({script_env: str(outside_script), "PYTHONPATH": ""})
    env.pop("PYTHONSAFEPATH", None)

    result = subprocess.run(
        [str(_wrapper_under_test(tmp_path, wrapper_name))],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode in {1, 2}
    assert "IMPORT_ORIGIN" in result.stderr.upper() or "import origin" in result.stderr
    assert not governed_marker.exists()
    assert not outside_marker.exists()


@pytest.mark.parametrize(
    ("case", "wrapper_name", "root_env", "env_file_env", "python_env", "script_env"),
    [
        wrapper_case
        for wrapper_case in _WRAPPER_CASES
        if wrapper_case[0] in {"timeseries_retention", "raw_retention"}
    ],
)
def test_retention_wrappers_probe_empty_pythonpath_segment_from_final_cwd(
    tmp_path: Path,
    case: str,
    wrapper_name: str,
    root_env: str,
    env_file_env: str,
    python_env: str | None,
    script_env: str | None,
) -> None:
    env, _, _, marker = _real_python_wrapper_harness(
        tmp_path, case, root_env, env_file_env
    )
    caller_cwd = tmp_path / "caller cwd"
    caller_shadow = caller_cwd / "scripts/__init__.py"
    caller_shadow.parent.mkdir(parents=True)
    caller_shadow.write_text("# must not affect post-cd launch\n", encoding="utf-8")
    env["PYTHONPATH"] = ":"
    env.pop("PYTHONSAFEPATH", None)

    result = subprocess.run(
        [str(_ROOT / "scripts" / wrapper_name), "--ignored-for-raw"],
        cwd=caller_cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 37, result.stderr
    assert marker.exists()


def test_compression_wrapper_pins_an_absolute_launcher_instead_of_resolving_it_on_path() -> None:
    """Resolving the launcher through PATH would let a caller substitute their own."""
    source = (_ROOT / "scripts/node27_timeseries_compression_once.sh").read_text(encoding="utf-8")

    assert _PINNED_LAUNCHER_GUARD in source
    assert _PINNED_LAUNCHER_EXEC in source
    assert "command -v timeout" not in source
    assert "gtimeout" not in source
    assert "$(which timeout)" not in source


def test_compression_wrapper_refuses_an_unavailable_pinned_launcher(tmp_path: Path) -> None:
    """The launcher check fails closed rather than launching the entrypoint unbounded."""
    bin_dir = _shell_tools(tmp_path)
    zstd = tmp_path / "zstd"
    _write_executable(zstd, "#!/bin/sh\nexit 0\n")
    python_bin = tmp_path / "python"
    _write_executable(python_bin, _CAPTURE_SCRIPT)
    entrypoint = tmp_path / "entrypoint.py"
    entrypoint.write_text("raise SystemExit(99)\n", encoding="utf-8")
    capture = tmp_path / "capture.txt"
    wrapper = _relaunched_wrapper(
        tmp_path,
        "node27_timeseries_compression_once.sh",
        str(tmp_path / "absent-launcher"),
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONPATH": "",
        "NODE27_TIMESERIES_COMPRESSION_REPO_ROOT": str(tmp_path),
        "NODE27_TIMESERIES_COMPRESSION_ENV_FILE": str(
            _env_file(tmp_path, _runtime_env("timeseries_compression", tmp_path, zstd) + "\n")
        ),
        "NODE27_TIMESERIES_COMPRESSION_PYTHON": str(python_bin),
        "NODE27_TIMESERIES_COMPRESSION_SCRIPT": str(entrypoint),
        "WRAPPER_CAPTURE": str(capture),
    }

    result = subprocess.run(
        [str(wrapper)], env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode == 1
    assert json.loads(result.stderr) == {
        "status": "failed",
        "reason": "timeout launcher is unavailable",
    }
    assert not capture.exists()
