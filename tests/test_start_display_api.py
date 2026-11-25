"""``scripts/ops/start-display-api.sh``: the node-27 display API restart wrapper.

The script runs as a copy inside a temporary checkout with ``pgrep``, ``setsid``,
``curl`` and ``jq`` faked on ``PATH`` (``_prepare_start_display_api_harness`` /
``_run_start_display_api_harness``); the restart-anchor cases in
``tests/test_start_display_api_restart_anchor.py`` share the harness.

Moved unchanged from ``tests/test_two_node_docker_runtime.py`` (#3231): the wrapper
outlives the docker/compose lane that file tests.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_start_display_api_sources_display_api_port_and_ignores_legacy_alias(tmp_path: Path) -> None:
    temp_repo = tmp_path / "repo"
    object_store_root = temp_repo / "object-store"
    display_env = [
        "DATABASE_URL=postgresql://nhms_display_ro:secret@db.internal.example:5432/nhms",
        "NHMS_ENABLE_LIVE_POSTGIS_MVT=true",
        f"OBJECT_STORE_ROOT={object_store_root}",
        "NHMS_DISPLAY_API_PORT=18080",
        "NHMS_DISPLAY_PORT=19090",
    ]
    fake_bin, record_dir = _prepare_start_display_api_harness(
        temp_repo,
        display_env=display_env,
        object_store_root=object_store_root,
    )

    completed = _run_start_display_api_harness(
        temp_repo,
        fake_bin,
        record_dir,
        extra_env={"NHMS_DISPLAY_PORT": "19090"},
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    launch_text = (record_dir / "setsid.argv").read_text(encoding="utf-8")
    launch_args = launch_text.splitlines()
    assert launch_args[:4] == ["nohup", str(temp_repo / ".venv/bin/python"), "-m", "uvicorn"]
    assert "--port" in launch_args
    assert launch_args[launch_args.index("--port") + 1] == "18080"
    # Element-level and ``host:port``-anchored: argv holds the tmp repository path
    # and stdout also echoes the relaunched pid, either of which may contain 19090.
    assert "19090" not in launch_args
    assert "target=127.0.0.1:18080" in completed.stdout
    assert ":19090" not in completed.stdout + completed.stderr
    # The health probe and the models smoke check use the sourced port too.
    curl_args = (record_dir / "curl.argv").read_text(encoding="utf-8").splitlines()
    assert "http://127.0.0.1:18080/health" in curl_args
    assert "http://127.0.0.1:18080/api/v1/models?limit=1" in curl_args
    assert not any("19090" in arg for arg in curl_args)
    _assert_detached_fallback_log_in_temp_repo(completed, temp_repo)


def test_start_display_api_defaults_display_api_port_when_unset(tmp_path: Path) -> None:
    temp_repo = tmp_path / "repo"
    object_store_root = temp_repo / "object-store"
    display_env = [
        "DATABASE_URL=postgresql://nhms_display_ro:secret@db.internal.example:5432/nhms",
        "NHMS_ENABLE_LIVE_POSTGIS_MVT=true",
        f"OBJECT_STORE_ROOT={object_store_root}",
    ]
    fake_bin, record_dir = _prepare_start_display_api_harness(
        temp_repo,
        display_env=display_env,
        object_store_root=object_store_root,
    )

    completed = _run_start_display_api_harness(temp_repo, fake_bin, record_dir)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    launch_text = (record_dir / "setsid.argv").read_text(encoding="utf-8")
    launch_args = launch_text.splitlines()
    assert "--port" in launch_args
    assert launch_args[launch_args.index("--port") + 1] == "8080"
    assert launch_args[launch_args.index("--workers") + 1] == "2"
    assert "target=127.0.0.1:8080" in completed.stdout
    # The detached fallback redirect opened the harness log, not a host file.
    assert (temp_repo / "display-api.log").is_file()
    _assert_detached_fallback_log_in_temp_repo(completed, temp_repo)


@pytest.mark.parametrize("invalid_workers", ["0", "5", "many"])
def test_start_display_api_invalid_worker_count_exits_before_stop_or_relaunch(
    tmp_path: Path,
    invalid_workers: str,
) -> None:
    temp_repo = tmp_path / "repo"
    object_store_root = temp_repo / "object-store"
    display_env = [
        "DATABASE_URL=postgresql://nhms_display_ro:secret@db.internal.example:5432/nhms",
        "NHMS_ENABLE_LIVE_POSTGIS_MVT=true",
        f"OBJECT_STORE_ROOT={object_store_root}",
        f"NHMS_DISPLAY_WORKERS={invalid_workers}",
    ]
    fake_bin, record_dir = _prepare_start_display_api_harness(
        temp_repo,
        display_env=display_env,
        object_store_root=object_store_root,
    )

    completed = _run_start_display_api_harness(temp_repo, fake_bin, record_dir)

    assert completed.returncode != 0
    assert "NHMS_DISPLAY_WORKERS" in completed.stderr
    assert (invalid_workers or "<empty>") in completed.stderr
    assert not (record_dir / "pgrep.argv").exists()
    assert not (record_dir / "setsid.argv").exists()


@pytest.mark.parametrize("invalid_port", ["notaport", "70000", ""])
def test_start_display_api_invalid_display_api_port_exits_before_stop_or_relaunch(
    tmp_path: Path,
    invalid_port: str,
) -> None:
    temp_repo = tmp_path / "repo"
    object_store_root = temp_repo / "object-store"
    display_env = [
        "DATABASE_URL=postgresql://nhms_display_ro:secret@db.internal.example:5432/nhms",
        "NHMS_ENABLE_LIVE_POSTGIS_MVT=true",
        f"OBJECT_STORE_ROOT={object_store_root}",
        f"NHMS_DISPLAY_API_PORT={invalid_port}",
    ]
    fake_bin, record_dir = _prepare_start_display_api_harness(
        temp_repo,
        display_env=display_env,
        object_store_root=object_store_root,
    )

    completed = _run_start_display_api_harness(temp_repo, fake_bin, record_dir)

    assert completed.returncode != 0
    assert "NHMS_DISPLAY_API_PORT" in completed.stderr
    assert (invalid_port or "<empty>") in completed.stderr
    assert not (record_dir / "pgrep.argv").exists()
    assert not (record_dir / "setsid.argv").exists()
    assert not (record_dir / "curl.argv").exists()


@pytest.mark.parametrize(
    "missing_key",
    ["DATABASE_URL", "NHMS_ENABLE_LIVE_POSTGIS_MVT", "OBJECT_STORE_ROOT"],
)
def test_start_display_api_missing_required_key_exits_before_stop_or_relaunch(
    tmp_path: Path,
    missing_key: str,
) -> None:
    temp_repo = tmp_path / "repo"
    object_store_root = temp_repo / "object-store"
    display_env_values = {
        "DATABASE_URL": "postgresql://nhms_display_ro:secret@db.internal.example:5432/nhms",
        "NHMS_ENABLE_LIVE_POSTGIS_MVT": "true",
        "OBJECT_STORE_ROOT": str(object_store_root),
        "NHMS_DISPLAY_API_PORT": "18080",
    }
    display_env_values.pop(missing_key)
    display_env = [f"{key}={value}" for key, value in display_env_values.items()]
    fake_bin, record_dir = _prepare_start_display_api_harness(
        temp_repo,
        display_env=display_env,
        object_store_root=object_store_root,
    )

    completed = _run_start_display_api_harness(temp_repo, fake_bin, record_dir)

    assert completed.returncode != 0
    assert missing_key in completed.stderr
    assert not (record_dir / "pgrep.argv").exists()
    assert not (record_dir / "setsid.argv").exists()
    assert not (record_dir / "curl.argv").exists()


def test_start_display_api_ignores_sourced_host_and_log_path(tmp_path: Path) -> None:
    temp_repo = tmp_path / "repo"
    object_store_root = temp_repo / "object-store"
    display_env = [
        "DATABASE_URL=postgresql://nhms_display_ro:secret@db.internal.example:5432/nhms",
        "NHMS_ENABLE_LIVE_POSTGIS_MVT=true",
        f"OBJECT_STORE_ROOT={object_store_root}",
        "NHMS_DISPLAY_API_PORT=18080",
        "NHMS_DISPLAY_HOST=0.0.0.0",
        "NHMS_DISPLAY_LOG_PATH=/missing/dir/display.log",
    ]
    fake_bin, record_dir = _prepare_start_display_api_harness(
        temp_repo,
        display_env=display_env,
        object_store_root=object_store_root,
    )

    completed = _run_start_display_api_harness(temp_repo, fake_bin, record_dir)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    # The log path comes from the process environment (the harness default), never
    # from display.env.
    assert f"target=127.0.0.1:18080  log={temp_repo / 'display-api.log'} workers=" in completed.stdout
    assert "/missing/dir/display.log" not in completed.stdout + completed.stderr
    _assert_detached_fallback_log_in_temp_repo(completed, temp_repo)
    launch_text = (record_dir / "setsid.argv").read_text(encoding="utf-8")
    launch_args = launch_text.splitlines()
    assert "--host" in launch_args
    assert launch_args[launch_args.index("--host") + 1] == "127.0.0.1"
    assert "0.0.0.0" not in launch_text


def test_start_display_api_log_path_default_is_read_before_display_env_is_sourced() -> None:
    """Checked on the script text: running it unset would append to the host's log."""
    script_lines = (REPO_ROOT / "scripts/ops/start-display-api.sh").read_text(encoding="utf-8").splitlines()
    log_path_line = script_lines.index('LOG_PATH="${NHMS_DISPLAY_LOG_PATH:-/tmp/display-api.log}"')
    source_line = script_lines.index('. "$ENV_FILE"')
    assert log_path_line < source_line
    assert [line for line in script_lines if line.lstrip().startswith("LOG_PATH=")] == [script_lines[log_path_line]]


