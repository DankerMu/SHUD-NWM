"""#2282 D8: ``scripts/ops/start-display-api.sh`` signals only this checkout's uvicorn.

The script runs against real ``sleep`` children standing in for uvicorn processes.
A fake ``pgrep`` reads a ``pid<TAB>cmdline`` table and applies the script's own ERE
to the cmdline field only (so ``^`` anchors), printing only live, non-zombie pids:
a TERMed child stays a zombie until pytest reaps it, and without that check the
script would fall through to its SIGKILL path. ``kill`` is a bash builtin, so the
signals are real; only this test's own children are ever listed.

The harness helpers are shared with the preflight cases in
``tests/test_two_node_docker_runtime.py``.
"""

from __future__ import annotations

import signal
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.test_two_node_docker_runtime import (
    REPO_ROOT,
    _prepare_start_display_api_harness,
    _run_start_display_api_harness,
    _write_executable,
)

UVICORN_ARGS = "-m uvicorn apps.api.main:app --host 127.0.0.1 --port 8080 --workers 2"

FAKE_PGREP = r"""#!/bin/sh
printf '%s\n' "$@" >>"$HARNESS_RECORD_DIR/pgrep.argv"
pattern=
for arg do
  case "$arg" in
    -f|--) ;;
    *) pattern=$arg ;;
  esac
done
tab=$(printf '\t')
found=1
while IFS="$tab" read -r pid cmdline; do
  [ -n "$pid" ] || continue
  printf '%s\n' "$cmdline" | grep -E -q -e "$pattern" || continue
  kill -0 "$pid" 2>/dev/null || continue
  state=$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')
  case "$state" in
    ''|Z*) continue ;;
  esac
  printf '%s\n' "$pid"
  found=0
done <"$HARNESS_PROCESS_TABLE"
exit $found
"""

FAKE_SYSTEMCTL = r"""#!/bin/sh
printf '%s\n' "$*" >>"$HARNESS_RECORD_DIR/systemctl.argv"
[ "${1:-}" = "--user" ] || exit 64
shift
case "$1" in
  show-environment|daemon-reload|stop|enable|is-active) exit 0 ;;
  show) printf '%s\n' "$HARNESS_SYSTEMD_MAIN_PID"; exit 0 ;;
esac
exit 64
"""


def _display_env(temp_repo: Path) -> list[str]:
    object_store_root = temp_repo / "object-store"
    return [
        "DATABASE_URL=postgresql://nhms_display_ro:secret@db.internal.example:5432/nhms",
        "NHMS_ENABLE_LIVE_POSTGIS_MVT=true",
        f"OBJECT_STORE_ROOT={object_store_root}",
        f"NHMS_DISPLAY_LOG_PATH={temp_repo / 'display-api.log'}",
    ]


def _harness(temp_repo: Path) -> tuple[Path, Path]:
    fake_bin, record_dir = _prepare_start_display_api_harness(
        temp_repo,
        display_env=_display_env(temp_repo),
        object_store_root=temp_repo / "object-store",
    )
    _write_executable(fake_bin / "pgrep", FAKE_PGREP)
    return fake_bin, record_dir


@contextmanager
def _sleepers(count: int) -> Iterator[list[subprocess.Popen[bytes]]]:
    children = [subprocess.Popen(["sleep", "300"]) for _ in range(count)]
    try:
        yield children
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


def _write_process_table(path: Path, rows: list[tuple[subprocess.Popen[bytes], str]]) -> None:
    path.write_text("".join(f"{child.pid}\t{cmdline}\n" for child, cmdline in rows), encoding="utf-8")


def _terminated_by_sigterm(child: subprocess.Popen[bytes]) -> bool:
    return child.wait(timeout=10) == -signal.SIGTERM


def _assert_restart_spared_the_foreign_checkout(
    completed: subprocess.CompletedProcess[str],
    own: subprocess.Popen[bytes],
    foreign: list[subprocess.Popen[bytes]],
    record_dir: Path,
) -> None:
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"stopping prior uvicorn pid(s): {own.pid}" in completed.stdout
    assert "SIGTERM timed out" not in completed.stdout + completed.stderr
    assert all(child.poll() is None for child in foreign), "a foreign checkout's uvicorn was signalled"
    assert _terminated_by_sigterm(own)
    pgrep_args = (record_dir / "pgrep.argv").read_text(encoding="utf-8").splitlines()
    assert pgrep_args[:2] == ["-f", "--"]
    assert pgrep_args[2].startswith("^")


def test_legacy_restart_stops_this_checkouts_uvicorn_and_spares_another_checkout(tmp_path: Path) -> None:
    temp_repo = tmp_path / "repo"
    fake_bin, record_dir = _harness(temp_repo)
    table = tmp_path / "processes.tsv"

    with _sleepers(2) as (own, foreign):
        _write_process_table(
            table,
            [
                (own, f"{temp_repo}/.venv/bin/python {UVICORN_ARGS}"),
                (foreign, f"{tmp_path}/yd-NWM/.venv/bin/python -m uvicorn apps.api.main:app --port 8081"),
            ],
        )
        completed = _run_start_display_api_harness(
            temp_repo, fake_bin, record_dir, extra_env={"HARNESS_PROCESS_TABLE": str(table)}
        )

        _assert_restart_spared_the_foreign_checkout(completed, own, [foreign], record_dir)
        assert (record_dir / "setsid.argv").is_file()
        assert "detached fallback relaunched" in completed.stdout


