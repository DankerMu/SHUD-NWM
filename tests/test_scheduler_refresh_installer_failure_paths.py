"""The node-22 refresh installer's failure paths and restore baseline (#2294).

Behavioural cases run the real
``scripts/install_node22_scheduler_file_provider_refresh.sh`` against the
real-exit-code fake ``systemctl`` of ``tests/scheduler_refresh_installer_harness.py``
and judge each run by its exit status, its stdout (the status line) and the
systemctl trace. Divergence classes (design D7):

* (A) failure before any mutation: rc != 0, no status line, no mutating verb,
  state root and unit dir byte-identical;
* (B) ``--rollback`` read-back failure: rc != 0, no status line, the rollback
  steps exactly once;
* (C) failure after the first mutation of ``--install`` / ``--enable``: rc 1, no
  status line, exactly one main-shell restore followed by both read-backs.

They also replace the one-sided source-substring checks the deployment
contract used to make about installer behaviour (env-file checks, ``cmp -s``,
``--validate-current-receipt``, the compute scheduler only ever read).
"""

from __future__ import annotations

import pytest

from tests.scheduler_refresh_installer_harness import (
    NODE22_REFRESH_BEFORE,
    NODE22_SCHEDULER_BEFORE,
    REFRESH_READ_BACK,
    REFRESH_UNITS,
    RESTORE_SIGNATURE,
    SCHEDULER_READ_BACK,
    SCHEDULER_SERVICE,
    SCHEDULER_TIMER,
    SERVICE,
    STATUS,
    TIMER,
    Result,
    Rig,
    installed_and_armed,
    is_subsequence,
    make_rig,
    set_on,
    write_env,
)


@pytest.fixture
def rig(tmp_path) -> Rig:
    return make_rig(tmp_path)


def assert_unmutated(result: Result, rig: Rig, before: dict[str, bytes]) -> None:
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.mutating() == [], result.trace
    assert rig.snapshot() == before


def assert_read_backs_follow(result: Result) -> None:
    tail = result.after_last_mutation()
    for query in (*REFRESH_READ_BACK, *SCHEDULER_READ_BACK):
        assert query in tail, f"read-back {query!r} missing after the restore: {result.trace}"


# ---------------------------------------------------------------------------
# (A) failures before any mutation
# ---------------------------------------------------------------------------


def test_enable_with_an_invalid_current_receipt_fails_before_any_mutation(rig: Rig) -> None:
    assert rig.run("--install").stdout == STATUS["--install"]
    before = rig.snapshot()

    assert_unmutated(rig.run("--enable", validate_rc=2), rig, before)
    assert rig.unit(TIMER) == ("disabled", "inactive")


@pytest.mark.parametrize(
    ("unit", "enabled", "active"),
    [
        (TIMER, "enabled", "active"),
        (TIMER, "enabled", "inactive"),
        (TIMER, "disabled", "active"),
        (TIMER, "masked", "inactive"),
        (TIMER, "disabled", ""),
        (SERVICE, "enabled", "inactive"),
    ],
)
def test_install_refuses_while_the_lane_is_armed_and_changes_nothing(
    rig: Rig, unit: str, enabled: str, active: str
) -> None:
    assert rig.run("--install").stdout == STATUS["--install"]
    rig.set_unit(unit, enabled, active)
    before = rig.snapshot()

    refused = rig.run("--install")

    assert_unmutated(refused, rig, before)
    assert f"refusing --install: {unit} is {enabled}/{active or '<no answer>'}; run --rollback first" in (
        refused.stderr
    )
    assert rig.unit(unit) == (enabled, active)


def test_install_over_the_armed_steady_state_refuses_and_leaves_it_armed(rig: Rig) -> None:
    installed_and_armed(rig)
    before = rig.snapshot()

    refused = rig.run("--install")

    assert_unmutated(refused, rig, before)
    assert rig.unit(TIMER) == ("enabled", "active")