def test_start_display_api_invalid_object_store_exits_before_stop_or_relaunch(tmp_path: Path) -> None:
    temp_repo = tmp_path / "repo"
    invalid_object_store_root = temp_repo / "missing-object-store"
    display_env = [
        "DATABASE_URL=postgresql://nhms_display_ro:secret@db.internal.example:5432/nhms",
        "NHMS_ENABLE_LIVE_POSTGIS_MVT=true",
        f"OBJECT_STORE_ROOT={invalid_object_store_root}",
        "NHMS_DISPLAY_API_PORT=18080",
    ]
    fake_bin, record_dir = _prepare_start_display_api_harness(
        temp_repo,
        display_env=display_env,
        object_store_root=None,
    )

    completed = _run_start_display_api_harness(temp_repo, fake_bin, record_dir)

    assert completed.returncode != 0
    assert "OBJECT_STORE_ROOT" in completed.stderr
    assert not (record_dir / "pgrep.argv").exists()
    assert not (record_dir / "setsid.argv").exists()
    assert not (record_dir / "curl.argv").exists()


def _prepare_start_display_api_harness(
    temp_repo: Path,
    *,
    display_env: list[str],
    object_store_root: Path | None,
) -> tuple[Path, Path]:
    fake_bin = temp_repo / "fake-bin"
    record_dir = temp_repo / "records"
    env_dir = temp_repo / "infra" / "env"
    venv_bin = temp_repo / ".venv" / "bin"
    env_dir.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    fake_bin.mkdir()
    record_dir.mkdir()
    if object_store_root is not None:
        object_store_root.mkdir(parents=True)

    (env_dir / "display.env").write_text("\n".join(display_env) + "\n", encoding="utf-8")
    _write_executable(
        venv_bin / "python",
        "\n".join(
            [
                "#!/bin/sh",
                "printf '%s\\n' \"$@\" >\"$HARNESS_RECORD_DIR/python.argv\"",
                "exit 0",
            ]
        )
        + "\n",
    )
    _write_start_display_api_fake_bin(fake_bin)
    return fake_bin, record_dir


