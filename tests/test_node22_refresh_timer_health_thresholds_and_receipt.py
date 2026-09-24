"""Node-22 refresh-timer probe: thresholds, read-only structure and the receipt.

Partition (#2532 partition of the 3456-line / 202-case
tests/test_node22_refresh_timer_health.py).
R10 threshold bounds derived from the consumer's 168 h,
R11-R13 structural incapability of unit mutation, R14 receipt shape / mode /
privacy, grading totality (``ok`` is a pure ``else``) and R14b durable,
non-destructive receipt writes.
"""

from __future__ import annotations

import ast
import json
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from scripts import node22_refresh_timer_health as probe
from tests.node22_refresh_timer_health_helpers import (
    MUTATION_VERBS,
    NOW,
    PROBE_SOURCE,
    UNIT,
    _properties,
    _run,
    _systemd_timestamp,
    _verdict,
    _write_refresh_receipt,
)

# ---------------------------------------------------------------------------
# R10 -- threshold configuration is bounded by the consumer's 168 h
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_name",
    [probe.ENV_MAX_NEXT_DWELL_HOURS, probe.ENV_MAX_MANIFEST_AGE_HOURS],
)
@pytest.mark.parametrize("value", ["168", "200"])
def test_r10_a_freshness_threshold_at_or_over_168_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_name: str, value: str
) -> None:
    status, root, _log = _run(tmp_path, monkeypatch, thresholds={env_name: value})

    assert status != 0
    assert not (root / "latest.json").exists()


@pytest.mark.parametrize("value", ["0", "-4", "36.5", "abc"])
def test_r10_a_non_positive_or_non_integer_threshold_is_rejected(value: str) -> None:
    with pytest.raises(probe.ConfigError):
        probe.load_thresholds({probe.ENV_STOPPED_DWELL_HOURS: value})


def test_r10_defaults_are_the_documented_thresholds() -> None:
    thresholds = probe.load_thresholds({})

    assert thresholds.max_next_dwell_hours == 36
    assert thresholds.max_manifest_age_hours == 120
    assert thresholds.stopped_dwell_hours == 6


def test_r10_the_threshold_ceilings_are_derived_from_the_consumer_bound() -> None:
    """The two ceilings have ONE source each, and the tests below read them.

    `MAX_THRESHOLD_HOURS` is not an independent number to be kept in sync by
    hand: it is the consumer's bound minus one refresh cadence, and the
    stopped-dwell ceiling IS one cadence.  Asserting the arithmetic here is what
    lets every boundary test below quote the constants instead of literals.
    """
    assert probe.REFRESH_CADENCE_HOURS == 24
    assert (
        probe.MAX_THRESHOLD_HOURS
        == probe.CONSUMER_MAX_MANIFEST_AGE_HOURS - probe.REFRESH_CADENCE_HOURS
    )
    assert probe.MAX_STOPPED_DWELL_HOURS == probe.REFRESH_CADENCE_HOURS
    # The shipped defaults must sit inside both rules, or the unit ships refused.
    defaults = probe.load_thresholds({})
    assert defaults.max_next_dwell_hours <= probe.MAX_THRESHOLD_HOURS
    assert defaults.max_manifest_age_hours <= probe.MAX_THRESHOLD_HOURS
    assert defaults.stopped_dwell_hours <= probe.MAX_STOPPED_DWELL_HOURS


@pytest.mark.parametrize(
    "env_name",
    [probe.ENV_MAX_NEXT_DWELL_HOURS, probe.ENV_MAX_MANIFEST_AGE_HOURS],
)
def test_r10_a_freshness_threshold_accepts_the_ceiling_and_refuses_one_over(
    env_name: str,
) -> None:
    """The boundary, quoted from the constant rather than from a literal."""
    accepted = probe.load_thresholds({env_name: str(probe.MAX_THRESHOLD_HOURS)})

    assert (
        getattr(
            accepted,
            {
                probe.ENV_MAX_NEXT_DWELL_HOURS: "max_next_dwell_hours",
                probe.ENV_MAX_MANIFEST_AGE_HOURS: "max_manifest_age_hours",
            }[env_name],
        )
        == probe.MAX_THRESHOLD_HOURS
    )
    with pytest.raises(probe.ConfigError):
        probe.load_thresholds({env_name: str(probe.MAX_THRESHOLD_HOURS + 1)})