@pytest.mark.parametrize(
    "baseline",
    [
        pytest.param(b"disabled\nstatic\tinactive\n", id="one-field"),
        pytest.param(b"disabled\tinactive\textra\nstatic\tinactive\n", id="three-field"),
        pytest.param(b"disabled\tinactive\nstatic\tinactive\nstatic\tinactive\n", id="three-line"),
        pytest.param(b"disabled\tinactive\n", id="one-line"),
        pytest.param(b"disabled\t\nstatic\tinactive\n", id="empty-field"),
        pytest.param(b"", id="empty"),
    ],
)
def test_rollback_with_a_malformed_baseline_fails_before_any_mutation(rig: Rig, baseline: bytes) -> None:
    installed_and_armed(rig)
    (rig.state_root / "refresh.before").write_bytes(baseline)
    before = rig.snapshot()

    assert_unmutated(rig.run("--rollback"), rig, before)
    assert rig.unit(TIMER) == ("enabled", "active")


def test_rollback_without_a_recorded_baseline_fails_before_any_mutation(rig: Rig) -> None:
    rig.state_root.mkdir(mode=0o700)
    rig.unit_dir.mkdir(mode=0o700)
    before = rig.snapshot()

    result = rig.run("--rollback")

    assert_unmutated(result, rig, before)
    assert "no recorded refresh baseline" in result.stderr


@pytest.mark.parametrize(
    ("lines", "mode", "returncode"),
    [
        pytest.param([], 0o600, 1, id="direct-grid-missing"),
        pytest.param(["NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true"] * 2, 0o600, 1, id="direct-grid-twice"),
        pytest.param(["NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true"], 0o640, 1, id="mode-0640"),
        pytest.param(["NHMS_SCHEDULER_REQUIRE_DIRECT_GRID=true", "PGHOST=db"], 0o600, 2, id="db-selector"),
    ],
)
def test_install_env_file_checks_refuse_before_any_mutation(
    rig: Rig, lines: list[str], mode: int, returncode: int
) -> None:
    write_env(rig, lines, mode=mode)

    result = rig.run("--install")

    assert result.returncode == returncode
    assert result.stdout == ""
    assert result.mutating() == []
    assert not (rig.state_root / "refresh.before").exists()


def test_enable_refuses_unit_files_that_differ_from_the_repo(rig: Rig) -> None:
    assert rig.run("--install").stdout == STATUS["--install"]
    edited = rig.unit_dir / TIMER
    edited.write_text(edited.read_text() + "# local edit\n")
    before = rig.snapshot()

    assert_unmutated(rig.run("--enable"), rig, before)


# ---------------------------------------------------------------------------
# (B) --rollback read-back failures
# ---------------------------------------------------------------------------


def test_rollback_fails_when_systemctl_silently_leaves_the_timer_enabled(rig: Rig) -> None:
    installed_and_armed(rig)

    result = rig.run("--rollback", noop=[f"disable --now {TIMER}", f"disable {TIMER}"])

    assert result.returncode != 0
    assert result.stdout == ""
    assert f"refresh read-back: {TIMER} is enabled/inactive, expected disabled/inactive" in result.stderr
    assert result.trace.count("daemon-reload") == 1
    assert result.restores() == 1
    assert rig.unit(TIMER) == ("enabled", "inactive")


def test_rollback_fails_when_a_compute_scheduler_timer_moves(rig: Rig) -> None:
    installed_and_armed(rig)

    result = rig.run("--rollback", after=[set_on("daemon-reload@1", SCHEDULER_TIMER, "enabled", "inactive")])

    assert result.returncode != 0
    assert result.stdout == ""
    assert "scheduler read-back" in result.stderr
    assert result.trace.count("daemon-reload") == 1
    assert result.restores() == 1


def test_rollback_restores_the_baseline_and_reads_it_back(rig: Rig) -> None:
    installed_and_armed(rig)

    result = rig.run("--rollback")

    assert result.returncode == 0, result.stderr
    assert result.stdout == STATUS["--rollback"]
    assert rig.unit(TIMER) == ("disabled", "inactive")
    assert rig.unit(SERVICE) == ("static", "inactive")
    assert not any((rig.unit_dir / unit).exists() for unit in REFRESH_UNITS)
    assert_read_backs_follow(result)


# ---------------------------------------------------------------------------
# (C) failures after the first mutation of --install / --enable
# ---------------------------------------------------------------------------


def _assert_backed_out_once(result: Result) -> None:
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.restores() == 1, result.trace
    assert result.stderr.count("restoring") == 1, result.stderr
    assert_read_backs_follow(result)