def _write_start_display_api_fake_bin(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / "pgrep",
        "\n".join(
            [
                "#!/bin/sh",
                "printf '%s\\n' \"$@\" >>\"$HARNESS_RECORD_DIR/pgrep.argv\"",
                "exit 1",
            ]
        )
        + "\n",
    )
    _write_executable(
        fake_bin / "setsid",
        "\n".join(
            [
                "#!/bin/sh",
                "printf '%s\\n' \"$@\" >\"$HARNESS_RECORD_DIR/setsid.argv\"",
                "exit 0",
            ]
        )
        + "\n",
    )
    _write_executable(
        fake_bin / "curl",
        "\n".join(
            [
                "#!/bin/sh",
                "printf '%s\\n' \"$@\" >>\"$HARNESS_RECORD_DIR/curl.argv\"",
                "url=",
                "for arg do",
                "  url=$arg",
                "done",
                "case \"$url\" in",
                "  *health*)",
                "    printf 'ok\\n'",
                "    exit 0",
                "    ;;",
                "  *models*)",
                "    printf '{\"data\":{\"items\":[{\"basin_id\":\"basin-1\"}]}}\\n'",
                "    exit 0",
                "    ;;",
                "esac",
                "exit 22",
            ]
        )
        + "\n",
    )
    _write_executable(
        fake_bin / "jq",
        "\n".join(
            [
                "#!/bin/sh",
                "cat >/dev/null",
                "case \"$*\" in",
                "  *length*)",
                "    printf '1\\n'",
                "    exit 0",
                "    ;;",
                "  *basin_id*)",
                "    printf 'basin-1\\n'",
                "    exit 0",
                "    ;;",
                "esac",
                "exit 1",
            ]
        )
        + "\n",
    )


