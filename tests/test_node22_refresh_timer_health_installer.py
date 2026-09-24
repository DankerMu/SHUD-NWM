"""Node-22 refresh-timer probe: the installer and the probe's unit files.

Partition (#2532 partition of the 3456-line / 202-case
tests/test_node22_refresh_timer_health.py).
R15: the installer never mutates the four protected
units, lands and rolls back the probe units byte-identical, and its
protected-unit assertion and read-back accept sets actually bite per unit type
(C2). Runs ``scripts/install_node22_refresh_timer_health.sh`` as a subprocess
against a fake ``systemctl`` and ``read_text``s both probe units and the probe
runbook section.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.node22_refresh_timer_health_helpers import (
    INSTALLER,
    PROBE_UNITS,
    PROTECTED_UNITS,
    _probe_runbook_section,
    _probe_timer_state,
    _run_installer,
)

# ---------------------------------------------------------------------------
# R15 -- the installer never touches the four protected units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["--install", "--enable", "--rollback"])
def test_r15_installer_never_mutates_a_protected_unit(tmp_path: Path, action: str) -> None:
    if action != "--install":
        _run_installer(tmp_path, "--install")
    completed, log = _run_installer(tmp_path, action)

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["protected_unchanged"] is True
    mutating = []
    for line in log.read_text().splitlines():
        argv = line.split()
        if len(argv) >= 2 and argv[1] in {
            "enable",
            "disable",
            "start",
            "stop",
            "restart",
        }:
            mutating.append(line)
    for line in mutating:
        assert not any(unit in line for unit in PROTECTED_UNITS), line
        assert any(unit in line for unit in PROBE_UNITS), line
    # The protected units are only ever read.
    for line in log.read_text().splitlines():
        if any(unit in line for unit in PROTECTED_UNITS):
            assert line.split()[1] in {"is-enabled", "is-active"}, line


def test_r15_install_lands_the_units_byte_identical_and_rollback_removes_them(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).resolve().parents[1]
    completed, _log = _run_installer(tmp_path, "--install")

    assert completed.returncode == 0, completed.stderr
    for unit in PROBE_UNITS:
        assert (tmp_path / "units" / unit).read_bytes() == (
            repo / "infra" / "systemd" / unit
        ).read_bytes()
    assert (tmp_path / "receipts").stat().st_mode & 0o777 == 0o700

    completed, _log = _run_installer(tmp_path, "--rollback")

    assert completed.returncode == 0, completed.stderr
    for unit in PROBE_UNITS:
        assert not (tmp_path / "units" / unit).exists()


def test_r15_installer_rejects_an_unknown_action(tmp_path: Path) -> None:
    completed, _log = _run_installer(tmp_path, "--arm-everything")

    assert completed.returncode == 2


# ---------------------------------------------------------------------------
# Unit files
# ---------------------------------------------------------------------------


def test_probe_units_declare_the_documented_shape() -> None:
    systemd_root = Path(__file__).resolve().parents[1] / "infra" / "systemd"
    service = (systemd_root / "nhms-node22-refresh-timer-health.service").read_text()
    timer = (systemd_root / "nhms-node22-refresh-timer-health.timer").read_text()
    refresh_service = (
        systemd_root / "nhms-scheduler-file-provider-refresh.service"
    ).read_text()

    assert "Type=oneshot" in service
    assert "TimeoutStartSec=" in service
    # The directive itself must be absent (the unit's comment explains why).
    assert not any(
        line.strip().startswith("PrivateTmp") for line in service.splitlines()
    )
    assert "scripts/node22_refresh_timer_health.py" in service
    # Same DB selector clearing the refresh service uses -- node-22 is
    # permanently DB-free and the probe must not inherit a libpq selector.
    probe_unset = next(
        line for line in service.splitlines() if line.startswith("UnsetEnvironment=")
    )
    refresh_unset = next(
        line for line in refresh_service.splitlines() if line.startswith("UnsetEnvironment=")
    )
    assert probe_unset == refresh_unset
    assert "OnCalendar=hourly" in timer
    assert "Persistent=true" in timer
    assert "Unit=nhms-node22-refresh-timer-health.service" in timer


# ---------------------------------------------------------------------------
# R15 (C2) -- the protected-unit assertion actually bites, per unit TYPE
# ---------------------------------------------------------------------------

PROBE_TIMER = "nhms-node22-refresh-timer-health.timer"


@pytest.mark.parametrize(
    "timer_unit",
    ["nhms-compute-scheduler.timer", "nhms-scheduler-file-provider-refresh.timer"],
)
@pytest.mark.parametrize(
    ("query", "divergent"),
    [("is-enabled", "disabled"), ("is-active", "inactive")],
)
def test_r15_a_divergent_timer_read_aborts_the_install_and_the_trap_backs_it_out(
    tmp_path: Path, timer_unit: str, query: str, divergent: str
) -> None:
    """For the two TIMERS both fields are compared, and the assertion must
    actually bite: the run aborts AND the ERR trap removes the probe units it
    had just laid down.  Asserting the trap's side effect, not the exit status:
    `set -e` alone exits non-zero without ever running the trap.
    """
    completed, _log = _run_installer(
        tmp_path, "--install", diverge=f"{timer_unit}.{query}", diverge_value=divergent
    )

    assert completed.returncode != 0
    for unit in PROBE_UNITS:
        assert not (tmp_path / "units" / unit).exists(), (
            f"the ERR trap never ran: {unit} was left installed"
        )


def test_r15_a_divergent_oneshot_unit_file_state_still_aborts_the_install(
    tmp_path: Path,
) -> None:
    """`UnitFileState` IS compared for the oneshot services -- it is what
    "unchanged" means for a unit whose activity is driven by its timer."""
    completed, _log = _run_installer(
        tmp_path,
        "--install",
        diverge="nhms-compute-scheduler.service.is-enabled",
        diverge_value="disabled",
    )

    assert completed.returncode != 0
    for unit in PROBE_UNITS:
        assert not (tmp_path / "units" / unit).exists()


@pytest.mark.parametrize(
    "oneshot",
    ["nhms-compute-scheduler.service", "nhms-scheduler-file-provider-refresh.service"],
)
def test_r15_a_oneshot_flipping_active_mid_run_does_not_fire_the_assertion(
    tmp_path: Path, oneshot: str
) -> None:
    """The paired negative case.

    A timer-driven oneshot's `is-active` legitimately flips on its own cadence
    -- the compute scheduler every 5 minutes, the refresh service inside its
    02:15-04:15Z window.  Comparing it would make the assertion fire on a unit
    nobody touched, and an installer that aborts and rolls back on that false
    positive turns arming into a retry loop.
    """
    completed, _log = _run_installer(
        tmp_path, "--install", diverge=f"{oneshot}.is-active", diverge_value="inactive"
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["protected_unchanged"] is True
    for unit in PROBE_UNITS:
        assert (tmp_path / "units" / unit).exists()


def test_r15_enable_restores_the_probe_timer_when_the_assertion_fires(
    tmp_path: Path,
) -> None:
    """C1(a): `--enable` arms an ERR trap BEFORE `enable --now`, mirroring the
    sibling installer's `enable_failure_restore`.  Without it a failed
    post-arming assertion leaves the probe timer armed with an exit code of 1.
    """
    installed, _log = _run_installer(tmp_path, "--install")
    assert installed.returncode == 0, installed.stderr
    assert _probe_timer_state(tmp_path) == ("disabled", "inactive")

    completed, log = _run_installer(
        tmp_path,
        "--enable",
        diverge="nhms-scheduler-file-provider-refresh.timer.is-active",
        diverge_value="inactive",
    )
    lines = log.read_text().splitlines()
    armed = [index for index, line in enumerate(lines) if line.startswith("--user enable --now")]

    assert completed.returncode != 0
    assert armed, "the installer never reached `enable --now`"
    assert any(
        line.startswith("--user disable") and PROBE_TIMER in line
        for line in lines[armed[0] + 1 :]
    ), "the ERR trap never restored the probe timer's pre-invocation state"
    assert _probe_timer_state(tmp_path) == ("disabled", "inactive")


def test_r15_enable_leaves_the_probe_timer_armed_on_the_happy_path(
    tmp_path: Path,
) -> None:
    installed, _log = _run_installer(tmp_path, "--install")
    assert installed.returncode == 0, installed.stderr

    completed, _log = _run_installer(tmp_path, "--enable")

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "enabled_active"
    assert _probe_timer_state(tmp_path) == ("enabled", "active")


def test_r15_the_probe_installer_runs_with_errtrace() -> None:
    """`-E` is what lets the top-level ERR traps see an assertion failing inside
    a function body; without it the trap never runs."""
    assert "set -Eeuo pipefail" in INSTALLER.read_text()


PROTECTED_QUERIES = (
    "--user is-enabled nhms-compute-scheduler.timer",
    "--user is-active nhms-compute-scheduler.timer",
    "--user is-enabled nhms-scheduler-file-provider-refresh.timer",
    "--user is-active nhms-scheduler-file-provider-refresh.timer",
    "--user is-enabled nhms-compute-scheduler.service",
    "--user is-enabled nhms-scheduler-file-provider-refresh.service",
)


@pytest.mark.parametrize(
    ("diverge", "divergent"),
    [
        ("nhms-scheduler-file-provider-refresh.timer.is-active", "inactive"),
        ("nhms-compute-scheduler.service.is-enabled", "disabled"),
    ],
)
def test_r15_rollback_does_not_report_success_when_a_protected_unit_moved(
    tmp_path: Path, diverge: str, divergent: str
) -> None:
    """`--rollback` has no trap: its protected-unit assertion is the only thing
    between "a protected unit moved under the rollback" and a printed
    `{"status":"rolled_back","protected_unchanged":true}`.

    The probe units are removed either way (`remove_probe_units` runs first),
    so that is not evidence.  The evidence is the success document: it must not
    be emitted, and the divergent unit must have been re-read AFTER the removal.
    """
    installed, install_log = _run_installer(tmp_path, "--install")
    assert installed.returncode == 0, installed.stderr
    install_log.write_text("")  # the fake appends; read only this invocation

    completed, log = _run_installer(
        tmp_path, "--rollback", diverge=diverge, diverge_value=divergent
    )
    lines = log.read_text().splitlines()
    unit, query = diverge.rsplit(".", 1)
    reads = [index for index, line in enumerate(lines) if line == f"--user {query} {unit}"]
    reloads = [index for index, line in enumerate(lines) if line == "--user daemon-reload"]

    assert completed.stdout == "", (
        f"--rollback reported success over a moved protected unit: {completed.stdout}"
    )
    assert completed.returncode != 0
    assert len(reads) == 2, lines
    assert reloads and reads[1] > reloads[-1], (
        f"the protected units were not re-read after the rollback acted: {lines}"
    )
    for probe_unit in PROBE_UNITS:
        assert not (tmp_path / "units" / probe_unit).exists()


def test_rollback_refuses_success_when_systemctl_refused_to_disarm_the_probe(
    tmp_path: Path,
) -> None:
    """`remove_probe_units` guards `disable --now` with `|| true` -- it is also the
    `--install` ERR trap body -- so a refused disarm used to fall straight
    through to `rolled_back`, exit 0, with the probe timer still armed.

    The outcome is what is asserted: the timer really is still enabled and
    active afterwards, and `--rollback` does not claim otherwise.
    """
    assert _run_installer(tmp_path, "--install")[0].returncode == 0
    enabled, _log = _run_installer(tmp_path, "--enable")
    assert enabled.returncode == 0, enabled.stderr
    assert _probe_timer_state(tmp_path) == ("enabled", "active")

    completed, _log = _run_installer(tmp_path, "--rollback", fail_verb="disable")

    assert _probe_timer_state(tmp_path) == ("enabled", "active")
    assert "rolled_back" not in completed.stdout, completed.stdout
    assert completed.returncode != 0
    assert "still enabled" in completed.stderr


def test_rollback_of_an_armed_probe_succeeds_when_systemctl_complies(
    tmp_path: Path,
) -> None:
    """The paired positive case: the read-back must not refuse a real disarm."""
    assert _run_installer(tmp_path, "--install")[0].returncode == 0
    assert _run_installer(tmp_path, "--enable")[0].returncode == 0

    completed, _log = _run_installer(tmp_path, "--rollback")

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"status": "rolled_back", "protected_unchanged": True}
    assert _probe_timer_state(tmp_path) == ("disabled", "inactive")
    for probe_unit in PROBE_UNITS:
        assert not (tmp_path / "units" / probe_unit).exists()


# The read-back's closed accept sets (`--install` and `--rollback`).
# `test_the_read_back_accept_sets_are_the_installers_own_case_arms` reads them
# against the installer's `case` arms, and
# `test_the_runbook_states_exactly_the_rollback_accept_sets` against the
# runbook table; the parametrized tests below exercise them by behaviour.
# `""` is "no stdout answer".
ROLLBACK_ACCEPTED_IS_ENABLED = ("disabled", "static", "not-found", "")
ROLLBACK_ACCEPTED_IS_ACTIVE = ("inactive", "failed")
ROLLBACK_REFUSED_IS_ENABLED = ("enabled", "enabled-runtime", "linked", "masked", "alias")
ROLLBACK_REFUSED_IS_ACTIVE = ("active", "activating", "reloading", "deactivating", "")


@pytest.mark.parametrize("is_enabled", ROLLBACK_ACCEPTED_IS_ENABLED)
@pytest.mark.parametrize("is_active", ROLLBACK_ACCEPTED_IS_ACTIVE)
def test_rollback_accepts_every_stopped_probe_state(
    tmp_path: Path, is_enabled: str, is_active: str
) -> None:
    assert _run_installer(tmp_path, "--install")[0].returncode == 0

    completed, _log = _run_installer(
        tmp_path,
        "--rollback",
        probe_answers={"is-enabled": is_enabled, "is-active": is_active},
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "rolled_back"


@pytest.mark.parametrize(
    ("is_enabled", "is_active"),
    [(value, "inactive") for value in ROLLBACK_REFUSED_IS_ENABLED]
    + [("disabled", value) for value in ROLLBACK_REFUSED_IS_ACTIVE],
)
def test_rollback_refuses_every_state_that_can_still_fire(
    tmp_path: Path, is_enabled: str, is_active: str
) -> None:
    assert _run_installer(tmp_path, "--install")[0].returncode == 0

    completed, _log = _run_installer(
        tmp_path,
        "--rollback",
        probe_answers={"is-enabled": is_enabled, "is-active": is_active},
    )

    assert "rolled_back" not in completed.stdout, completed.stdout
    assert completed.returncode != 0


def test_the_runbook_states_exactly_the_rollback_accept_sets() -> None:
    """Prose<->code: the runbook's `--rollback` table must list exactly the
    states the installer accepts -- set equality, so a state added to or
    dropped from either side reds here.  `空` is the runbook's word for `""`."""
    section = _probe_runbook_section()

    def accepted_in_row(query: str) -> set[str]:
        prefix = f"| `{query}` |"
        rows = [line for line in section.splitlines() if line.startswith(prefix)]
        assert len(rows) == 1, f"expected one `{query}` row in the rollback table: {rows}"
        cell = rows[0][len(prefix) :].split("|", 1)[0]
        states = set(re.findall(r"`([^`]+)`", cell))
        if "空" in cell:
            states.add("")
        return states

    assert accepted_in_row("is-enabled") == set(ROLLBACK_ACCEPTED_IS_ENABLED)
    assert accepted_in_row("is-active") == set(ROLLBACK_ACCEPTED_IS_ACTIVE)
    assert "--rollback" in section and "rolled_back" in section

    # Both actions that print a "disarmed" status read back first, in the
    # installer, and the runbook names the read-back on both.
    source = INSTALLER.read_text()
    branches = {
        "--install": source[source.index('if [[ "$action" == --install ]]') : source.index("elif")],
        "--rollback": source[source.rindex("\nelse\n") : source.rindex("\nfi\n")],
    }
    for action, status in READ_BACK_ACTIONS:
        branch = branches[action]
        printed = branch.index(f'{{"status":"{status}"')
        assert "assert_probe_units_gone" in branch[:printed], action
        assert f'`{{"status":"{status}",...}}`' in section
        (command,) = [
            line
            for line in section.splitlines()
            if line.startswith(f"scripts/install_node22_refresh_timer_health.sh {action} ")
        ]
        assert "读回" in command, command