def test_install_backs_out_once_when_the_scheduler_assertion_fails_inside_a_function(rig: Rig) -> None:
    result = rig.run("--install", after=[set_on("daemon-reload@1", SCHEDULER_TIMER, "disabled", "active")])

    _assert_backed_out_once(result)
    assert not any((rig.unit_dir / unit).exists() for unit in REFRESH_UNITS)


def test_install_backs_out_once_when_the_lane_reads_back_armed_after_install(rig: Rig) -> None:
    """The swallowed `disable --now` did not take: the success read-back refuses."""
    result = rig.run(
        "--install",
        after=[set_on("daemon-reload@1", TIMER, "enabled", "active")],
        noop=[f"disable --now {TIMER}@1"],
    )

    _assert_backed_out_once(result)
    assert f"install read-back: {TIMER} is enabled/active, not disarmed" in result.stderr
    assert rig.unit(TIMER) == ("disabled", "inactive")


def test_enable_backs_out_once_when_is_active_fails_inside_a_command_substitution(rig: Rig) -> None:
    """`is-active` exits 3 for the inactive timer: one restore, never a second
    one from inside the substitution's subshell."""
    assert rig.run("--install").stdout == STATUS["--install"]

    result = rig.run("--enable", after=[set_on(f"enable --now {TIMER}@1", TIMER, "enabled", "inactive")])

    _assert_backed_out_once(result)
    assert rig.unit(TIMER) == ("disabled", "inactive")


def test_enable_backs_out_once_when_the_scheduler_assertion_fails(rig: Rig) -> None:
    assert rig.run("--install").stdout == STATUS["--install"]

    result = rig.run("--enable", after=[set_on(f"enable --now {TIMER}@1", SCHEDULER_SERVICE, "disabled", "inactive")])

    _assert_backed_out_once(result)
    assert rig.unit(TIMER) == ("disabled", "inactive")


def test_a_failed_enable_of_an_armed_lane_restores_it_armed(rig: Rig) -> None:
    """The enable trap's target is the state THIS invocation started from."""
    installed_and_armed(rig)

    result = rig.run("--enable", fail=[f"enable --now {TIMER}"])

    assert result.returncode == 1
    assert result.stdout == ""
    assert is_subsequence([f"enable {TIMER}", f"start {TIMER}"], result.trace), result.trace
    assert rig.unit(TIMER) == ("enabled", "active")
    assert_read_backs_follow(result)


INSTALL_HANDLER_STEPS = (
    f"disable --now {TIMER}",
    f"stop {SERVICE}",
    "daemon-reload",
    f"disable {TIMER}",
    f"stop {TIMER}",
    f"disable {SERVICE}",
    f"stop {SERVICE}",
)


@pytest.mark.parametrize(
    "failing",
    [
        f"disable --now {TIMER}",
        f"stop {SERVICE}@1",
        "daemon-reload@2",
        f"disable {TIMER}",
        f"stop {TIMER}",
        f"disable {SERVICE}",
        f"stop {SERVICE}@2",
    ],
)
def test_a_failing_install_restore_step_does_not_cut_the_restore_short(rig: Rig, failing: str) -> None:
    result = rig.run("--install", fail=["daemon-reload@1", failing])

    assert result.returncode == 1
    assert result.stdout == ""
    handler = result.trace[result.trace.index("daemon-reload") + 1 :]
    assert is_subsequence(INSTALL_HANDLER_STEPS, handler), handler
    assert_read_backs_follow(result)
    assert not any((rig.unit_dir / unit).exists() for unit in REFRESH_UNITS)


def test_a_failing_unit_file_restore_does_not_cut_the_install_restore_short(rig: Rig) -> None:
    rig.unit_dir.mkdir(mode=0o700)
    try:
        result = rig.run(
            "--install",
            fail=["daemon-reload@1"],
            after=[{"on": "daemon-reload@1", "chmod": [str(rig.unit_dir), 0o500]}],
        )
    finally:
        rig.unit_dir.chmod(0o700)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "restore step failed: remove unit file" in result.stderr
    handler = result.trace[result.trace.index("daemon-reload") + 1 :]
    assert is_subsequence(INSTALL_HANDLER_STEPS, handler), handler
    assert_read_backs_follow(result)


