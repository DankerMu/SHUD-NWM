"""Execution contracts for the node-27 ordinary compression wrapper.

These tests use the production shell wrapper and a copied trusted preflight
inside a temporary checkout.  Only the timeout binary is replaced in that copy,
so the tests can inspect its argv without launching a database runner.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_PREFLIGHT = _ROOT / "scripts" / "node27_timeseries_budget_preflight.py"
_BUDGET_MODULE = _ROOT / "packages/common/node27_timeseries_compression_budget.py"
_SAFE_FS_MODULE = _ROOT / "packages/common/safe_fs.py"
_TIMEOUT_PATH = "/usr/bin/timeout"
_SECRET_DSN = "postgresql://alice:super-secret-password@127.0.0.1:55432/nhms?signed=very-secret-token"

_LANE = {
    "wrapper": "node27_timeseries_compression_once.sh",
    "root": "NODE27_TIMESERIES_COMPRESSION_REPO_ROOT",
    "python": "NODE27_TIMESERIES_COMPRESSION_PYTHON",
    "script": "NODE27_TIMESERIES_COMPRESSION_SCRIPT",
    "entrypoint": "node27_timeseries_compression.py",
}

_TIMEOUT_CAPTURE = """#!/bin/sh
{
  printf 'PYTHONPATH=%s\\n' "${PYTHONPATH-}"
  printf 'ASSEMBLED=%s\\n' "${NODE27_TIMESERIES_COMPRESSION_BUDGET_ASSEMBLED-}"
  printf 'COMPRESSION_WALL=%s\\n' "${NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_WRAPPER_WALL_SECONDS-}"
  printf 'SERVICE_WALL=%s\\n' "${NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_SERVICE_WALL_SECONDS-}"
  printf 'COMPRESSION_TIMEOUT=%s\\n' "${NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_STATEMENT_TIMEOUT_MS-}"
  printf 'COMPRESSION_BOUND=%s\\n' "${NODE27_TIMESERIES_COMPRESSION_ASSEMBLED_PER_TICK_BOUND-}"
  printf 'LANE_TOKEN=%s\\n' "${WRAPPER_LANE_TOKEN-}"
  printf 'COMPRESSION_PYTHON=%s\\n' "${NODE27_TIMESERIES_COMPRESSION_PYTHON-}"
  printf 'COMPRESSION_SCRIPT=%s\\n' "${NODE27_TIMESERIES_COMPRESSION_SCRIPT-}"
  printf '%s\\n' --argv--
  for argument in "$@"; do
    printf '%s\\n' "$argument"
  done
} > "$WRAPPER_CAPTURE"
exit "${WRAPPER_EXIT_CODE:-0}"
"""

_FAKE_PYTHON = """#!/bin/sh
if [ "${1:-}" = "-c" ]; then
  exit 0