def test_r10_the_stopped_dwell_accepts_one_cadence_and_refuses_one_over() -> None:
    """The stopped-dwell's own ceiling is tighter than the shared one.

    A timer idle for longer than its own period has already missed a tick, so
    `MAX_STOPPED_DWELL_HOURS` refuses values the two freshness thresholds
    accept -- which is why this boundary is asserted separately.
    """
    accepted = probe.load_thresholds(
        {probe.ENV_STOPPED_DWELL_HOURS: str(probe.MAX_STOPPED_DWELL_HOURS)}
    )

    assert accepted.stopped_dwell_hours == probe.MAX_STOPPED_DWELL_HOURS
    with pytest.raises(probe.ConfigError):
        probe.load_thresholds(
            {probe.ENV_STOPPED_DWELL_HOURS: str(probe.MAX_STOPPED_DWELL_HOURS + 1)}
        )
    # And the tighter ceiling is genuinely tighter: a value the freshness
    # thresholds accept is refused here.
    assert probe.MAX_STOPPED_DWELL_HOURS < probe.MAX_THRESHOLD_HOURS
    with pytest.raises(probe.ConfigError):
        probe.load_thresholds(
            {probe.ENV_STOPPED_DWELL_HOURS: str(probe.MAX_THRESHOLD_HOURS)}
        )


@pytest.mark.parametrize(
    "env_name",
    [
        probe.ENV_MAX_NEXT_DWELL_HOURS,
        probe.ENV_MAX_MANIFEST_AGE_HOURS,
        probe.ENV_STOPPED_DWELL_HOURS,
    ],
)
def test_r10_every_threshold_refuses_the_shared_ceiling_plus_one(env_name: str) -> None:
    with pytest.raises(probe.ConfigError):
        probe.load_thresholds({env_name: str(probe.MAX_THRESHOLD_HOURS + 1)})


@pytest.mark.parametrize(
    "env_name",
    [
        probe.ENV_MAX_NEXT_DWELL_HOURS,
        probe.ENV_MAX_MANIFEST_AGE_HOURS,
        probe.ENV_STOPPED_DWELL_HOURS,
    ],
)
def test_r10_the_previously_accepted_167_is_now_refused(env_name: str) -> None:
    """The measured defect, pinned as a literal because 167 is the regression.

    `167` passed the old `< 168` check while leaving 0.6% of the consumer's
    budget as warning: `STOPPED_DWELL_HOURS=167` with
    `MAX_MANIFEST_AGE_HOURS=167` graded the 2026-08-28 geometry `ok`/exit 0 for
    166 hours.  This literal must stay a literal -- a constant-derived
    expression would move with the constant it is meant to fence.
    """
    with pytest.raises(probe.ConfigError):
        probe.load_thresholds({env_name: "167"})


@pytest.mark.parametrize(
    ("env_name", "ceiling"),
    [
        (probe.ENV_MAX_NEXT_DWELL_HOURS, probe.MAX_THRESHOLD_HOURS),
        (probe.ENV_MAX_MANIFEST_AGE_HOURS, probe.MAX_THRESHOLD_HOURS),
        (probe.ENV_STOPPED_DWELL_HOURS, probe.MAX_STOPPED_DWELL_HOURS),
    ],
)
def test_r10_a_refused_threshold_writes_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_name: str, ceiling: int
) -> None:
    """A refusal happens BEFORE any evidence is collected, so no receipt lands.

    A receipt written by a refused configuration would be a document with a
    verdict field and no verdict behind it.
    """
    status, root, _log = _run(
        tmp_path, monkeypatch, thresholds={env_name: str(ceiling + 1)}
    )

    assert status != 0
    assert not (root / "latest.json").exists()


# R10b -- the margin claim, executable.  The prose version ("far enough below
# 168 h to leave operator margin") held for the shipped defaults only; this
# sweeps the whole ACCEPTED range and asserts the claim for every combination
# an operator can actually configure.
DEAD_LANE_IDLE_HOURS = (24.5, 48.0, 100.0, 167.5)