def test_systemd_restart_stops_the_unit_then_sweeps_only_this_checkouts_orphans(tmp_path: Path) -> None:
    temp_repo = tmp_path / "repo"
    fake_bin, record_dir = _harness(temp_repo)
    _write_executable(fake_bin / "systemctl", FAKE_SYSTEMCTL)
    unit_source = temp_repo / "infra" / "systemd" / "nhms-display-api.service"
    unit_source.parent.mkdir(parents=True)
    unit_source.write_text((REPO_ROOT / "infra/systemd/nhms-display-api.service").read_text(encoding="utf-8"))
    config_home = tmp_path / "xdg-config"
    table = tmp_path / "processes.tsv"

    with _sleepers(2) as (own, foreign):
        _write_process_table(
            table,
            [
                (own, f"{temp_repo}/.venv/bin/python {UVICORN_ARGS}"),
                (foreign, f"{tmp_path}/yd-NWM/.venv/bin/python -m uvicorn apps.api.main:app --port 8081"),
            ],
        )
        completed = _run_start_display_api_harness(
            temp_repo,
            fake_bin,
            record_dir,
            extra_env={
                "HARNESS_PROCESS_TABLE": str(table),
                "HARNESS_SYSTEMD_MAIN_PID": "424242",
                "XDG_CONFIG_HOME": str(config_home),
            },
        )

        _assert_restart_spared_the_foreign_checkout(completed, own, [foreign], record_dir)
        calls = (record_dir / "systemctl.argv").read_text(encoding="utf-8").splitlines()
        assert calls.index("--user stop nhms-display-api.service") < calls.index(
            "--user enable --now nhms-display-api.service"
        )
        installed = config_home / "systemd" / "user" / "nhms-display-api.service"
        assert installed.read_text(encoding="utf-8") == unit_source.read_text(encoding="utf-8")
        assert not (record_dir / "setsid.argv").exists()
        assert "systemd relaunched main_pid=424242" in completed.stdout


def test_a_metacharacter_repo_root_matches_only_its_own_checkout(tmp_path: Path) -> None:
    parent = tmp_path / "checkouts"
    temp_repo = parent / "a+b.c"
    fake_bin, record_dir = _harness(temp_repo)
    table = tmp_path / "processes.tsv"

    with _sleepers(3) as (own, sibling, foreign):
        _write_process_table(
            table,
            [
                (own, f"{temp_repo}/.venv/bin/python {UVICORN_ARGS}"),
                # Matches an unescaped `a+b.c` (`a+` = "aa", `.` = "_"), never the escaped root.
                (sibling, f"{parent}/aab_c/.venv/bin/python {UVICORN_ARGS}"),
                (foreign, f"{tmp_path}/yd-NWM/.venv/bin/python -m uvicorn apps.api.main:app --port 8081"),
            ],
        )
        completed = _run_start_display_api_harness(
            temp_repo, fake_bin, record_dir, extra_env={"HARNESS_PROCESS_TABLE": str(table)}
        )

        _assert_restart_spared_the_foreign_checkout(completed, own, [sibling, foreign], record_dir)
        pattern = (record_dir / "pgrep.argv").read_text(encoding="utf-8").splitlines()[2]
        assert pattern == f"^{parent}/a\\+b\\.c/\\.venv/bin/python -m uvicorn apps\\.api\\.main:app"


def test_no_prior_uvicorn_of_this_checkout_leaves_every_other_process_alone(tmp_path: Path) -> None:
    temp_repo = tmp_path / "repo"
    fake_bin, record_dir = _harness(temp_repo)
    table = tmp_path / "processes.tsv"

    with _sleepers(1) as (foreign,):
        _write_process_table(
            table, [(foreign, f"{tmp_path}/yd-NWM/.venv/bin/python -m uvicorn apps.api.main:app --port 8081")]
        )
        completed = _run_start_display_api_harness(
            temp_repo, fake_bin, record_dir, extra_env={"HARNESS_PROCESS_TABLE": str(table)}
        )

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "no prior uvicorn process found" in completed.stdout
        assert foreign.poll() is None


@pytest.mark.parametrize("path_fragment", ["[x]", "(x)", "x|y", "x{1}", "x?", "x^y", "x$y", "x*"])
def test_the_repo_root_escape_covers_every_ere_metacharacter(tmp_path: Path, path_fragment: str) -> None:
    """The escaped root, used as an ERE, matches exactly its own literal spelling."""
    root = f"/srv/{path_fragment}/NWM"
    script = (REPO_ROOT / "scripts/ops/start-display-api.sh").read_text(encoding="utf-8")
    escape_line = next(line for line in script.splitlines() if line.startswith("REPO_ROOT_RE="))
    escaped = subprocess.run(
        ["bash", "-c", f'REPO_ROOT="$1"; {escape_line}; printf "%s" "$REPO_ROOT_RE"', "escape", root],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    candidates = tmp_path / "candidates.txt"
    decoy = root.replace(path_fragment, "x")
    candidates.write_text(f"{root}/.venv\n{decoy}/.venv\n", encoding="utf-8")

    matched = subprocess.run(
        ["grep", "-E", "-e", f"^{escaped}/", str(candidates)], check=False, capture_output=True, text=True
    ).stdout.splitlines()

    assert matched == [f"{root}/.venv"]