fi
exit 99
"""


def _write_executable(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)
    return path


def _write_env(path: Path, lines: list[str], *, mode: int = 0o600) -> Path:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(mode)
    return path


def _write_compression_env(
    tmp_path: Path,
    *,
    compression_wall: int = 3900,
    service_wall: int = 3941,
    compression_timeout_ms: int = 3_600_000,
    compression_bound: int = 4,
    extra: list[str] | None = None,
) -> Path:
    lines = [
        f"DATABASE_URL='{_SECRET_DSN}'",
        f"NODE27_TIMESERIES_COMPRESSION_COMPRESS_TIMEOUT_MS={compression_timeout_ms}",
        f"NODE27_TIMESERIES_COMPRESSION_PER_TICK_BOUND={compression_bound}",
        f"NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS={compression_wall}",
        f"NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS={service_wall}",
    ]
    lines.extend(extra or [])
    return _write_env(tmp_path / "compression.env", lines)


def _install_preflight(repo_root: Path, *, timeout_path: Path | None = None) -> None:
    source = _PREFLIGHT.read_text(encoding="utf-8")
    if timeout_path is not None and _TIMEOUT_PATH in source:
        source = source.replace(_TIMEOUT_PATH, str(timeout_path))
    preflight = repo_root / "scripts/node27_timeseries_budget_preflight.py"
    preflight.parent.mkdir(parents=True, exist_ok=True)
    preflight.write_text(source, encoding="utf-8")
    packages = repo_root / "packages"
    common = packages / "common"
    common.mkdir(parents=True, exist_ok=True)
    (packages / "__init__.py").write_text("", encoding="utf-8")
    (common / "__init__.py").write_text("", encoding="utf-8")
    (common / "node27_timeseries_compression_budget.py").write_text(
        _BUDGET_MODULE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (common / "safe_fs.py").write_text(_SAFE_FS_MODULE.read_text(encoding="utf-8"), encoding="utf-8")


def _install_runner(repo_root: Path, *, name: str | None = None) -> Path:
    entrypoint = repo_root / "scripts" / (name or _LANE["entrypoint"])
    entrypoint.parent.mkdir(parents=True, exist_ok=True)
    entrypoint.write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['WRAPPER_ENTRYPOINT_MARKER']).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )
    return entrypoint


def _install_python(repo_root: Path) -> Path:
    python_bin = repo_root / ".venv/bin/python"
    python_bin.parent.mkdir(parents=True, exist_ok=True)
    python_bin.symlink_to(sys.executable)
    return python_bin


def _shell_tools(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "shell-tools"
    bin_dir.mkdir()
    _write_executable(bin_dir / "stat", "#!/bin/sh\nprintf '600\\n'\n")
    return bin_dir


def _base_process_env(
    tmp_path: Path,
    *,
    precheck_root: Path,
    compression_env: Path,
    caller_pythonpath: str = "",
) -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "NODE27_TIMESERIES_COMPRESSION_REPO_ROOT",
        "NODE27_COLD_RESIDENCY_REPO_ROOT",
        "NODE27_TIMESERIES_COMPRESSION_PYTHON",
        "NODE27_COLD_RESIDENCY_PYTHON",
        "NODE27_TIMESERIES_COMPRESSION_SCRIPT",
        "NODE27_COLD_RESIDENCY_SCRIPT",
        "NODE27_COLD_RESIDENCY_ENV_FILE",
        "PYTHONSAFEPATH",
    ):
        env.pop(key, None)
    env.update(
        {
            "PATH": f"{_shell_tools(tmp_path)}:/usr/bin:/bin",
            "PYTHONPATH": caller_pythonpath,
            "NODE27_TIMESERIES_COMPRESSION_REPO_ROOT": str(precheck_root),
            "NODE27_TIMESERIES_COMPRESSION_ENV_FILE": str(compression_env),
            "WRAPPER_CAPTURE": str(tmp_path / "timeout-capture.txt"),
            "WRAPPER_ENTRYPOINT_MARKER": str(tmp_path / "entrypoint-ran"),
        }
    )
    return env


def _wrapper_harness(
    tmp_path: Path,
    *,
    compression_wall: int = 3900,
    service_wall: int = 3941,
    compression_timeout_ms: int = 3_600_000,
    compression_bound: int = 4,
    extra: list[str] | None = None,
    timeout_path: Path | None = None,
    caller_pythonpath: str = "",
) -> tuple[Path, dict[str, str], Path, Path, Path]:
    launcher = timeout_path or _write_executable(tmp_path / "timeout-capture", _TIMEOUT_CAPTURE)
    precheck_root = tmp_path / "precheck-root"
    _install_preflight(precheck_root, timeout_path=launcher)
    _install_python(precheck_root)
    _install_runner(precheck_root)
    compression_env = _write_compression_env(
        tmp_path,
        compression_wall=compression_wall,
        service_wall=service_wall,
        compression_timeout_ms=compression_timeout_ms,
        compression_bound=compression_bound,
        extra=extra,
    )
    env = _base_process_env(
        tmp_path,
        precheck_root=precheck_root,
        compression_env=compression_env,
        caller_pythonpath=caller_pythonpath,
    )
    return (
        _ROOT / "scripts" / _LANE["wrapper"],
        env,
        Path(env["WRAPPER_CAPTURE"]),
        Path(env["WRAPPER_ENTRYPOINT_MARKER"]),
        compression_env,
    )


def _run(wrapper: Path, env: dict[str, str], *runner_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(wrapper), *runner_args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _captured_timeout(path: Path) -> tuple[dict[str, str], list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    delimiter = lines.index("--argv--")
    values = dict(line.split("=", 1) for line in lines[:delimiter])
    return values, lines[delimiter + 1 :]


def test_wrapper_refuses_own_lane_command_substitution_without_executing_it(tmp_path: Path) -> None:
    marker = tmp_path / "compression-command-substitution-ran"
    unsafe = f"UNSAFE_VALUE=$(touch {shlex.quote(str(marker))})"
    wrapper, env, capture, entrypoint_marker, _ = _wrapper_harness(tmp_path, extra=[unsafe])

    result = _run(wrapper, env)

    assert result.returncode != 0
    assert _SECRET_DSN not in result.stderr
    assert not marker.exists()
    assert not capture.exists()
    assert not entrypoint_marker.exists()


@pytest.mark.parametrize(
    ("compression_wall", "service_wall", "timeout_ms", "bound", "expected_wall"),
    [
        (3900, 3941, 3_600_000, 4, "3900s"),
        (5700, 5741, 5_400_000, 1, "5700s"),
    ],
)
def test_thin_wrapper_launches_the_preflight_resolved_timeout(
    tmp_path: Path,
    compression_wall: int,
    service_wall: int,
    timeout_ms: int,
    bound: int,
    expected_wall: str,
) -> None:
    wrapper, env, capture, entrypoint_marker, _ = _wrapper_harness(
        tmp_path,
        compression_wall=compression_wall,
        service_wall=service_wall,
        compression_timeout_ms=timeout_ms,
        compression_bound=bound,
        extra=["WRAPPER_LANE_TOKEN=compression-only"],
    )

    result = _run(wrapper, env, "--probe", "value with spaces")

    assert result.returncode == 0, result.stderr
    values, argv = _captured_timeout(capture)
    assert values == {
        "PYTHONPATH": str(Path(env[_LANE["root"]])),
        "ASSEMBLED": "1",
        "COMPRESSION_WALL": str(compression_wall),
        "SERVICE_WALL": str(service_wall),
        "COMPRESSION_TIMEOUT": str(timeout_ms),
        "COMPRESSION_BOUND": str(bound),
        "LANE_TOKEN": "compression-only",
        "COMPRESSION_PYTHON": "",
        "COMPRESSION_SCRIPT": "",
    }
    assert argv == [
        "--signal=TERM",
        "--kill-after=30s",
        expected_wall,
        str(Path(env[_LANE["root"]]) / ".venv/bin/python"),
        str(Path(env[_LANE["root"]]) / "scripts" / _LANE["entrypoint"]),
        "--probe",
        "value with spaces",
    ]
    assert not entrypoint_marker.exists()
    assert "NODE27_COLD_RESIDENCY_ENV_FILE" not in env


def test_wrapper_succeeds_with_absent_cold_env(tmp_path: Path) -> None:
    wrapper, env, capture, entrypoint_marker, _ = _wrapper_harness(tmp_path)
    assert "NODE27_COLD_RESIDENCY_ENV_FILE" not in env
    assert not (tmp_path / "cold.env").exists()

    result = _run(wrapper, env, "--enforce")

    assert result.returncode == 0, result.stderr
    _values, argv = _captured_timeout(capture)
    assert argv[-1] == "--enforce"
    assert not entrypoint_marker.exists()


def test_wrapper_refuses_below_bound_service_wall_before_timeout_or_entrypoint(tmp_path: Path) -> None:
    wrapper, env, capture, entrypoint_marker, compression_env = _wrapper_harness(tmp_path)
    compression_env.write_text(
        compression_env.read_text(encoding="utf-8").replace(
            "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS=3941",
            "NODE27_TIMESERIES_COMPRESSION_SYSTEMD_WALL_SECONDS=3940",
        ),
        encoding="utf-8",
    )
    compression_env.chmod(0o600)

    result = _run(wrapper, env)

    assert result.returncode != 0
    assert _SECRET_DSN not in result.stderr
    assert not capture.exists()
    assert not entrypoint_marker.exists()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "wrong-mode", "directory", "oversized", "duplicate"])
def test_wrapper_refuses_unsafe_env_file_before_timeout(tmp_path: Path, unsafe_kind: str) -> None:
    wrapper, env, capture, entrypoint_marker, compression_env = _wrapper_harness(tmp_path)
    if unsafe_kind == "symlink":
        target = compression_env.with_name(f"{compression_env.stem}-target.env")
        compression_env.rename(target)
        compression_env.symlink_to(target)
    elif unsafe_kind == "wrong-mode":
        compression_env.chmod(0o640)
    elif unsafe_kind == "directory":
        compression_env.unlink()
        compression_env.mkdir()
    elif unsafe_kind == "oversized":
        compression_env.write_bytes(b"A" * (128 * 1024))
        compression_env.chmod(0o600)
    elif unsafe_kind == "duplicate":
        compression_env.write_text(
            compression_env.read_text(encoding="utf-8")
            + "NODE27_TIMESERIES_COMPRESSION_WRAPPER_WALL_SECONDS=3900\n",
            encoding="utf-8",
        )
        compression_env.chmod(0o600)
    else:
        raise AssertionError(unsafe_kind)

    result = _run(wrapper, env)

    assert result.returncode != 0
    assert _SECRET_DSN not in result.stderr
    assert not capture.exists()
    assert not entrypoint_marker.exists()


def test_wrapper_uses_caller_overrides_and_rebuilds_pythonpath_from_own_root(tmp_path: Path) -> None:
    precheck_root = tmp_path / "precheck-root"
    runner_root = tmp_path / "runner-root"
    explicit_python = _write_executable(tmp_path / "explicit-python", _FAKE_PYTHON)
    explicit_script = tmp_path / "explicit-entrypoint.py"
    explicit_script.write_text("raise SystemExit(99)\n", encoding="utf-8")
    own_extra = [
        f"{_LANE['root']}={runner_root}",
        f"{_LANE['python']}={tmp_path / 'env-file-python-must-not-select-launch'}",
        f"{_LANE['script']}={tmp_path / 'env-file-script-must-not-select-launch'}",
        "PYTHONPATH=/env-file-must-not-win",
    ]
    wrapper, env, capture, entrypoint_marker, _ = _wrapper_harness(
        tmp_path,
        caller_pythonpath="/caller/one:/caller/two",
        extra=own_extra,
    )
    _install_runner(runner_root)
    env[_LANE["root"]] = str(precheck_root)
    env[_LANE["python"]] = str(explicit_python)
    env[_LANE["script"]] = str(explicit_script)

    result = _run(wrapper, env, "--explicit-probe")

    assert result.returncode == 0, result.stderr
    values, argv = _captured_timeout(capture)
    assert values["PYTHONPATH"] == f"{runner_root}:/caller/one:/caller/two"
    assert values["COMPRESSION_PYTHON"] == str(explicit_python)
    assert values["COMPRESSION_SCRIPT"] == str(explicit_script)
    assert argv[3:5] == [str(explicit_python), str(explicit_script)]
    assert argv[5:] == ["--explicit-probe"]
    assert not entrypoint_marker.exists()


def test_lane_file_python_and_script_assignments_remain_inert_for_launch(tmp_path: Path) -> None:
    own_extra = [
        f"{_LANE['python']}={tmp_path / 'lane-python-must-not-launch'}",
        f"{_LANE['script']}={tmp_path / 'lane-script-must-not-launch.py'}",
    ]
    wrapper, env, capture, entrypoint_marker, _ = _wrapper_harness(tmp_path, extra=own_extra)

    result = _run(wrapper, env)

    assert result.returncode == 0, result.stderr
    _values, argv = _captured_timeout(capture)
    root = Path(env[_LANE["root"]])
    assert argv[3:5] == [
        str(root / ".venv/bin/python"),
        str(root / "scripts" / _LANE["entrypoint"]),
    ]
    assert not entrypoint_marker.exists()


@pytest.mark.parametrize("shadow", ["foreign-package", "entrypoint-directory", "explicit-entrypoint-directory"])
def test_import_origin_guard_refuses_conflicting_scripts_before_timeout(tmp_path: Path, shadow: str) -> None:
    wrapper, env, capture, entrypoint_marker, _ = _wrapper_harness(tmp_path)
    repo_root = Path(env[_LANE["root"]])
    if shadow == "foreign-package":
        foreign = tmp_path / "foreign"
        init = foreign / "scripts/__init__.py"
        init.parent.mkdir(parents=True)
        init.write_text("# foreign regular package\n", encoding="utf-8")
        env["PYTHONPATH"] = str(foreign)
    elif shadow == "entrypoint-directory":
        init = repo_root / "scripts/scripts/__init__.py"
        init.parent.mkdir(parents=True)
        init.write_text("# entrypoint-directory shadow\n", encoding="utf-8")
    elif shadow == "explicit-entrypoint-directory":
        outside = tmp_path / "outside-entrypoint"
        outside.mkdir()
        script = outside / "entrypoint.py"
        script.write_text("raise SystemExit(99)\n", encoding="utf-8")
        init = outside / "scripts/__init__.py"
        init.parent.mkdir()
        init.write_text("# explicit entrypoint shadow\n", encoding="utf-8")
        env[_LANE["script"]] = str(script)
    else:
        raise AssertionError(shadow)

    result = _run(wrapper, env)

    assert result.returncode != 0
    assert json.loads(result.stderr) == {
        "status": "failed",
        "reason": "scripts import origin is outside repository root",
    }
    assert not capture.exists()
    assert not entrypoint_marker.exists()


def test_import_origin_guard_accepts_safe_path_with_final_pythonpath(tmp_path: Path) -> None:
    wrapper, env, capture, _, _ = _wrapper_harness(tmp_path)
    env["PYTHONSAFEPATH"] = "1"

    result = _run(wrapper, env)

    assert result.returncode == 0, result.stderr
    values, _ = _captured_timeout(capture)
    assert values["PYTHONPATH"] == str(Path(env[_LANE["root"]]))


def test_preflight_refuses_unavailable_absolute_timeout_before_entrypoint(tmp_path: Path) -> None:
    absent = tmp_path / "missing-timeout"
    wrapper, env, capture, entrypoint_marker, _ = _wrapper_harness(tmp_path, timeout_path=absent)

    result = _run(wrapper, env)

    assert result.returncode != 0
    assert json.loads(result.stderr) == {
        "status": "failed",
        "reason": "timeout launcher is unavailable",
    }
    assert not capture.exists()
    assert not entrypoint_marker.exists()


@pytest.mark.parametrize("unsafe_kind", ["missing", "directory"])
def test_wrapper_refuses_missing_or_nonregular_trusted_preflight_interpreter(
    tmp_path: Path, unsafe_kind: str
) -> None:
    wrapper, env, capture, entrypoint_marker, _ = _wrapper_harness(tmp_path)
    precheck_python = Path(env[_LANE["root"]]) / ".venv/bin/python"
    precheck_python.unlink()
    if unsafe_kind == "directory":
        precheck_python.mkdir()

    result = _run(wrapper, env)

    assert result.returncode == 1
    assert json.loads(result.stderr) == {
        "status": "failed",
        "reason": "python executable is unavailable",
    }
    assert not capture.exists()
    assert not entrypoint_marker.exists()


@pytest.mark.parametrize("path_name", ["compression", "root"])
def test_thin_wrapper_refuses_relative_bootstrap_paths_before_preflight(
    tmp_path: Path, path_name: str
) -> None:
    wrapper, env, capture, entrypoint_marker, _ = _wrapper_harness(tmp_path)
    env_key = {
        "compression": "NODE27_TIMESERIES_COMPRESSION_ENV_FILE",
        "root": _LANE["root"],
    }[path_name]
    env[env_key] = "relative/path"

    result = _run(wrapper, env)

    assert result.returncode == 1
    assert json.loads(result.stderr) == {
        "status": "failed",
        "reason": "wrapper paths must be absolute",
    }
    assert not capture.exists()
    assert not entrypoint_marker.exists()


@pytest.mark.parametrize("unsafe_kind", ["missing", "symlink"])
def test_thin_wrapper_refuses_missing_or_symlinked_trusted_preflight(
    tmp_path: Path, unsafe_kind: str
) -> None:
    wrapper, env, capture, entrypoint_marker, _ = _wrapper_harness(tmp_path)
    preflight = Path(env[_LANE["root"]]) / "scripts/node27_timeseries_budget_preflight.py"
    if unsafe_kind == "missing":
        preflight.unlink()
    else:
        target = tmp_path / "linked-preflight.py"
        target.write_text(preflight.read_text(encoding="utf-8"), encoding="utf-8")
        preflight.unlink()
        preflight.symlink_to(target)

    result = _run(wrapper, env)

    assert result.returncode == 1
    assert json.loads(result.stderr) == {
        "status": "failed",
        "reason": "compression budget preflight is unavailable or a symlink",
    }
    assert not capture.exists()
    assert not entrypoint_marker.exists()


@pytest.mark.parametrize("unsafe_kind", ["missing-python", "missing-script", "symlink-script"])
def test_preflight_refuses_unsafe_runner_paths_before_timeout(tmp_path: Path, unsafe_kind: str) -> None:
    wrapper, env, capture, entrypoint_marker, _ = _wrapper_harness(tmp_path)
    if unsafe_kind == "missing-python":
        env[_LANE["python"]] = str(tmp_path / "missing-python")
        expected = "python executable is unavailable"
    elif unsafe_kind == "missing-script":
        env[_LANE["script"]] = str(tmp_path / "missing-script.py")
        expected = "compression entrypoint is unavailable or a symlink"
    elif unsafe_kind == "symlink-script":
        target = tmp_path / "target-entrypoint.py"
        target.write_text("raise SystemExit(99)\n", encoding="utf-8")
        link = tmp_path / "linked-entrypoint.py"
        link.symlink_to(target)
        env[_LANE["script"]] = str(link)
        expected = "compression entrypoint is unavailable or a symlink"
    else:
        raise AssertionError(unsafe_kind)

    result = _run(wrapper, env)

    assert result.returncode == 1
    failure = json.loads(result.stderr)
    assert failure["status"] == "failed"
    assert failure["reason"] == expected
    assert not capture.exists()
    assert not entrypoint_marker.exists()


def test_thin_wrapper_never_sources_lane_env_and_preflight_owns_absolute_timeout() -> None:
    wrapper = (_ROOT / "scripts" / _LANE["wrapper"]).read_text(encoding="utf-8")
    assert ". \"$" not in wrapper
    assert "source " not in wrapper
    assert "WALL=" not in wrapper
    assert _TIMEOUT_PATH not in wrapper
    assert "--launch compression" in wrapper
    assert "--cold-env" not in wrapper
    preflight = _PREFLIGHT.read_text(encoding="utf-8")
    assert "os.execve(" in preflight
    assert '"/usr/bin/timeout"' in preflight
    assert "command -v timeout" not in preflight
    assert "gtimeout" not in preflight
    assert "python -E" in wrapper or ' -E "' in wrapper
    assert "--cold-env" not in preflight


def test_wrapper_keeps_runner_arguments_after_double_dash(tmp_path: Path) -> None:
    wrapper, env, capture, entrypoint_marker, _ = _wrapper_harness(tmp_path)

    result = _run(wrapper, env, "--enforce", "--receipt-path", "/tmp/owned-receipt.json")

    assert result.returncode == 0, result.stderr
    _values, argv = _captured_timeout(capture)
    assert argv[-3:] == ["--enforce", "--receipt-path", "/tmp/owned-receipt.json"]
    assert argv[:3] == ["--signal=TERM", "--kill-after=30s", "3900s"]
    assert not entrypoint_marker.exists()


def test_wrapper_refuses_unsafe_nonregular_path_without_timeout(tmp_path: Path) -> None:
    wrapper, env, capture, entrypoint_marker, compression_env = _wrapper_harness(tmp_path)
    env["NODE27_TIMESERIES_COMPRESSION_ENV_FILE"] = str(tmp_path / "missing.env")

    result = _run(wrapper, env)

    assert result.returncode != 0
    assert _SECRET_DSN not in result.stderr
    assert not capture.exists()
    assert not entrypoint_marker.exists()
    assert not compression_env.is_symlink()