def test_the_read_back_accept_sets_are_the_installers_own_case_arms() -> None:
    """Code<->test: across EVERY arm of each `case` in `assert_probe_units_gone`,
    the non-`*)` patterns are exactly the accept tuple, and each `case` has
    exactly one `*)` arm, which is where every other state goes."""
    body = re.search(
        r"^assert_probe_units_gone\(\) \{\n(.*?)^\}\n", INSTALLER.read_text(), re.S | re.M
    )
    assert body is not None

    def accepted_arm(variable: str) -> set[str]:
        blocks = re.findall(rf'case "\${variable}" in\n(.*?)\n\s*esac\n', body.group(1), re.S)
        assert len(blocks) == 1, blocks
        # An arm starts on a line whose pattern list is closed by `)`; the
        # body lines (`printf`, `return 1`, `;;`) never match this shape.
        patterns = re.findall(r"^\s*([^\s()][^()\n]*)\)", blocks[0], re.M)
        defaults = [pattern for pattern in patterns if pattern.strip() == "*"]
        assert len(defaults) == 1, patterns
        return {
            "" if state.strip() == "''" else state.strip()
            for pattern in patterns
            if pattern.strip() != "*"
            for state in pattern.split("|")
        }

    assert accepted_arm("enabled") == set(ROLLBACK_ACCEPTED_IS_ENABLED)
    assert accepted_arm("active") == set(ROLLBACK_ACCEPTED_IS_ACTIVE)
    assert not set(ROLLBACK_REFUSED_IS_ENABLED) & accepted_arm("enabled")
    assert not set(ROLLBACK_REFUSED_IS_ACTIVE) & accepted_arm("active")