def test_r10b_no_accepted_threshold_combination_grades_a_dead_lane_ok() -> None:
    """The 2026-08-28 geometry: `enabled` + `inactive`, manifest ageing with it.

    For every combination `load_thresholds` ACCEPTS, a lane idle past one
    refresh cadence must grade non-`ok`.  Under the old `< 168` rule this fails
    outright: stopped-dwell 36 with manifest-age 120 grades a 24.5-hour-dead
    lane `ok`, which is the hole the cadence cap closes.
    """
    freshness_values = sorted(
        {1, 2, 36, 120, probe.MAX_THRESHOLD_HOURS - 1, probe.MAX_THRESHOLD_HOURS}
    )
    dwell_values = list(range(1, probe.MAX_STOPPED_DWELL_HOURS + 1))
    checked = 0
    for idle_hours in DEAD_LANE_IDLE_HOURS:
        properties = _properties(
            unit_file_state="enabled",
            active_state="inactive",
            sub_state="dead",
            inactive_enter=_systemd_timestamp(NOW - timedelta(hours=idle_hours)),
            next_elapse="",
        )
        for next_dwell in freshness_values:
            for manifest_age_threshold in freshness_values:
                for stopped_dwell in dwell_values:
                    thresholds = probe.load_thresholds(
                        {
                            probe.ENV_MAX_NEXT_DWELL_HOURS: str(next_dwell),
                            probe.ENV_MAX_MANIFEST_AGE_HOURS: str(manifest_age_threshold),
                            probe.ENV_STOPPED_DWELL_HOURS: str(stopped_dwell),
                        }
                    )
                    verdict = probe.grade(
                        now=NOW,
                        properties=properties,
                        # The manifest stopped ageing when the timer did.
                        manifest_age_hours=idle_hours,
                        thresholds=thresholds,
                        systemd_error=None,
                    )
                    checked += 1
                    assert verdict != probe.VERDICT_OK, (
                        f"idle {idle_hours} h graded ok at next_dwell={next_dwell}, "
                        f"manifest_age={manifest_age_threshold}, "
                        f"stopped_dwell={stopped_dwell}"
                    )
    assert checked == len(DEAD_LANE_IDLE_HOURS) * len(freshness_values) ** 2 * len(
        dwell_values
    )


def test_a_pinned_clock_must_carry_a_timezone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(probe.ConfigError):
        probe.resolve_now("2026-09-12T15:34:00")


# ---------------------------------------------------------------------------
# R11 / R12 / R13 -- the probe is structurally incapable of mutation
# ---------------------------------------------------------------------------


def test_r11_probe_source_contains_no_unit_mutation_verb() -> None:
    source = PROBE_SOURCE.read_text()
    hits = sorted({match.group(0) for match in MUTATION_VERBS.finditer(source)})

    assert hits == [], f"probe source carries unit mutation verbs: {hits}"


def test_r12_only_read_only_systemctl_subcommands_are_ever_invoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _status, _root, log = _run(tmp_path, monkeypatch)
    invocations = [line.split() for line in log.read_text().splitlines() if line.strip()]

    assert invocations, "the fake systemctl recorded no invocation at all"
    subcommands = set()
    for argv in invocations:
        assert argv[0] == "--user"
        subcommands.add(argv[1])
    assert subcommands <= {"show", "list-timers"}
    assert subcommands == {"show", "list-timers"}


def test_r13_probe_imports_only_the_standard_library() -> None:
    tree = ast.parse(PROBE_SOURCE.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
            elif node.level:  # pragma: no cover - a relative import would be a defect
                pytest.fail("the probe must not carry a relative import")

    non_stdlib = sorted(imported - set(sys.stdlib_module_names))
    assert non_stdlib == [], f"probe imports non-stdlib modules: {non_stdlib}"


# ---------------------------------------------------------------------------
# R14 -- receipt shape, mode and privacy
# ---------------------------------------------------------------------------


def test_r14_receipt_is_private_bounded_and_carries_the_required_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt_path_hint = tmp_path / "refresh-latest.json"
    status, root, _log = _run(tmp_path, monkeypatch)
    target = root / "latest.json"
    payload = json.loads(target.read_text())

    assert status == 0
    assert root.stat().st_mode & 0o777 == 0o700
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.stat().st_size <= probe.MAX_HEALTH_RECEIPT_BYTES
    assert set(payload) == {
        "schema_version",
        "generated_at",
        "verdict",
        "unit",
        "unit_file_state",
        "active_state",
        "sub_state",
        "inactive_enter_timestamp",
        "next_elapse",
        "last_trigger",
        "manifest_age_hours",
        "manifest_source",
        "max_next_dwell_hours",
        "max_manifest_age_hours",
        "stopped_dwell_hours",
    }
    assert payload["manifest_source"] == probe.MANIFEST_SOURCE_LATEST
    assert payload["schema_version"] == probe.RECEIPT_SCHEMA_VERSION
    assert payload["unit"] == UNIT
    assert payload["generated_at"] == "2026-09-12T15:34:00Z"
    assert (payload["max_next_dwell_hours"], payload["max_manifest_age_hours"]) == (36, 120)
    assert payload["stopped_dwell_hours"] == 6
    # No environment value other than the integer thresholds and the unit name:
    # in particular neither receipt path nor the systemctl binary path leaks.
    serialized = target.read_text()
    for leak in (str(receipt_path_hint), str(root), str(tmp_path / "fake-systemctl")):
        assert leak not in serialized


def test_r14_receipt_signal_strings_are_length_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, root, _log = _run(
        tmp_path, monkeypatch, properties=_properties(sub_state="x" * 5000)
    )
    payload = json.loads((root / "latest.json").read_text())

    del status  # the verdict is irrelevant to the length cap
    assert len(payload["sub_state"]) == probe.MAX_SIGNAL_LENGTH


@pytest.mark.parametrize("umask_value", [0o022, 0o002])
def test_every_created_receipt_ancestor_is_private_not_just_the_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, umask_value: int
) -> None:
    """`Path.mkdir(mode=..., parents=True)` applies `mode` to the LEAF only; the
    intermediate levels would be created 0777 minus umask.  D2 wants the whole
    receipt root private, so assert every level the probe created is 0700 under
    a permissive umask -- the condition under which the leaf-only bug is visible.
    """
    root = tmp_path / "nested" / "refresh-timer-health" / "receipts"
    previous = os.umask(umask_value)
    try:
        status, _root, _log = _run(tmp_path, monkeypatch, receipt_root=root)
    finally:
        os.umask(previous)

    assert status == 0
    for level in (root, root.parent, root.parent.parent):
        assert level.stat().st_mode & 0o777 == 0o700, level


