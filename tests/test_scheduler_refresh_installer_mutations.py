"""Mutation acceptance of the refresh installer's failure-path contract (#2294).

Each case copies ``scripts/install_node22_scheduler_file_provider_refresh.sh``,
applies ONE exact-anchor mutation (the anchor must occur exactly once, so a
refactor that drops it reds here instead of turning the case into a no-op),
and runs the scenario that mutation must break. The scenario's verdict holds on
the real installer and flips on the mutant (design D7, mutations 1-9). The
probe installer's two sibling mutations live beside its own suite,
``tests/test_node22_refresh_timer_health_installer.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.scheduler_refresh_installer_harness import (
    INSTALLER,
    REFRESH_READ_BACK,
    REFRESH_UNITS,
    SCHEDULER_READ_BACK,
    SCHEDULER_SERVICE,
    SCHEDULER_TIMER,
    STATUS,
    TIMER,
    Rig,
    installed_and_armed,
    is_subsequence,
    make_rig,
    set_on,
)

Scenario = Callable[[Rig, Path], bool]


def _refused_status_and_rc(result, action: str) -> bool:
    return result.returncode != 0 and result.stdout == "" and STATUS[action] not in result.stdout


def install_scheduler_divergence_backs_out(rig: Rig, installer: Path) -> bool:
    """A scheduler assertion failing inside a function backs the install out."""
    result = rig.run(
        "--install",
        installer=installer,
        after=[set_on("daemon-reload@1", SCHEDULER_TIMER, "disabled", "active")],
    )
    removed = not any((rig.unit_dir / unit).exists() for unit in REFRESH_UNITS)
    return _refused_status_and_rc(result, "--install") and result.restores() == 1 and removed


def install_refuses_scheduler_divergence(rig: Rig, installer: Path) -> bool:
    result = rig.run(
        "--install",
        installer=installer,
        after=[set_on("daemon-reload@1", SCHEDULER_TIMER, "disabled", "active")],
    )
    return _refused_status_and_rc(result, "--install")


def enable_refuses_scheduler_divergence(rig: Rig, installer: Path) -> bool:
    assert rig.run("--install").stdout == STATUS["--install"]
    result = rig.run(
        "--enable",
        installer=installer,
        after=[set_on(f"enable --now {TIMER}@1", SCHEDULER_SERVICE, "disabled", "inactive")],
    )
    return _refused_status_and_rc(result, "--enable")


def rollback_refuses_scheduler_divergence(rig: Rig, installer: Path) -> bool:
    installed_and_armed(rig)
    result = rig.run(
        "--rollback",
        installer=installer,
        after=[set_on("daemon-reload@1", SCHEDULER_TIMER, "enabled", "inactive")],
    )
    return _refused_status_and_rc(result, "--rollback")


def rollback_refuses_a_timer_left_enabled(rig: Rig, installer: Path) -> bool:
    installed_and_armed(rig)
    result = rig.run("--rollback", installer=installer, noop=[f"disable --now {TIMER}", f"disable {TIMER}"])
    return _refused_status_and_rc(result, "--rollback")


def _read_back_after_restore(result, queries: tuple[str, ...]) -> bool:
    tail = result.after_last_mutation()
    return result.returncode == 1 and result.stdout == "" and all(query in tail for query in queries)


def install_restore_reads_the_scheduler_back(rig: Rig, installer: Path) -> bool:
    result = rig.run("--install", installer=installer, fail=["daemon-reload@1"])
    return _read_back_after_restore(result, SCHEDULER_READ_BACK)


def install_restore_reads_the_refresh_units_back(rig: Rig, installer: Path) -> bool:
    result = rig.run("--install", installer=installer, fail=["daemon-reload@1"])
    return _read_back_after_restore(result, REFRESH_READ_BACK)


def enable_restore_reads_the_scheduler_back(rig: Rig, installer: Path) -> bool:
    assert rig.run("--install").stdout == STATUS["--install"]
    result = rig.run("--enable", installer=installer, fail=[f"enable --now {TIMER}"])
    return _read_back_after_restore(result, SCHEDULER_READ_BACK)


def enable_restore_reads_the_refresh_units_back(rig: Rig, installer: Path) -> bool:
    assert rig.run("--install").stdout == STATUS["--install"]
    result = rig.run("--enable", installer=installer, fail=[f"enable --now {TIMER}"])
    return _read_back_after_restore(result, REFRESH_READ_BACK)


def enable_substitution_failure_restores_exactly_once(rig: Rig, installer: Path) -> bool:
    assert rig.run("--install").stdout == STATUS["--install"]
    result = rig.run(
        "--enable",
        installer=installer,
        after=[set_on(f"enable --now {TIMER}@1", TIMER, "enabled", "inactive")],
    )
    return _refused_status_and_rc(result, "--enable") and result.restores() == 1


def a_failing_reload_does_not_cut_the_install_restore_short(rig: Rig, installer: Path) -> bool:
    result = rig.run("--install", installer=installer, fail=["daemon-reload"])
    handler = result.trace[result.trace.index("daemon-reload") + 1 :]
    later = ["daemon-reload", f"disable {TIMER}", f"stop {TIMER}", *REFRESH_READ_BACK, *SCHEDULER_READ_BACK]
    return result.returncode == 1 and result.stdout == "" and is_subsequence(later, handler)


def install_refuses_an_armed_lane_without_mutation(rig: Rig, installer: Path) -> bool:
    installed_and_armed(rig)
    result = rig.run("--install", installer=installer)
    return _refused_status_and_rc(result, "--install") and result.mutating() == []


def two_installs_then_rollback_restore_the_first_baseline(rig: Rig, installer: Path) -> bool:
    rig.unit_dir.mkdir(mode=0o700)
    seeded = {unit: f"# operator {unit}\n".encode() for unit in REFRESH_UNITS}
    for unit, content in seeded.items():
        (rig.unit_dir / unit).write_bytes(content)
    for _ in range(2):
        assert rig.run("--install", installer=installer).stdout == STATUS["--install"]
    result = rig.run("--rollback", installer=installer)
    restored = {unit: (rig.unit_dir / unit).read_bytes() for unit in REFRESH_UNITS if (rig.unit_dir / unit).exists()}
    return result.stdout == STATUS["--rollback"] and restored == seeded


def rollback_refuses_a_three_field_state_before_mutating(rig: Rig, installer: Path) -> bool:
    installed_and_armed(rig)
    (rig.state_root / "refresh.before").write_bytes(b"disabled\tinactive\textra\nstatic\tinactive\n")
    result = rig.run("--rollback", installer=installer)
    return _refused_status_and_rc(result, "--rollback") and result.mutating() == []


def install_refuses_a_lane_that_reads_back_armed(rig: Rig, installer: Path) -> bool:
    result = rig.run(
        "--install",
        installer=installer,
        after=[set_on("daemon-reload@1", TIMER, "enabled", "active")],
        noop=[f"disable --now {TIMER}@1"],
    )
    return _refused_status_and_rc(result, "--install")


GUARD_ANCHOR = 'enable_failure_restore() {\n  [[ $BASHPID == "$$" ]] || exit 1\n'
CAPTURE_ANCHOR = (
    '  timer_active=$($systemctl_bin --user is-active "$timer" 2>/dev/null || true)\n'
    '  [[ "$timer_active" == active ]]\n'
)
# master's shape: `is-active` exits 3 inside the substitution, so with `-E`
# the ERR trap also runs in the substitution's subshell.
UNGUARDED_CAPTURE = '  [[ "$($systemctl_bin --user is-active "$timer")" == active ]]\n'

MUTATIONS: list[tuple[str, tuple[tuple[str, str], ...], Scenario]] = [
    ("1-errtrace-dropped", (("set -Eeuo pipefail\n", "set -euo pipefail\n"),), install_scheduler_divergence_backs_out),
    (
        "2-scheduler-assert-install",
        (("  assert_refresh_units_disarmed\n  assert_scheduler_unchanged\n", "  assert_refresh_units_disarmed\n"),),
        install_refuses_scheduler_divergence,
    ),
    (
        "2-scheduler-assert-enable",
        (
            (
                '== inactive ]]\n  assert_scheduler_unchanged\n  trap - ERR\n  printf \'{"status":"enabled_active"',
                '== inactive ]]\n  trap - ERR\n  printf \'{"status":"enabled_active"',
            ),
        ),
        enable_refuses_scheduler_divergence,
    ),
    (
        "2-scheduler-assert-rollback",
        (('  assert_scheduler_unchanged || note_failure "rollback: scheduler read-back"\n', ""),),
        rollback_refuses_scheduler_divergence,
    ),
    (
        "2-scheduler-assert-install-restore",
        (('  assert_scheduler_unchanged || note_failure "install restore: scheduler read-back"\n', ""),),
        install_restore_reads_the_scheduler_back,
    ),
    (
        "2-scheduler-assert-enable-restore",
        (('  assert_scheduler_unchanged || note_failure "enable restore: scheduler read-back"\n', ""),),
        enable_restore_reads_the_scheduler_back,
    ),
    (
        "3-refresh-read-back-rollback",
        (
            (
                '  assert_refresh_state_restored "$baseline_timer_state" "$baseline_service_state"'
                ' || note_failure "rollback: refresh read-back"\n',
                "",
            ),
        ),
        rollback_refuses_a_timer_left_enabled,
    ),
    (
        "3-refresh-read-back-install-restore",
        (
            (
                '  assert_refresh_state_restored "$baseline_timer_state" "$baseline_service_state"'
                ' || note_failure "install restore: refresh read-back"\n',
                "",
            ),
        ),
        install_restore_reads_the_refresh_units_back,
    ),
    (
        "3-refresh-read-back-enable-restore",
        (
            (
                '  assert_refresh_state_restored "$invocation_timer_state" "$invocation_service_state"'
                ' || note_failure "enable restore: refresh read-back"\n',
                "",
            ),
        ),
        enable_restore_reads_the_refresh_units_back,
    ),
    (
        # The real capture cannot fail (`|| true`), so the guard is exercised as
        # the backstop it is: over master's unguarded substitution, the guard
        # keeps the restore at exactly one; without it the subshell runs a second.
        "4-main-shell-guard",
        ((CAPTURE_ANCHOR, UNGUARDED_CAPTURE), (GUARD_ANCHOR, "enable_failure_restore() {\n")),
        enable_substitution_failure_restores_exactly_once,
    ),
    (
        "5-daemon-reload-guard",
        (
            (
                '  $systemctl_bin --user daemon-reload || note_failure "daemon-reload"\n',
                "  $systemctl_bin --user daemon-reload\n",
            ),
        ),
        a_failing_reload_does_not_cut_the_install_restore_short,
    ),
    (
        "6-refusal",
        (
            (
                "  if ! refresh_units_disarmed; then\n"
                "    printf 'refusing --install: %s is %s/%s; run --rollback first\\n'"
                ' "$armed_unit" "$armed_enabled" "$armed_active" >&2\n'
                "    exit 1\n"
                "  fi\n",
                "",
            ),
        ),
        install_refuses_an_armed_lane_without_mutation,
    ),
    (
        "7-baseline-preservation",
        (('  if [[ ! -f "$state_root/refresh.before" ]]; then\n', "  if true; then\n"),),
        two_installs_then_rollback_restore_the_first_baseline,
    ),
    (
        "8-field-count-check",
        (
            (
                "  if [[ ${#tabs} -ne 1 ]]; then\n"
                "    printf 'malformed unit state %q: expected 2 tab-separated fields, found %d\\n'"
                ' "$state" "$((${#tabs} + 1))" >&2\n'
                "    return 1\n"
                "  fi\n",
                "",
            ),
        ),
        rollback_refuses_a_three_field_state_before_mutating,
    ),
    (
        "9-install-success-read-back",
        (("  assert_refresh_units_disarmed\n  assert_scheduler_unchanged\n", "  assert_scheduler_unchanged\n"),),
        install_refuses_a_lane_that_reads_back_armed,
    ),
]


@pytest.mark.parametrize(
    ("edits", "scenario"),
    [pytest.param(edits, scenario, id=name) for name, edits, scenario in MUTATIONS],
)
def test_each_mutation_flips_its_scenario(
    tmp_path: Path, edits: tuple[tuple[str, str], ...], scenario: Scenario
) -> None:
    real_root, mutant_root = tmp_path / "real", tmp_path / "mutant"
    real_root.mkdir()
    mutant_root.mkdir()
    real, mutant = make_rig(real_root), make_rig(mutant_root)

    assert scenario(real, INSTALLER), "the scenario does not hold on the real installer"
    assert not scenario(mutant, mutant.mutated_installer(*edits)), "the mutant survived its scenario"


def test_the_main_shell_guard_alone_keeps_an_unguarded_substitution_to_one_restore(tmp_path: Path) -> None:
    """Control for mutation 4: master's substitution shape, guard kept."""
    rig = make_rig(tmp_path)

    assert enable_substitution_failure_restores_exactly_once(
        rig, rig.mutated_installer((CAPTURE_ANCHOR, UNGUARDED_CAPTURE))
    )


def test_the_mutation_list_covers_every_named_call_site() -> None:
    """Five scheduler-assertion sites and three refresh read-back sites."""
    source = INSTALLER.read_text()
    names = [name for name, _edits, _scenario in MUTATIONS]

    assert source.count("assert_scheduler_unchanged\n") + source.count("assert_scheduler_unchanged ||") == 5
    assert source.count("  assert_refresh_state_restored ") == 3
    assert sum(name.startswith("2-") for name in names) == 5
    assert sum(name.startswith("3-") for name in names) == 3
    assert {name.split("-", 1)[0] for name in names} == {str(number) for number in range(1, 10)}