READ_BACK_ACTIONS = [("--install", "installed_stopped"), ("--rollback", "rolled_back")]
DISARMED_TIMER = {"is-enabled": "disabled", "is-active": "inactive"}


@pytest.mark.parametrize(("action", "status"), READ_BACK_ACTIONS)
@pytest.mark.parametrize(
    ("timer_answers", "service_answers"),
    [
        pytest.param(
            DISARMED_TIMER, {"is-enabled": "static", "is-active": "activating"}, id="service-activating"
        ),
        pytest.param(
            DISARMED_TIMER, {"is-enabled": "static", "is-active": "active"}, id="service-active"
        ),
        pytest.param(
            {"is-enabled": "enabled", "is-active": "active"},
            {"is-enabled": "static", "is-active": "inactive"},
            id="timer-still-armed",
        ),
    ],
)
def test_the_read_back_judges_each_probe_unit_on_its_own(
    tmp_path: Path,
    action: str,
    status: str,
    timer_answers: dict[str, str],
    service_answers: dict[str, str],
) -> None:
    """R15c: a disarmed timer does not vouch for a service that is still
    running, and a stopped service does not vouch for an armed timer."""
    if action == "--rollback":
        assert _run_installer(tmp_path, "--install")[0].returncode == 0

    completed, _log = _run_installer(
        tmp_path, action, timer_answers=timer_answers, service_answers=service_answers
    )

    assert status not in completed.stdout, completed.stdout
    assert completed.stdout == ""
    assert completed.returncode != 0