@pytest.mark.parametrize(
    ("value", "expected_stdout"),
    [
        pytest.param("1", True, id="one"),
        pytest.param("true", True, id="true"),
        pytest.param("0", False, id="zero"),
        pytest.param("false", False, id="false"),
    ],
)
def test_the_json_stdout_switch_reads_the_env_value_not_merely_its_presence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
    expected_stdout: bool,
) -> None:
    """`NHMS_REFRESH_HEALTH_JSON=0` must mean off, not "the name is set".

    `thresholds=` is just "extra env to set after `_run`'s delenv sweep", which
    is the only way to hand this switch a value the helper will not wipe.
    """
    status, _root, _log = _run(tmp_path, monkeypatch, thresholds={probe.ENV_JSON: value})

    assert status == 0
    printed = capsys.readouterr().out
    assert bool(printed.strip()) is expected_stdout, printed
    if expected_stdout:
        assert json.loads(printed)["verdict"] == "ok"


def test_receipt_is_rewritten_in_place_on_every_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick is a pure read plus one receipt write; repeated ticks are
    idempotent apart from the timestamp."""
    root = tmp_path / "health-receipts"
    _run(tmp_path, monkeypatch, receipt_root=root)
    first = json.loads((root / "latest.json").read_text())
    _run(tmp_path, monkeypatch, receipt_root=root, now=NOW + timedelta(hours=1))
    second = json.loads((root / "latest.json").read_text())

    assert sorted(path.name for path in root.iterdir()) == ["latest.json"]
    assert first["generated_at"] != second["generated_at"]
    assert {key: value for key, value in first.items() if key != "generated_at"} == {
        key: value for key, value in second.items() if key not in {"generated_at", "manifest_age_hours"}
    } | {"manifest_age_hours": first["manifest_age_hours"]}


def test_the_probe_refuses_a_symlinked_receipt_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "health-receipts"
    root.mkdir(mode=0o700)
    (root / "latest.json").symlink_to(tmp_path / "elsewhere.json")

    status, _root, _log = _run(tmp_path, monkeypatch, receipt_root=root)

    assert status != 0
    assert not (tmp_path / "elsewhere.json").exists()


def test_the_probe_refuses_a_symlinked_refresh_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = _write_refresh_receipt(tmp_path)
    link = tmp_path / "linked-latest.json"
    link.symlink_to(real)

    status, root, _log = _run(tmp_path, monkeypatch, receipt=link)

    assert status != 0
    assert _verdict(root) == "manifest_unavailable"


def test_the_probe_refuses_an_oversize_refresh_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oversize = tmp_path / "oversize.json"
    oversize.write_bytes(b"{" + b" " * (probe.MAX_REFRESH_RECEIPT_BYTES + 16) + b"}")

    status, root, _log = _run(tmp_path, monkeypatch, receipt=oversize)

    assert status != 0
    assert _verdict(root) == "manifest_unavailable"


def test_json_flag_prints_the_verdict_document_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    status, root, _log = _run(tmp_path, monkeypatch, extra_argv=["--json"])
    captured = capsys.readouterr()

    assert status == 0
    assert json.loads(captured.out) == json.loads((root / "latest.json").read_text())


# ---------------------------------------------------------------------------
# Grading totality -- `ok` is a pure `else`
# ---------------------------------------------------------------------------


def test_every_verdict_the_table_names_is_reachable_and_nothing_else_is() -> None:
    known = {
        probe.VERDICT_PROBE_FAILED,
        probe.VERDICT_MANIFEST_EXPIRED,
        probe.VERDICT_TIMER_STOPPED,
        probe.VERDICT_TIMER_NOT_ENABLED,
        probe.VERDICT_TIMER_NOT_SCHEDULED,
        probe.VERDICT_MANIFEST_STALE,
        probe.VERDICT_MANIFEST_UNAVAILABLE,
        probe.VERDICT_OK,
    }
    thresholds = probe.load_thresholds({})
    produced = set()
    for unit_file_state in ("enabled", "disabled", "masked", "not-found", ""):
        for active_state in ("active", "inactive", "failed"):
            for inactive_enter in ("", _systemd_timestamp(NOW - timedelta(days=6)),
                                   _systemd_timestamp(NOW - timedelta(minutes=30))):
                for next_elapse in ("", _systemd_timestamp(NOW + timedelta(hours=11)),
                                    _systemd_timestamp(NOW + timedelta(hours=40))):
                    for age in (None, 1.0, 130.0, 200.0):
                        produced.add(
                            probe.grade(
                                now=NOW,
                                properties=_properties(
                                    unit_file_state=unit_file_state,
                                    active_state=active_state,
                                    inactive_enter=inactive_enter,
                                    next_elapse=next_elapse,
                                ),
                                manifest_age_hours=age,
                                thresholds=thresholds,
                                systemd_error=None,
                            )
                        )

    assert produced <= known
    assert produced == known


# ---------------------------------------------------------------------------
# R14b -- the receipt write is durable and non-destructive
# ---------------------------------------------------------------------------


def test_r14b_a_short_writing_os_write_still_lands_a_complete_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.write` may write fewer bytes than asked; a single unchecked call
    silently truncates the receipt.  The loop is what makes it whole."""
    real_write = os.write

    def chunked(descriptor: int, data: bytes) -> int:
        return real_write(descriptor, data[:7])

    monkeypatch.setattr(probe.os, "write", chunked)

    status, root, _log = _run(tmp_path, monkeypatch)
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0
    assert payload["verdict"] == "ok"
    assert payload["unit"] == UNIT


