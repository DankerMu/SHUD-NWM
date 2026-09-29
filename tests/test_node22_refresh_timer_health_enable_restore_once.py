"""#2640: a failed `--enable` restores the probe timer exactly once, in the main shell.

`set -E` hands the ERR trap to command substitutions, and a real user manager
exits 3 from `is-active` for anything but `active`. The master-era read
`[[ "$(systemctl --user is-active "$timer")" == active ]]` therefore fired
`enable_failure_restore` twice: once inside the substitution's subshell, before
the main shell knew anything had failed, and once in the main shell. The fake
systemctl's ``NHMS_FAKE_IS_ACTIVE_STRICT_RC`` knob reproduces the exit-3.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.node22_refresh_timer_health_helpers import (
    INSTALLER,
    _installer_fake_systemctl,
    _probe_timer_state,
    _run_installer,
)

PROBE_TIMER = "nhms-node22-refresh-timer-health.timer"
PROTECTED_QUERIES = (
    "--user is-enabled nhms-compute-scheduler.timer",
    "--user is-active nhms-compute-scheduler.timer",
    "--user is-enabled nhms-scheduler-file-provider-refresh.timer",
    "--user is-active nhms-scheduler-file-provider-refresh.timer",
    "--user is-enabled nhms-compute-scheduler.service",
    "--user is-enabled nhms-scheduler-file-provider-refresh.service",
)
MASTER_READ = '  [[ "$($systemctl_bin --user is-active "$timer")" == active ]]\n'


def _enable_with_a_timer_that_stays_inactive(
    tmp_path: Path, installer: Path = INSTALLER
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    installed, install_log = _run_installer(tmp_path, "--install")
    assert installed.returncode == 0, installed.stderr
    install_log.write_text("")  # the fake appends; read only the --enable invocation
    completed, log = _run_installer(
        tmp_path,
        "--enable",
        timer_answers={"is-active": "inactive"},
        strict_is_active_rc=True,
        installer=installer,
    )
    return completed, log.read_text().splitlines()


def _after_enable_now(lines: list[str]) -> list[str]:
    armed = [index for index, line in enumerate(lines) if line.startswith("--user enable --now")]
    assert armed, f"the installer never reached `enable --now`: {lines}"
    return lines[armed[0] + 1 :]


def _restores(lines: list[str]) -> list[str]:
    return [
        line
        for line in lines
        if (line.startswith("--user disable") or line.startswith("--user stop")) and PROBE_TIMER in line
    ]


def test_the_fake_reports_a_non_active_timer_with_exit_3(tmp_path: Path) -> None:
    script, _log = _installer_fake_systemctl(tmp_path)
    environment = dict(os.environ, NHMS_FAKE_IS_ACTIVE_STRICT_RC="1", NHMS_FAKE_PROBE_TIMER_IS_ACTIVE="inactive")

    def is_active(unit: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(script), "--user", "is-active", unit], env=environment, capture_output=True, text=True, check=False
        )

    probe = is_active(PROBE_TIMER)
    protected = is_active("nhms-compute-scheduler.timer")

    assert (probe.returncode, probe.stdout) == (3, "inactive\n")
    assert (protected.returncode, protected.stdout) == (0, "active\n")


def test_a_probe_timer_that_stays_inactive_is_restored_exactly_once(tmp_path: Path) -> None:
    completed, lines = _enable_with_a_timer_that_stays_inactive(tmp_path)
    after = _after_enable_now(lines)

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert _restores(after) == [f"--user disable {PROBE_TIMER}", f"--user stop {PROBE_TIMER}"], after
    for query in PROTECTED_QUERIES:
        assert after.count(query) == 1, (query, after)
    assert _probe_timer_state(tmp_path) == ("disabled", "inactive")


def test_the_main_shell_guard_keeps_a_substitution_failure_from_restoring(tmp_path: Path) -> None:
    """The guard on its own: the master-era read put back into today's script."""
    source = INSTALLER.read_text(encoding="utf-8")
    captured = source[source.index("  probe_active=$(") : source.index('  [[ "$probe_active" == active ]]\n')]
    unguarded_read = source.replace(captured, "").replace('  [[ "$probe_active" == active ]]\n', MASTER_READ)
    assert MASTER_READ in unguarded_read
    installer = tmp_path / "installer-with-the-master-read.sh"
    installer.write_text(unguarded_read, encoding="utf-8")

    completed, lines = _enable_with_a_timer_that_stays_inactive(tmp_path, installer)
    after = _after_enable_now(lines)

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert _restores(after) == [f"--user disable {PROBE_TIMER}", f"--user stop {PROBE_TIMER}"], after
    for query in PROTECTED_QUERIES:
        assert after.count(query) == 1, (query, after)