@pytest.mark.parametrize(("action", "status"), READ_BACK_ACTIONS)
@pytest.mark.parametrize("service_active", ["inactive", "failed"])
def test_the_read_back_accepts_a_disarmed_timer_beside_a_stopped_service(
    tmp_path: Path, action: str, status: str, service_active: str
) -> None:
    """The paired positive case for the per-unit read-back."""
    if action == "--rollback":
        assert _run_installer(tmp_path, "--install")[0].returncode == 0

    completed, _log = _run_installer(
        tmp_path,
        action,
        timer_answers=DISARMED_TIMER,
        service_answers={"is-enabled": "static", "is-active": service_active},
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"status": status, "protected_unchanged": True}


def test_install_refuses_installed_stopped_when_systemctl_refused_to_disarm_the_probe(
    tmp_path: Path,
) -> None:
    """R15c on the `--install` main path, whose `disable --now` is `|| true`.

    A re-install over an armed probe whose disarm systemctl refuses used to
    print `installed_stopped` with the timer still armed.  Now the read-back
    refuses inside the ERR trap window, so the trap backs the install out: no
    status line, non-zero, the timer's arm state is what it was before this
    invocation, and the unit files are byte-equal to what preceded it (an
    operator-local edit included, so "restored" is not "reinstalled").
    """
    assert _run_installer(tmp_path, "--install")[0].returncode == 0
    assert _run_installer(tmp_path, "--enable")[0].returncode == 0
    assert _probe_timer_state(tmp_path) == ("enabled", "active")
    timer_file = tmp_path / "units" / PROBE_TIMER
    timer_file.write_text(timer_file.read_text() + "# operator-local edit\n")
    before = {unit: (tmp_path / "units" / unit).read_bytes() for unit in PROBE_UNITS}

    completed, _log = _run_installer(tmp_path, "--install", fail_verb="disable")

    assert completed.stdout == "", completed.stdout
    assert completed.returncode != 0
    assert "still enabled" in completed.stderr
    assert _probe_timer_state(tmp_path) == ("enabled", "active")
    assert {unit: (tmp_path / "units" / unit).read_bytes() for unit in PROBE_UNITS} == before


