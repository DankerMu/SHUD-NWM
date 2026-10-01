"""#2032 MVT cache retention: systemd units, env template and the ``_once.sh`` wrapper.

Partition of ``tests/test_node27_mvt_cache_retention.py`` (#2490, pure move);
the governing invariant is stated there.
"""

from __future__ import annotations

import ast
import fcntl
import json
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_mvt_cache_retention as runner
from tests.node27_mvt_cache_retention_helpers import (
    _AGED,
    _ENV_EXAMPLE_PATH,
    _REPO_ROOT,
    _SERVICE_PATH,
    _SHA_A,
    _TIMER_PATH,
    _WRAPPER_PATH,
    _clean_env,  # noqa: F401 -- autouse
    _code_only,
    _mixed_tree,
)


# ---------------------------------------------------------------------------
# Unit / timer / env template files
# ---------------------------------------------------------------------------
def test_mvt_cache_retention_service_bootstraps_log_dir() -> None:
    service_text = _SERVICE_PATH.read_text(encoding="utf-8")

    assert (
        "ExecStartPre=/usr/bin/mkdir -p /home/nwm/node27-mvt-cache-retention-logs" in service_text
    )
    assert (
        "StandardOutput=append:/home/nwm/node27-mvt-cache-retention-logs/systemd.log"
        in service_text
    )
    assert (
        "StandardError=append:/home/nwm/node27-mvt-cache-retention-logs/systemd.err"
        in service_text
    )
    assert "Type=oneshot" in service_text
    assert "WorkingDirectory=/home/nwm/NWM" in service_text
    assert (
        "Environment=NODE27_MVT_CACHE_RETENTION_ENV_FILE="
        "/home/nwm/NWM/infra/env/node27-mvt-cache-retention.env" in service_text
    )
    assert (
        "ExecStart=/home/nwm/NWM/scripts/node27_mvt_cache_retention_once.sh" in service_text
    )
    lines = service_text.splitlines()
    pre_index = next(i for i, line in enumerate(lines) if line.startswith("ExecStartPre="))
    start_index = next(i for i, line in enumerate(lines) if line.startswith("ExecStart="))
    assert pre_index < start_index


def test_mvt_cache_retention_timer_fires_daily_and_catches_up() -> None:
    timer_text = _TIMER_PATH.read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* 04:05:00 UTC" in timer_text
    assert "Persistent=true" in timer_text
    assert "Unit=nhms-node27-mvt-cache-retention.service" in timer_text
    assert "WantedBy=timers.target" in timer_text


def test_env_example_pins_the_display_process_cache_root_and_a_health_criterion() -> None:
    text = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    assert "NHMS_MVT_FILE_CACHE_DIR=/home/nwm/.cache/nhms/mvt" in text
    assert "the value the display API PROCESS" in text
    assert "display.example" in text
    assert "NODE27_MVT_CACHE_RETENTION_DAYS=14" in text
    assert "NODE27_MVT_CACHE_RETENTION_LOG_ROOT=/home/nwm/node27-mvt-cache-retention-logs" in text
    assert "NODE27_MVT_CACHE_RETENTION_LOCK_PATH=/tmp/node27-mvt-cache-retention.lock" in text
    assert "# NODE27_MVT_CACHE_RETENTION_ENABLED=true" in text
    assert "# NODE27_MVT_CACHE_RETENTION_PLAN_ONLY=false" in text
    assert 'execution_mode == "production_execute"' in text
    assert "now - 26*3600" in text
    assert ".failed | length == 0" in text


def test_the_raw_retention_env_example_points_at_this_runner() -> None:
    sibling = (_REPO_ROOT / "infra/env/node27-raw-retention.example").read_text(encoding="utf-8")

    assert "the MVT tile cache is a sibling directory here" in sibling
    assert "scripts/node27_mvt_cache_retention.py" in sibling