def _run_start_display_api_harness(
    temp_repo: Path,
    fake_bin: Path,
    record_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    # The script resolves its checkout from its own location (#2638), so it runs as
    # a copy inside temp_repo: it can never reach the real checkout's display.env,
    # .venv or unit, even when pytest itself runs inside the node-27 checkout.
    script = temp_repo / "scripts" / "ops" / "start-display-api.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / "scripts/ops/start-display-api.sh", script)
    path_parts = [str(fake_bin)]
    if os.environ.get("PATH"):
        path_parts.append(os.environ["PATH"])
    temp_dir = temp_repo / "tmp"
    temp_dir.mkdir()
    # The script reads NHMS_DISPLAY_LOG_PATH from the process environment before it
    # sources display.env, so only this keeps the detached fallback's append redirect
    # off the host's /tmp/display-api.log.
    clean_env = {
        "HARNESS_RECORD_DIR": str(record_dir),
        "HOME": str(temp_repo),
        "NHMS_DISPLAY_LOG_PATH": str(temp_repo / "display-api.log"),
        "PATH": os.pathsep.join(path_parts),
        "TMPDIR": str(temp_dir),
    }
    if extra_env:
        clean_env.update(extra_env)
    return subprocess.run(
        ["bash", str(script)],
        cwd=temp_repo if cwd is None else cwd,
        env=clean_env,
        check=False,
        capture_output=True,
        text=True,
    )


def _assert_detached_fallback_log_in_temp_repo(
    completed: subprocess.CompletedProcess[str],
    temp_repo: Path,
) -> None:
    """The detached fallback's append redirect opened a file inside ``temp_repo``."""
    match = re.search(r"detached fallback relaunched pid=\d+ \(log: (.+)\)$", completed.stdout, re.MULTILINE)
    assert match is not None, completed.stdout + completed.stderr
    log_path = Path(match.group(1))
    assert log_path.is_relative_to(temp_repo), log_path
    assert log_path.is_file()


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