def test_rollback_after_two_installs_keeps_the_first_installs_disarmed_files(
    tmp_path: Path,
) -> None:
    """Disarmed means inert, not file-absent (R15c): `--rollback` restores the
    unit files that preceded the LAST `--install`, so after two installs the
    probe's own files remain -- disarmed, and never re-armed by the rollback."""
    for _ in range(2):
        assert _run_installer(tmp_path, "--install")[0].returncode == 0
    enabled, enable_log = _run_installer(tmp_path, "--enable")
    assert enabled.returncode == 0, enabled.stderr
    enable_log.write_text("")  # the fake appends; read only the rollback

    completed, log = _run_installer(tmp_path, "--rollback")

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["status"] == "rolled_back"
    repo_units = Path(__file__).resolve().parents[1] / "infra" / "systemd"
    for unit in PROBE_UNITS:
        assert (tmp_path / "units" / unit).read_bytes() == (repo_units / unit).read_bytes()
    assert _probe_timer_state(tmp_path) == ("disabled", "inactive")
    rearming = [
        line
        for line in log.read_text().splitlines()
        if line.startswith(("--user enable", "--user start"))
    ]
    assert rearming == []


def test_r15_without_errtrace_the_install_trap_never_runs(tmp_path: Path) -> None:
    """`-E` is behavioural, not decoration.  The same divergent protected read
    through the real installer and through a copy with `-E` dropped: the real
    ERR trap removes the probe units, the copy's never runs and leaves them."""
    source = INSTALLER.read_text()
    assert source.count("set -Eeuo pipefail\n") == 1
    # The installer header and the runbook both cite this test.
    assert "test_r15_without_errtrace_the_install_trap_never_runs" in source
    assert "去掉 `-E` 的副本实跑" in _probe_runbook_section()
    without_errtrace = tmp_path / "installer-without-errtrace.sh"
    without_errtrace.write_text(source.replace("set -Eeuo pipefail\n", "set -euo pipefail\n"))
    real_root, copy_root = tmp_path / "real", tmp_path / "copy"
    real_root.mkdir()
    copy_root.mkdir()
    divergence = {"diverge": "nhms-compute-scheduler.timer.is-enabled", "diverge_value": "disabled"}

    real, _log = _run_installer(real_root, "--install", **divergence)
    copy, _log = _run_installer(copy_root, "--install", installer=without_errtrace, **divergence)

    assert real.returncode != 0 and copy.returncode != 0
    assert real.stdout == copy.stdout == ""
    for unit in PROBE_UNITS:
        assert not (real_root / "units" / unit).exists(), f"the real trap left {unit}"
        assert (copy_root / "units" / unit).exists(), f"without -E the trap still removed {unit}"