# ---------------------------------------------------------------------------
# Wrapper contract (real bash subprocess)
# ---------------------------------------------------------------------------
def _wrapper_repo(tmp_path: Path, *, runner_rc: int = 0, with_python: bool = True) -> Path:
    """A stand-in repo: `.venv/bin/python` plays the runner, no real work."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "node27_mvt_cache_retention.py").write_text("", encoding="utf-8")
    if with_python:
        (repo / ".venv" / "bin").mkdir(parents=True)
        python_bin = repo / ".venv" / "bin" / "python"
        python_bin.write_text(
            "#!/bin/sh\n" 'echo "RUNNER_INVOKED args=$*"\n' f"exit {runner_rc}\n",
            encoding="utf-8",
        )
        python_bin.chmod(0o755)
    return repo


def _wrapper_env(tmp_path: Path, repo: Path, env_file: Path, *, bin_dir: Path | None = None) -> dict[str, str]:
    path = os.environ.get("PATH", "/usr/bin:/bin")
    if bin_dir is not None:
        path = f"{bin_dir}:{path}"
    return {
        "PATH": path,
        "NODE27_MVT_CACHE_RETENTION_REPO": str(repo),
        "NODE27_MVT_CACHE_RETENTION_ENV_FILE": str(env_file),
        "NODE27_MVT_CACHE_RETENTION_BOOTSTRAP_LOG": str(tmp_path / "bootstrap.log"),
        "NODE27_MVT_CACHE_RETENTION_LOG_ROOT": str(tmp_path / "logs"),
        "NODE27_MVT_CACHE_RETENTION_LOG_FILE": str(tmp_path / "logs" / "wrapper.log"),
        "NODE27_MVT_CACHE_RETENTION_LOCK_PATH": str(tmp_path / "wrapper.lock"),
        "NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH": str(tmp_path / "logs" / "summary.json"),
    }


def _flock_shim(tmp_path: Path, *, exit_code: int) -> Path:
    """macOS ships no `flock(1)`; without a shim every wrapper test would take
    the "previous run still active" branch and pass vacuously."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    shim = bin_dir / "flock"
    shim.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
    shim.chmod(0o755)
    return bin_dir