@pytest.mark.parametrize(
    "failing",
    [f"disable {TIMER}", f"stop {TIMER}", f"disable {SERVICE}", f"stop {SERVICE}"],
)
def test_a_failing_enable_restore_step_does_not_cut_the_restore_short(rig: Rig, failing: str) -> None:
    assert rig.run("--install").stdout == STATUS["--install"]

    result = rig.run("--enable", fail=[f"enable --now {TIMER}", failing])

    assert result.returncode == 1
    assert result.stdout == ""
    steps = [f"disable {TIMER}", f"stop {TIMER}", f"disable {SERVICE}", f"stop {SERVICE}"]
    assert is_subsequence(steps, result.trace), result.trace
    assert_read_backs_follow(result)


def test_a_failing_enable_step_in_the_restore_is_counted_and_start_still_runs(rig: Rig) -> None:
    installed_and_armed(rig)

    result = rig.run(
        "--enable",
        fail=[f"enable --now {TIMER}", f"enable {TIMER}"],
        after=[set_on(f"enable --now {TIMER}", TIMER, "disabled", "inactive")],
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert f"restore step failed: enable {TIMER}" in result.stderr
    assert is_subsequence([f"enable {TIMER}", f"start {TIMER}"], result.trace), result.trace
    assert "restore step failed: enable restore: refresh read-back" in result.stderr
    assert_read_backs_follow(result)


def test_the_install_restore_counts_a_malformed_baseline_as_a_failed_step_and_still_reads_back(
    rig: Rig,
) -> None:
    assert rig.run("--install").stdout == STATUS["--install"]
    (rig.state_root / "refresh.before").write_bytes(b"disabled\tinactive\textra\nstatic\tinactive\n")

    result = rig.run("--install", fail=["daemon-reload@1"])

    assert result.returncode == 1
    assert result.stdout == ""
    assert "restore step failed: read " in result.stderr
    assert "restore step failed: install restore: refresh read-back" in result.stderr
    assert_read_backs_follow(result)


# ---------------------------------------------------------------------------
# D3 -- per-type, per-invocation compute-scheduler comparison
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["--install", "--enable", "--rollback"])
def test_a_compute_scheduler_oneshot_firing_mid_run_does_not_abort(rig: Rig, action: str) -> None:
    if action != "--install":
        assert rig.run("--install").stdout == STATUS["--install"]

    result = rig.run(action, flip=[SCHEDULER_SERVICE])

    assert result.returncode == 0, result.stderr
    assert result.stdout == STATUS[action]


@pytest.mark.parametrize("garbage", [NODE22_SCHEDULER_BEFORE, b"\x00not\ta\tstate\n\n\n"])
@pytest.mark.parametrize("action", ["--install", "--enable", "--rollback"])
def test_a_legacy_scheduler_before_is_neither_read_nor_rewritten(rig: Rig, action: str, garbage: bytes) -> None:
    if action != "--install":
        assert rig.run("--install").stdout == STATUS["--install"]
    rig.state_root.mkdir(mode=0o700, exist_ok=True)
    legacy = rig.state_root / "scheduler.before"
    legacy.write_bytes(garbage)

    result = rig.run(action)

    assert result.returncode == 0, result.stderr
    assert result.stdout == STATUS[action]
    assert legacy.read_bytes() == garbage


def test_no_action_writes_a_scheduler_baseline_file(rig: Rig) -> None:
    for action in ("--install", "--enable", "--rollback"):
        result = rig.run(action)
        assert result.stdout == STATUS[action], result.stderr
        assert not (rig.state_root / "scheduler.before").exists()


@pytest.mark.parametrize("action", ["--install", "--enable", "--rollback"])
def test_the_compute_scheduler_units_are_only_ever_read(rig: Rig, action: str) -> None:
    if action != "--install":
        installed_and_armed(rig)

    result = rig.run(action)

    assert result.stdout == STATUS[action], result.stderr
    for line in result.trace:
        if SCHEDULER_TIMER in line or SCHEDULER_SERVICE in line:
            assert line.split()[0] in {"is-enabled", "is-active"}, line
    assert all(line.split()[-1] in REFRESH_UNITS for line in result.mutating() if line != "daemon-reload")