# A baseline in a shape this installer never writes: every protected unit on
# `unit<TAB>enabled<TAB>active`, i.e. the oneshots compared on `is-active` too.
# Timers match the fake's live answer; the oneshot rows differ only in SHAPE.
STALE_PROTECTED_BASELINE = "".join(
    f"{unit}\tenabled\tactive\n" for unit in PROTECTED_UNITS
)


@pytest.mark.parametrize(
    ("action", "status"),
    [
        ("--install", "installed_stopped"),
        ("--enable", "enabled_active"),
        ("--rollback", "rolled_back"),
    ],
)
def test_r15b_a_stale_differently_shaped_baseline_does_not_break_any_action(
    tmp_path: Path, action: str, status: str
) -> None:
    """`protected.before` is captured at the start of EVERY invocation, so the
    protected-unit assertion always means "this invocation changed nothing" and
    no on-disk file is ever a contract between two runs or two installer
    versions.  A file left by an earlier run must be overwritten, not read.
    """
    if action != "--install":
        assert _run_installer(tmp_path, "--install")[0].returncode == 0
    state_root = tmp_path / "install-state"
    state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    baseline = state_root / "protected.before"
    baseline.write_text(STALE_PROTECTED_BASELINE)

    completed, _log = _run_installer(tmp_path, action)

    assert completed.returncode == 0, (
        f"{action} failed on a stale baseline alone: {completed.stderr}"
    )
    assert json.loads(completed.stdout) == {"status": status, "protected_unchanged": True}
    assert baseline.read_text() != STALE_PROTECTED_BASELINE