def test_r14b_an_os_write_that_makes_no_progress_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe.os, "write", lambda descriptor, data: 0)

    status, root, _log = _run(tmp_path, monkeypatch)

    assert status != 0
    assert list(root.iterdir()) == []


def test_r14b_a_failed_write_leaves_the_previous_receipt_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Write-temp-then-`replace`: a failed write must not destroy the last good
    receipt, which on a watchdog is the only durable evidence there is."""
    root = tmp_path / "health-receipts"
    status, _root, _log = _run(tmp_path, monkeypatch, receipt_root=root)
    assert status == 0
    previous = (root / "latest.json").read_bytes()
    capsys.readouterr()

    real_write = os.write
    calls: list[int] = []

    def failing(descriptor: int, data: bytes) -> int:
        calls.append(1)
        if len(calls) == 1:
            return real_write(descriptor, data[:5])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(probe.os, "write", failing)

    status, _root, _log = _run(
        tmp_path, monkeypatch, receipt_root=root, now=NOW + timedelta(hours=2)
    )
    captured = capsys.readouterr()

    assert status != 0
    assert (root / "latest.json").read_bytes() == previous
    assert sorted(path.name for path in root.iterdir()) == ["latest.json"]
    # D2 makes the journal the alert channel, so the verdict must reach it even
    # when the receipt could not be written.
    assert "verdict=ok" in captured.err
    assert "receipt not written" in captured.err


def test_a4_a_receipt_write_failure_still_reports_the_verdict_and_evidence_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "health-receipts"
    root.mkdir(mode=0o700)
    (root / "latest.json").symlink_to(tmp_path / "elsewhere.json")

    status, _root, _log = _run(
        tmp_path, monkeypatch, receipt_root=root, receipt=tmp_path / "absent.json"
    )
    captured = capsys.readouterr()

    assert status != 0
    assert "verdict=manifest_unavailable" in captured.err
    assert "refresh receipt unreadable" in captured.err
    assert "receipt not written" in captured.err
    assert not (tmp_path / "elsewhere.json").exists()