def _run_wrapper(env: dict[str, str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", str(_WRAPPER_PATH)],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _wrapper_output(tmp_path: Path, result: subprocess.CompletedProcess[str]) -> str:
    bootstrap = tmp_path / "bootstrap.log"
    log_file = tmp_path / "logs" / "wrapper.log"
    return "".join(
        [
            result.stdout,
            result.stderr,
            bootstrap.read_text(encoding="utf-8") if bootstrap.exists() else "",
            log_file.read_text(encoding="utf-8") if log_file.exists() else "",
        ]
    )


def test_wrapper_refuses_a_missing_env_file(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path)
    result = _run_wrapper(_wrapper_env(tmp_path, repo, tmp_path / "absent.env"))

    assert result.returncode == 2
    combined = _wrapper_output(tmp_path, result)
    assert "ENV_FILE_MISSING" in combined
    assert "RUNNER_INVOKED" not in combined


def test_wrapper_refuses_a_symlinked_env_file(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path)
    target = tmp_path / "real.env"
    target.write_text("", encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "link.env"
    link.symlink_to(target)

    result = _run_wrapper(_wrapper_env(tmp_path, repo, link))

    assert result.returncode == 2
    combined = _wrapper_output(tmp_path, result)
    assert "ENV_FILE_SYMLINK_FORBIDDEN" in combined
    assert "RUNNER_INVOKED" not in combined


def test_wrapper_refuses_a_world_readable_env_file(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path)
    env_file = tmp_path / "runner.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o640)

    result = _run_wrapper(_wrapper_env(tmp_path, repo, env_file))

    assert result.returncode == 2
    combined = _wrapper_output(tmp_path, result)
    assert "ENV_FILE_MODE_UNSAFE" in combined
    assert "RUNNER_INVOKED" not in combined


def test_wrapper_refuses_a_missing_interpreter(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path, with_python=False)
    env_file = tmp_path / "runner.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)

    result = _run_wrapper(_wrapper_env(tmp_path, repo, env_file))

    assert result.returncode == 2
    combined = _wrapper_output(tmp_path, result)
    assert "PYTHON_EXECUTABLE_UNAVAILABLE" in combined


@pytest.mark.parametrize("runner_rc", [0, 1, 2])
def test_wrapper_propagates_the_runner_exit_code(tmp_path: Path, runner_rc: int) -> None:
    repo = _wrapper_repo(tmp_path, runner_rc=runner_rc)
    env_file = tmp_path / "runner.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    bin_dir = _flock_shim(tmp_path, exit_code=0)

    result = _run_wrapper(_wrapper_env(tmp_path, repo, env_file, bin_dir=bin_dir))

    assert result.returncode == runner_rc
    log_text = (tmp_path / "logs" / "wrapper.log").read_text(encoding="utf-8")
    assert "RUNNER_INVOKED args=" in log_text
    assert "--summary-path" in log_text
    assert f"node27-mvt-cache-retention: done rc={runner_rc}" in log_text


def test_wrapper_skips_the_tick_when_the_lock_is_held(tmp_path: Path) -> None:
    """`flock -n` failing is a benign skip (rc 0), not a failure."""
    repo = _wrapper_repo(tmp_path)
    env_file = tmp_path / "runner.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    bin_dir = _flock_shim(tmp_path, exit_code=1)

    result = _run_wrapper(_wrapper_env(tmp_path, repo, env_file, bin_dir=bin_dir))

    assert result.returncode == 0
    combined = _wrapper_output(tmp_path, result)
    assert "previous run still active, skipping tick" in combined
    assert "RUNNER_INVOKED" not in combined


@pytest.mark.skipif(shutil.which("flock") is None, reason="flock(1) is not installed (macOS)")
def test_wrapper_skips_the_tick_against_a_really_held_flock(tmp_path: Path) -> None:
    """The same skip, driven through the real `flock(1)` on the CI/node-27 lane."""
    repo = _wrapper_repo(tmp_path)
    env_file = tmp_path / "runner.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    env = _wrapper_env(tmp_path, repo, env_file)
    holder = os.open(env["NODE27_MVT_CACHE_RETENTION_LOCK_PATH"], os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _run_wrapper(env)
    finally:
        os.close(holder)

    assert result.returncode == 0
    combined = _wrapper_output(tmp_path, result)
    assert "previous run still active, skipping tick" in combined
    assert "RUNNER_INVOKED" not in combined


def test_wrapper_sources_the_env_file_and_creates_the_log_root(tmp_path: Path) -> None:
    repo = _wrapper_repo(tmp_path)
    env_file = tmp_path / "runner.env"
    env_file.write_text(
        f"NODE27_MVT_CACHE_RETENTION_LOG_ROOT={tmp_path / 'sourced-logs'}\n", encoding="utf-8"
    )
    env_file.chmod(0o600)
    bin_dir = _flock_shim(tmp_path, exit_code=0)
    env = _wrapper_env(tmp_path, repo, env_file, bin_dir=bin_dir)
    del env["NODE27_MVT_CACHE_RETENTION_LOG_ROOT"]
    del env["NODE27_MVT_CACHE_RETENTION_LOG_FILE"]
    del env["NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH"]

    result = _run_wrapper(env)

    assert result.returncode == 0, result.stderr
    sourced = tmp_path / "sourced-logs"
    assert sourced.is_dir()
    log_text = (sourced / "mvt-cache-retention.log").read_text(encoding="utf-8")
    assert "RUNNER_INVOKED args=" in log_text
    assert f"--summary-path {sourced}/mvt-cache-retention-" in log_text


# ---------------------------------------------------------------------------
# #2284: one summary file per run, even within one second.
# ---------------------------------------------------------------------------
_PINNED_STAMP = "20260912T165207Z"


def _pinned_date_shim(bin_dir: Path) -> None:
    """`date` frozen at 2026-09-12T16:52:07Z for every format the wrapper asks
    for (`+%s` for elapsed, the compact stamp for the file name, the RFC 3339
    stamp for log lines), so two runs are provably inside one second."""
    shim = bin_dir / "date"
    shim.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "  *%s*) echo 1757695927 ;;\n"
        f"  *%Y%m%dT%H%M%SZ*) echo {_PINNED_STAMP} ;;\n"
        "  *) echo 2026-09-12T16:52:07Z ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)


def _summary_writing_repo(tmp_path: Path, *, runner_rc: int = 0, write: bool = True) -> Path:
    """A stand-in runner that behaves like `_write_summary`: tmp file + rename
    onto `--summary-path`, content unique per process."""
    repo = _wrapper_repo(tmp_path)
    python_bin = repo / ".venv" / "bin" / "python"
    body = (
        '  printf \'{"pid": %s}\\n\' "$$" > "$sp.tmp" && mv "$sp.tmp" "$sp"\n' if write else ""
    )
    python_bin.write_text(
        "#!/bin/sh\n"
        'sp=""\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--summary-path" ]; then sp="$2"; shift; fi\n'
        "  shift\n"
        "done\n"
        'echo "RUNNER_INVOKED summary=$sp"\n'
        'if [ -n "$sp" ]; then\n'
        f"{body}"
        "  :\n"
        "fi\n"
        f"exit {runner_rc}\n",
        encoding="utf-8",
    )
    python_bin.chmod(0o755)
    return repo


def _default_summary_env(tmp_path: Path, repo: Path, *, flock_rc: int = 0) -> dict[str, str]:
    env_file = tmp_path / "runner.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    bin_dir = _flock_shim(tmp_path, exit_code=flock_rc)
    _pinned_date_shim(bin_dir)
    env = _wrapper_env(tmp_path, repo, env_file, bin_dir=bin_dir)
    del env["NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH"]
    return env


def _summaries(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "logs").glob("mvt-cache-retention-*.json"))


def test_two_same_second_runs_leave_two_summary_files(tmp_path: Path) -> None:
    """The runbook's plan-only-then-production flow inside one second."""
    repo = _summary_writing_repo(tmp_path)
    env = _default_summary_env(tmp_path, repo)

    first = _run_wrapper(env)
    second = _run_wrapper(env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    names = [path.name for path in _summaries(tmp_path)]
    assert names == [
        f"mvt-cache-retention-{_PINNED_STAMP}-2.json",
        f"mvt-cache-retention-{_PINNED_STAMP}.json",
    ]
    pids = {json.loads(path.read_text(encoding="utf-8"))["pid"] for path in _summaries(tmp_path)}
    assert len(pids) == 2
    # Readers use `ls -t mvt-cache-retention-*.json | head -1`: the collision
    # suffix stays inside that glob (which is what `_summaries` globs).
    lines = (tmp_path / "logs" / "wrapper.log").read_text(encoding="utf-8").splitlines()
    # The start/done lines name the file each run actually got.
    for name in names:
        path = tmp_path / "logs" / name
        assert any(line.endswith(f"start summary={path}") for line in lines), lines
        assert any("done rc=0" in line and line.endswith(f"summary={path}") for line in lines), lines


def test_a_skipped_tick_leaves_no_summary_file(tmp_path: Path) -> None:
    repo = _summary_writing_repo(tmp_path)
    env = _default_summary_env(tmp_path, repo, flock_rc=1)

    result = _run_wrapper(env)

    assert result.returncode == 0
    assert "previous run still active, skipping tick" in _wrapper_output(tmp_path, result)
    assert list((tmp_path / "logs").glob("*.json")) == []


def test_a_blocked_run_leaves_no_summary_file(tmp_path: Path) -> None:
    repo = _summary_writing_repo(tmp_path)
    env = _default_summary_env(tmp_path, repo)
    env["NODE27_MVT_CACHE_RETENTION_LOCK_PATH"] = "relative.lock"

    result = _run_wrapper(env)

    assert result.returncode == 2
    assert "LOCK_PATH_NOT_ABSOLUTE" in _wrapper_output(tmp_path, result)
    assert list((tmp_path / "logs").glob("*.json")) == []


def test_a_runner_that_dies_before_writing_leaves_no_empty_summary(tmp_path: Path) -> None:
    """An empty reservation would become the NEWEST `*.json` and every reader's
    `ls -t | head -1` would parse nothing; the wrapper removes its own."""
    repo = _summary_writing_repo(tmp_path, runner_rc=1, write=False)
    env = _default_summary_env(tmp_path, repo)

    result = _run_wrapper(env)

    assert result.returncode == 1
    assert "RUNNER_INVOKED summary=" in _wrapper_output(tmp_path, result)
    assert list((tmp_path / "logs").glob("*.json")) == []


def test_an_explicit_summary_override_is_used_verbatim(tmp_path: Path) -> None:
    """The override is the operator's own path: no reservation, no suffix, and
    two runs overwrite it exactly as before."""
    repo = _summary_writing_repo(tmp_path)
    env = _default_summary_env(tmp_path, repo)
    override = tmp_path / "logs" / "summary.json"
    env["NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH"] = str(override)

    _run_wrapper(env)
    result = _run_wrapper(env)

    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in (tmp_path / "logs").glob("*.json")) == ["summary.json"]
    assert f"RUNNER_INVOKED summary={override}" in _wrapper_output(tmp_path, result)


def _porcelain(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.mark.parametrize(
    ("variable", "relative", "reason"),
    [
        ("NODE27_MVT_CACHE_RETENTION_SUMMARY_PATH", "summary.json", "SUMMARY_PATH_NOT_ABSOLUTE"),
        ("NODE27_MVT_CACHE_RETENTION_LOG_FILE", "wrapper.log", "LOG_FILE_NOT_ABSOLUTE"),
        ("NODE27_MVT_CACHE_RETENTION_LOCK_PATH", "wrapper.lock", "LOCK_PATH_NOT_ABSOLUTE"),
    ],
)
def test_wrapper_refuses_a_relative_summary_log_or_lock_path(
    tmp_path: Path, variable: str, relative: str, reason: str
) -> None:
    """The wrapper `cd`s into the repository before the runner, and `exec 9>`
    plus the first log line resolve against the caller's cwd before that; a
    relative value would drop a file into the git work tree every tick. The
    subprocess runs FROM the stand-in repo so both resolutions land there, and
    that repo's own `git status --porcelain` is the oracle."""
    repo = _wrapper_repo(tmp_path)
    subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, check=True)
    env_file = tmp_path / "runner.env"
    env_file.write_text("", encoding="utf-8")
    env_file.chmod(0o600)
    bin_dir = _flock_shim(tmp_path, exit_code=0)
    env = _wrapper_env(tmp_path, repo, env_file, bin_dir=bin_dir)
    env[variable] = relative
    before = _porcelain(repo)

    result = _run_wrapper(env, cwd=repo)

    assert result.returncode == 2
    combined = _wrapper_output(tmp_path, result)
    assert f"BLOCKED rc=2 reason={reason}" in combined
    assert "RUNNER_INVOKED" not in combined
    assert _porcelain(repo) == before
    assert not (repo / relative).exists()
    # Refused before the lock is taken and before any log line is written.
    assert not (tmp_path / "wrapper.lock").exists()
    assert not (tmp_path / "logs" / "wrapper.log").exists()


def test_wrapper_never_opens_the_lock_file_with_o_creat_in_the_runner() -> None:
    """Source-level pin for the one flag whose absence a test cannot stage:
    a deleter that can recreate the file it removes is not a deleter."""
    text = (_REPO_ROOT / "scripts/node27_mvt_cache_retention.py").read_text(encoding="utf-8")

    assert "os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK" in text
    assert "os.O_CREAT" not in text
    assert "O_CREAT" not in _code_only(text)


def test_the_runner_never_imports_the_raw_retention_module() -> None:
    """D1: stdlib only, no DB. Importing the sibling would drag in psycopg and
    the watermark read this runner deliberately does not have."""
    text = (_REPO_ROOT / "scripts/node27_mvt_cache_retention.py").read_text(encoding="utf-8")

    tree = ast.parse(text)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not [name for name in imported if name.startswith(("scripts", "packages", "services", "apps"))]
    assert not [name for name in imported if name in {"psycopg2", "psycopg", "sqlalchemy"}]
    assert imported <= {
        "argparse",
        "errno",
        "fcntl",
        "json",
        "os",
        "re",
        "stat",
        "dataclasses",
        "datetime",
        "pathlib",
        "typing",
        "__future__",
    }


def test_collect_targets_reads_exactly_two_levels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resource bound: a fixed-depth enumeration, never a whole-tree walk."""
    root = tmp_path / "cache"
    _mixed_tree(root)
    scanned: list[str] = []
    real_scandir = os.scandir

    def recording_scandir(path: Any = ".") -> Any:
        scanned.append(str(path))
        return real_scandir(path)

    # Scoped for the same reason as the `os.replace` patch above: an unscoped
    # `os.scandir` patch stays installed through teardown, which is exactly the
    # hazard pyproject.toml's tmp_path retention note calls out.
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", recording_scandir)
        runner.collect_targets(root, cutoff=datetime(2026, 8, 25, tzinfo=UTC))

    assert sorted(scanned) == sorted(
        [
            str(root),
            str(root / "ab"),
            str(root / "cd"),
            str(root / ".locks"),
            str(root / ".locks" / "ab"),
        ]
    )
    assert str(root / "precip") not in scanned
    assert str(root / "ab" / "cd") not in scanned


def test_a_fifo_wearing_a_target_name_is_not_a_target(tmp_path: Path) -> None:
    """`lstat` must say REGULAR file, not merely "not a directory"."""
    root = tmp_path / "cache"
    (root / "ab").mkdir(parents=True)
    fifo = root / "ab" / f"{_SHA_A}.pbf"
    os.mkfifo(fifo)
    os.utime(fifo, (_AGED, _AGED))

    targets, _, _ = runner.collect_targets(root, cutoff=datetime(2026, 8, 25, tzinfo=UTC))

    assert targets == []
    assert stat.S_ISFIFO(os.lstat(fifo).st_mode)