def test_r15_the_install_trap_rechecks_the_protected_units_after_removing_the_probe(
    tmp_path: Path,
) -> None:
    """The `--install` ERR trap is `remove_probe_units; assert_protected_unchanged`:
    backing the install out mutates units too, so it needs its own check.

    Triggered by a failing `daemon-reload`, before the main protected-unit
    assertion, so the trap's assertion is the ONLY protected re-read.  The probe
    units are removed and the exit is non-zero whether or not that assertion
    runs, so the evidence is the trace: every protected query is re-issued after
    the trap's own `daemon-reload`.
    """
    completed, log = _run_installer(
        tmp_path,
        "--install",
        diverge="nhms-compute-scheduler.service.is-enabled",
        diverge_value="disabled",
        fail_verb="daemon-reload",
    )
    lines = log.read_text().splitlines()
    reloads = [index for index, line in enumerate(lines) if line == "--user daemon-reload"]

    assert completed.stdout == ""
    assert len(reloads) == 2, f"expected the failing reload and the trap's reload: {lines}"
    after_trap_reload = lines[reloads[-1] + 1 :]
    for query in PROTECTED_QUERIES:
        assert query in after_trap_reload, (
            f"the ERR trap removed the probe units but never re-checked `{query}`: "
            f"{after_trap_reload}"
        )
        assert lines[: reloads[0]].count(query) == 1, lines
    for probe_unit in PROBE_UNITS:
        assert not (tmp_path / "units" / probe_unit).exists(), f"{probe_unit} was left installed"


def test_r15_the_enable_trap_rechecks_the_protected_units_after_restoring_the_probe(
    tmp_path: Path,
) -> None:
    """`enable_failure_restore` restores the probe timer and THEN asserts the
    protected units -- the restore mutates units too, so it needs its own check.

    Triggered by a failing `enable --now`, not by the main protected-unit
    assertion: on this path the trap's assertion is the ONLY protected-unit
    re-read the invocation makes, and the divergence it must see is the second
    read of a protected unit.  Exit status and stdout are identical
    whether or not the trap's assertion runs (the shell exits non-zero on the
    original failure either way), so the evidence is the systemctl trace: every
    protected query is re-issued after the restoring `stop`.
    """
    installed, install_log = _run_installer(tmp_path, "--install")
    assert installed.returncode == 0, installed.stderr
    install_log.write_text("")  # the fake appends; read only this invocation

    completed, log = _run_installer(
        tmp_path,
        "--enable",
        diverge="nhms-compute-scheduler.timer.is-enabled",
        diverge_value="disabled",
        fail_verb="enable",
    )
    lines = log.read_text().splitlines()
    armed = [index for index, line in enumerate(lines) if line.startswith("--user enable --now")]
    restored = [
        index
        for index, line in enumerate(lines)
        if line.startswith("--user stop") and PROBE_TIMER in line
    ]

    assert completed.stdout == ""
    assert armed, f"the installer never attempted `enable --now`: {lines}"
    assert restored and restored[-1] > armed[0], (
        f"the ERR trap never restored the probe timer: {lines}"
    )
    after_restore = lines[restored[-1] + 1 :]
    for query in PROTECTED_QUERIES:
        assert query in after_restore, (
            f"the ERR trap restored the probe timer but never re-checked `{query}`: "
            f"{after_restore}"
        )
        # Exactly one read before the trap (the invocation baseline): the main
        # assertion never ran, so the trap's check is the only re-read.
        assert lines[: armed[0]].count(query) == 1, lines
    assert _probe_timer_state(tmp_path) == ("disabled", "inactive")