# ---------------------------------------------------------------------------
# D4 / D5 -- the restore baseline
# ---------------------------------------------------------------------------


def _seed_operator_units(rig: Rig) -> dict[str, bytes]:
    rig.unit_dir.mkdir(mode=0o700)
    seeded = {unit: f"# operator-local {unit} (pre-lane)\n".encode() for unit in REFRESH_UNITS}
    for unit, content in seeded.items():
        (rig.unit_dir / unit).write_bytes(content)
    return seeded


def test_two_installs_then_rollback_restore_the_first_baseline(rig: Rig) -> None:
    seeded = _seed_operator_units(rig)
    assert rig.run("--install").stdout == STATUS["--install"]
    baseline = {path: content for path, content in rig.snapshot().items() if "install-state" in path}

    assert rig.run("--install").stdout == STATUS["--install"]
    assert {path: content for path, content in rig.snapshot().items() if "install-state" in path} == baseline
    assert rig.run("--enable").stdout == STATUS["--enable"]
    result = rig.run("--rollback")

    assert result.stdout == STATUS["--rollback"], result.stderr
    assert {unit: (rig.unit_dir / unit).read_bytes() for unit in REFRESH_UNITS} == seeded
    assert rig.unit(TIMER) == ("disabled", "inactive")


def test_rollback_keeps_the_baseline_and_is_idempotent(rig: Rig) -> None:
    installed_and_armed(rig)
    baseline = (rig.state_root / "refresh.before").read_bytes()

    for _ in range(2):
        result = rig.run("--rollback")
        assert result.stdout == STATUS["--rollback"], result.stderr
        assert (rig.state_root / "refresh.before").read_bytes() == baseline


def test_an_interrupted_first_install_is_recaptured_without_temp_residue(rig: Rig) -> None:
    seeded = _seed_operator_units(rig)
    rig.state_root.mkdir(mode=0o700)
    for unit in REFRESH_UNITS:
        (rig.state_root / f"{unit}.before").write_bytes(b"partial capture\n")
    (rig.state_root / "refresh.before.tmp").write_bytes(b"enabled\tactive\n")

    assert rig.run("--install").stdout == STATUS["--install"]

    assert (rig.state_root / "refresh.before").read_bytes() == NODE22_REFRESH_BEFORE
    assert {unit: (rig.state_root / f"{unit}.before").read_bytes() for unit in REFRESH_UNITS} == seeded
    assert sorted(path.name for path in rig.state_root.iterdir()) == sorted(
        ["refresh.before", *(f"{unit}.before" for unit in REFRESH_UNITS)]
    )


def test_the_node22_legacy_baseline_bytes_are_accepted_as_is(rig: Rig) -> None:
    """node-22's July re-install snapshot: rollback leaves the July unit files, disabled."""
    installed_and_armed(rig)
    july = {unit: f"# July {unit}\n".encode() for unit in REFRESH_UNITS}
    (rig.state_root / "refresh.before").write_bytes(NODE22_REFRESH_BEFORE)
    for unit, content in july.items():
        (rig.state_root / f"{unit}.before").write_bytes(content)

    result = rig.run("--rollback")

    assert result.stdout == STATUS["--rollback"], result.stderr
    assert {unit: (rig.unit_dir / unit).read_bytes() for unit in REFRESH_UNITS} == july
    assert rig.unit(TIMER) == ("disabled", "inactive")
    assert rig.unit(SERVICE) == ("static", "inactive")
    assert (rig.state_root / "refresh.before").read_bytes() == NODE22_REFRESH_BEFORE


def test_a_failed_reinstall_is_backed_out_to_the_recorded_baseline(rig: Rig) -> None:
    seeded = _seed_operator_units(rig)
    assert rig.run("--install").stdout == STATUS["--install"]
    assert RESTORE_SIGNATURE not in rig.run("--install").trace

    result = rig.run("--install", fail=["daemon-reload@1"])

    assert result.returncode == 1
    assert result.stdout == ""
    assert {unit: (rig.unit_dir / unit).read_bytes() for unit in REFRESH_UNITS} == seeded
    assert rig.unit(TIMER) == ("disabled", "inactive")
