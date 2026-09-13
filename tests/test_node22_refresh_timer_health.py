"""Unit pins for the node-22 refresh-timer health probe (issues #2146 / #2041).

Every test drives the probe through a fake ``systemctl`` shim that records each
invocation, a pinned clock (``--now``), and a temporary receipt root.  No real
systemd, no production path, no network.

Test anchors are the Invariant Matrix rows R1-R15 of
``openspec/changes/harden-node22-scheduler-refresh-lane/design.md``.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import node22_refresh_timer_health as probe
from scripts import scheduler_file_provider_refresh as runner_module

UNIT = "nhms-scheduler-file-provider-refresh.timer"
NOW = datetime(2026, 9, 12, 15, 34, tzinfo=UTC)
PROBE_SOURCE = Path(probe.__file__)

# The verbs the probe must never contain, matched on word boundaries so that
# the verdict names (`timer_stopped`, `timer_not_enabled`) and the threshold
# env name (`..._STOPPED_DWELL_HOURS`) are not false positives.
MUTATION_VERBS = re.compile(
    r"\b(start|stop|enable|disable|restart|daemon-reload)\b", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


def _systemd_timestamp(moment: datetime) -> str:
    """Render an instant the way ``systemctl show`` does.

    Built in the *local* zone so the string round-trips through the probe's
    parser on any host, exactly as it does on node-22.
    """
    return moment.astimezone().strftime("%a %Y-%m-%d %H:%M:%S %Z")


def _write_fake_systemctl(
    tmp_path: Path,
    *,
    properties: dict[str, str],
    exit_code: int = 0,
    failing_subcommand: str = "",
) -> tuple[Path, Path]:
    """Create a fake ``systemctl`` that logs every invocation it receives.

    ``exit_code`` fails every invocation; ``failing_subcommand`` fails exactly
    one of the two read-only subcommands, which is what lets a test distinguish
    "``show`` was never readable" from "``show`` answered and the NON-GRADED
    ``list-timers`` call is what failed".
    """
    log = tmp_path / "systemctl.log"
    show_output = "\n".join(f"{key}={value}" for key, value in properties.items()) + "\n"
    payload = tmp_path / "show.txt"
    payload.write_text(show_output)
    script = tmp_path / "fake-systemctl"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        f"if [ {exit_code} -ne 0 ]; then exit {exit_code}; fi\n"
        f'failing="{failing_subcommand}"\n'
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        f'    show) [ "$failing" = show ] && exit 7; cat {payload}; exit 0 ;;\n'
        "    list-timers) [ \"$failing\" = list-timers ] && exit 7; "
        "printf 'NEXT LEFT LAST PASSED UNIT ACTIVATES\\n'; exit 0 ;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    return script, log


def _properties(
    *,
    unit_file_state: str = "enabled",
    active_state: str = "active",
    sub_state: str = "waiting",
    inactive_enter: str | None = None,
    next_elapse: str | None = None,
    last_trigger: str | None = None,
) -> dict[str, str]:
    return {
        "UnitFileState": unit_file_state,
        "ActiveState": active_state,
        "SubState": sub_state,
        "InactiveEnterTimestamp": (
            _systemd_timestamp(NOW - timedelta(days=15)) if inactive_enter is None else inactive_enter
        ),
        "NextElapseUSecRealtime": (
            _systemd_timestamp(NOW + timedelta(hours=11)) if next_elapse is None else next_elapse
        ),
        "LastTriggerUSec": (
            _systemd_timestamp(NOW - timedelta(hours=13)) if last_trigger is None else last_trigger
        ),
    }


def _refresh_receipt_payload(
    *,
    manifest_age_hours: float | None = 5.0,
    schema_version: str = probe.REFRESH_RECEIPT_SCHEMA_VERSION,
    providers: list[dict[str, object]] | None = None,
    anchor: datetime = NOW,
    outcome: str = "published",
) -> dict[str, object]:
    if providers is None:
        generated_at = (anchor - timedelta(hours=manifest_age_hours or 0.0)).isoformat().replace(
            "+00:00", "Z"
        )
        providers = [
            {"name": "registry", "after_generated_at": generated_at, "entry_count": 18},
            {"name": "registry_worker_mirror", "after_generated_at": generated_at},
            {"name": "readiness", "after_generated_at": generated_at},
            {"name": "state", "after_generated_at": generated_at},
        ]
    return {"schema_version": schema_version, "outcome": outcome, "providers": providers}


def _write_refresh_receipt(
    tmp_path: Path,
    *,
    manifest_age_hours: float | None = 5.0,
    schema_version: str = probe.REFRESH_RECEIPT_SCHEMA_VERSION,
    providers: list[dict[str, object]] | None = None,
    raw: str | None = None,
    anchor: datetime = NOW,
    name: str = "refresh-latest.json",
) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw)
        return path
    path.write_text(
        json.dumps(
            _refresh_receipt_payload(
                manifest_age_hours=manifest_age_hours,
                schema_version=schema_version,
                providers=providers,
                anchor=anchor,
            )
        )
    )
    return path


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    properties: dict[str, str] | None = None,
    systemctl_exit_code: int = 0,
    systemctl_path: str | None = None,
    failing_subcommand: str = "",
    receipt: Path | None = None,
    receipt_root: Path | None = None,
    thresholds: dict[str, str] | None = None,
    environment: dict[str, str] | None = None,
    now: datetime | None = NOW,
    extra_argv: list[str] | None = None,
) -> tuple[int, Path, Path]:
    """Run the probe end-to-end; return (exit status, receipt root, call log).

    ``now=None`` omits ``--now`` entirely, so the probe runs on its real clock
    -- the only shape the shipped unit ever uses, since the clock has no
    environment seam (B1 / design D4).
    """
    script, log = _write_fake_systemctl(
        tmp_path,
        properties=properties if properties is not None else _properties(),
        exit_code=systemctl_exit_code,
        failing_subcommand=failing_subcommand,
    )
    monkeypatch.setenv(
        probe.ENV_SYSTEMCTL, systemctl_path if systemctl_path is not None else str(script)
    )
    for name in (
        probe.ENV_MAX_NEXT_DWELL_HOURS,
        probe.ENV_MAX_MANIFEST_AGE_HOURS,
        probe.ENV_STOPPED_DWELL_HOURS,
        probe.ENV_UNIT,
        probe.ENV_REFRESH_RECEIPT,
        probe.ENV_HEALTH_RECEIPT_ROOT,
        probe.ENV_JSON,
    ):
        monkeypatch.delenv(name, raising=False)
    for key, value in {**(thresholds or {}), **(environment or {})}.items():
        monkeypatch.setenv(key, value)
    root = receipt_root if receipt_root is not None else tmp_path / "health-receipts"
    refresh_receipt = receipt if receipt is not None else _write_refresh_receipt(tmp_path)
    argv = [
        "--unit",
        UNIT,
        "--refresh-receipt",
        str(refresh_receipt),
        "--health-receipt-root",
        str(root),
        *([] if now is None else ["--now", now.isoformat().replace("+00:00", "Z")]),
        *(extra_argv or []),
    ]
    return probe.main(argv), root, log


def _verdict(root: Path) -> str:
    return json.loads((root / "latest.json").read_text())["verdict"]


# ---------------------------------------------------------------------------
# R1 -- the healthy lane
# ---------------------------------------------------------------------------


def test_r1_enabled_active_scheduled_and_fresh_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, root, _log = _run(tmp_path, monkeypatch)

    assert status == 0
    assert _verdict(root) == "ok"


# ---------------------------------------------------------------------------
# R2 / R2b / R3 -- the stopped-vs-not-enabled split and the operator dwell
# ---------------------------------------------------------------------------


def test_r2_enabled_but_inactive_past_the_dwell_is_timer_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-08-28 geometry: enabled facade, six days idle, no tick coming."""
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        properties=_properties(
            active_state="inactive",
            sub_state="dead",
            inactive_enter=_systemd_timestamp(NOW - timedelta(days=6)),
            next_elapse="",
        ),
    )

    assert status != 0
    assert _verdict(root) == "timer_stopped"


@pytest.mark.parametrize("active_state", ["inactive", "active"])
def test_r2b_a_not_enabled_timer_is_never_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active_state: str
) -> None:
    """`disabled` is the refresh installer's own terminal state after
    `--install` and `--rollback`; it must never grade healthy.

    The `inactive` case is inside the stopped-dwell on purpose, so the only
    verdict that can catch it is `timer_not_enabled`.
    """
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        properties=_properties(
            unit_file_state="disabled",
            active_state=active_state,
            inactive_enter=_systemd_timestamp(NOW - timedelta(minutes=30)),
        ),
    )

    assert status != 0
    assert _verdict(root) == "timer_not_enabled"


def test_r3_live_manual_publisher_window_inside_the_dwell_does_not_alarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1104 stop/publish/start window: 30 minutes idle is routine, not a fault."""
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        properties=_properties(
            active_state="inactive",
            sub_state="dead",
            inactive_enter=_systemd_timestamp(NOW - timedelta(minutes=30)),
            next_elapse="",
        ),
    )

    assert status == 0
    assert _verdict(root) == "ok"


def test_r3_inside_the_dwell_a_stale_manifest_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inside the dwell the lane is graded on its remaining signals, never
    short-circuited to `ok`."""
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        properties=_properties(
            active_state="inactive",
            sub_state="dead",
            inactive_enter=_systemd_timestamp(NOW - timedelta(minutes=30)),
            next_elapse="",
        ),
        receipt=_write_refresh_receipt(tmp_path, manifest_age_hours=130.0),
    )

    assert status != 0
    assert _verdict(root) == "manifest_stale"


# ---------------------------------------------------------------------------
# R4 / R5 -- an active timer with no usable tick
# ---------------------------------------------------------------------------


def test_r4_active_timer_with_empty_next_elapse_is_not_scheduled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, root, _log = _run(
        tmp_path, monkeypatch, properties=_properties(next_elapse="")
    )

    assert status != 0
    assert _verdict(root) == "timer_not_scheduled"


def test_r5_active_timer_with_next_beyond_the_dwell_is_not_scheduled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        properties=_properties(next_elapse=_systemd_timestamp(NOW + timedelta(hours=40))),
    )

    assert status != 0
    assert _verdict(root) == "timer_not_scheduled"


# ---------------------------------------------------------------------------
# R6 / R7 / R7b -- manifest freshness and precedence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("age_hours", [120.0, 150.0, 167.9])
def test_r6_manifest_between_the_threshold_and_the_consumer_bound_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, age_hours: float
) -> None:
    """At-or-over, never strictly-greater: 120 h exactly is already a finding."""
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        receipt=_write_refresh_receipt(tmp_path, manifest_age_hours=age_hours),
    )

    assert status != 0
    assert _verdict(root) == "manifest_stale"


@pytest.mark.parametrize("age_hours", [168.0, 400.0])
def test_r7_manifest_at_or_past_the_consumer_bound_is_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, age_hours: float
) -> None:
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        receipt=_write_refresh_receipt(tmp_path, manifest_age_hours=age_hours),
    )

    assert status != 0
    assert _verdict(root) == "manifest_expired"


def test_r7b_expired_manifest_outranks_a_stopped_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First-match-wins is asserted, not left to the implementation."""
    stopped = _properties(
        active_state="inactive",
        sub_state="dead",
        inactive_enter=_systemd_timestamp(NOW - timedelta(days=6)),
        next_elapse="",
    )
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        properties=stopped,
        receipt=_write_refresh_receipt(tmp_path, manifest_age_hours=200.0),
    )

    assert status != 0
    assert _verdict(root) == "manifest_expired"


def test_r7b_a_stopped_timer_outranks_a_merely_stale_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = _properties(
        active_state="inactive",
        sub_state="dead",
        inactive_enter=_systemd_timestamp(NOW - timedelta(days=6)),
        next_elapse="",
    )
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        properties=stopped,
        receipt=_write_refresh_receipt(tmp_path, manifest_age_hours=130.0),
    )

    assert status != 0
    assert _verdict(root) == "timer_stopped"


# ---------------------------------------------------------------------------
# R8 / R8b / R9 -- fail closed on unreadable or undefined evidence
# ---------------------------------------------------------------------------


def test_r8_missing_systemctl_binary_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, root, _log = _run(
        tmp_path, monkeypatch, systemctl_path=str(tmp_path / "no-such-systemctl")
    )

    assert status != 0
    assert _verdict(root) == "probe_failed"


def test_r8_systemctl_exiting_non_zero_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, root, _log = _run(tmp_path, monkeypatch, systemctl_exit_code=3)

    assert status != 0
    assert _verdict(root) == "probe_failed"


@pytest.mark.parametrize("inactive_enter", ["", "n/a", "not-a-timestamp"])
def test_r8b_inactive_without_a_parseable_instant_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inactive_enter: str
) -> None:
    """The dwell arithmetic is undefined; fail closed rather than guess."""
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        properties=_properties(
            active_state="inactive", sub_state="dead", inactive_enter=inactive_enter
        ),
    )

    assert status != 0
    assert _verdict(root) == "probe_failed"


def test_r9c_missing_refresh_receipt_with_no_history_is_manifest_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unresolvable manifest is its OWN verdict at precedence 7, never the
    generic `probe_failed` at precedence 1 (design D3 / D3b)."""
    status, root, _log = _run(tmp_path, monkeypatch, receipt=tmp_path / "absent.json")

    assert status != 0
    assert _verdict(root) == "manifest_unavailable"


def test_r9c_malformed_refresh_receipt_with_no_history_is_manifest_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, root, _log = _run(
        tmp_path, monkeypatch, receipt=_write_refresh_receipt(tmp_path, raw="{not json")
    )

    assert status != 0
    assert _verdict(root) == "manifest_unavailable"


@pytest.mark.parametrize(
    "receipt_kwargs",
    [
        pytest.param({"schema_version": "nhms.something.else.v9"}, id="wrong_schema"),
        pytest.param({"providers": []}, id="no_registry_provider"),
        pytest.param(
            {"providers": [{"name": "registry", "after_generated_at": "not-a-time"}]},
            id="unparseable_generated_at",
        ),
        pytest.param(
            {"providers": [{"name": "registry", "after_generated_at": "2026-09-01T00:00:00"}]},
            id="naive_generated_at",
        ),
        pytest.param({"providers": [{"name": "registry"}]}, id="missing_generated_at"),
        pytest.param({"raw": "[]"}, id="not_an_object"),
    ],
)
def test_r9c_schema_invalid_refresh_receipt_is_manifest_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt_kwargs: dict[str, object]
) -> None:
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        receipt=_write_refresh_receipt(tmp_path, **receipt_kwargs),  # type: ignore[arg-type]
    )

    assert status != 0
    assert _verdict(root) == "manifest_unavailable"


def test_r9c_a_failed_refresh_receipt_with_no_providers_is_manifest_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `failed` refresh writes `providers: []`.  With no history to fall back
    on there is no manifest age to grade, so the probe says exactly that --
    and it still says it at precedence 7, below every timer verdict."""
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        receipt=_write_refresh_receipt(tmp_path, providers=[]),
    )

    assert status != 0
    assert _verdict(root) == "manifest_unavailable"


def test_an_unresolvable_manifest_still_records_the_systemd_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest signal and the systemd signals are collected
    independently, so an unreadable receipt does not blank the receipt's
    systemd fields."""
    status, root, _log = _run(tmp_path, monkeypatch, receipt=tmp_path / "absent.json")
    receipt = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert receipt["verdict"] == "manifest_unavailable"
    assert receipt["unit_file_state"] == "enabled"
    assert receipt["active_state"] == "active"
    assert receipt["manifest_age_hours"] is None
    assert receipt["manifest_source"] == "unavailable"


def test_probe_failed_records_the_manifest_age_when_systemd_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, root, _log = _run(tmp_path, monkeypatch, systemctl_exit_code=3)
    receipt = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert receipt["verdict"] == "probe_failed"
    assert receipt["manifest_age_hours"] == pytest.approx(5.0)
    assert receipt["manifest_source"] == "latest"
    assert receipt["unit_file_state"] == ""


# A3: `list-timers` is a NON-GRADED read of the same timer subsystem.  It runs
# after `show` has already answered, so its failure must not blank the
# receipt's systemd fields -- the operator still needs the four signals that
# WERE readable.  The verdict stays `probe_failed`: a timer subsystem that
# cannot answer `list-timers` is unreadable evidence (R8).
def test_a3_a_failing_list_timers_call_does_not_blank_the_show_properties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, root, log = _run(tmp_path, monkeypatch, failing_subcommand="list-timers")
    receipt = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert receipt["verdict"] == "probe_failed"
    assert receipt["unit_file_state"] == "enabled"
    assert receipt["active_state"] == "active"
    assert receipt["sub_state"] == "waiting"
    assert receipt["next_elapse"] != ""
    # Both read-only subcommands were still attempted.
    assert "list-timers" in log.read_text()


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
# R15 -- the installer never touches the four protected units
# ---------------------------------------------------------------------------


INSTALLER = Path(__file__).resolve().parents[1] / "scripts" / "install_node22_refresh_timer_health.sh"
PROTECTED_UNITS = (
    "nhms-compute-scheduler.timer",
    "nhms-compute-scheduler.service",
    "nhms-scheduler-file-provider-refresh.timer",
    "nhms-scheduler-file-provider-refresh.service",
)
PROBE_UNITS = (
    "nhms-node22-refresh-timer-health.service",
    "nhms-node22-refresh-timer-health.timer",
)


def _installer_fake_systemctl(tmp_path: Path) -> tuple[Path, Path]:
    """A fake systemctl that answers the read-only queries and logs mutations.

    Two capabilities the R15 regression needs (C2):

    * the probe's OWN units carry real state, so an `enable --now` followed by a
      restoring `disable`/`stop` is observable rather than a no-op;
    * ``NHMS_FAKE_DIVERGE`` / ``NHMS_FAKE_DIVERGE_VALUE`` make ONE
      ``<unit>.<query>`` pair answer differently from its SECOND call onwards,
      which is exactly the "the protected units moved under us" shape
      `assert_protected_unchanged` exists to catch;
    * ``NHMS_FAKE_FAIL_VERB`` makes every call of one mutating verb exit 1
      without changing state, which reaches an action's ERR trap BEFORE its
      main protected-unit assertion -- so the trap's own assertion is the only
      protected re-read the invocation makes;
    * ``NHMS_FAKE_PROBE_IS_ENABLED`` / ``NHMS_FAKE_PROBE_IS_ACTIVE``, when SET
      (the empty string included), are the literal answer to that query for
      both probe units -- the states a real user manager reports that the
      stateful fake never produces (`static`, `not-found`, `failed`, no answer);
    * ``NHMS_FAKE_PROBE_TIMER_IS_*`` / ``NHMS_FAKE_PROBE_SERVICE_IS_*`` do the
      same for ONE probe unit and win over the shared knob, so the timer and
      the service can answer differently.
    """
    log = tmp_path / "installer-systemctl.log"
    state = tmp_path / "fake-state"
    state.mkdir(exist_ok=True)
    script = tmp_path / "fake-systemctl"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        f'state={state}\n'
        "shift\n"  # drop --user
        "verb=$1\n"
        "shift\n"
        '[ "$verb" = "${NHMS_FAKE_FAIL_VERB:-}" ] && exit 1\n'
        "now=no\n"
        "unit=\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        "    --now) now=yes ;;\n"
        "    -*) ;;\n"
        "    *) unit=$arg ;;\n"
        "  esac\n"
        "done\n"
        'enabled=$(cat "$state/probe.enabled" 2>/dev/null || printf disabled)\n'
        'active=$(cat "$state/probe.active" 2>/dev/null || printf inactive)\n'
        "case \"$verb\" in\n"
        '  enable) enabled=enabled; [ "$now" = yes ] && active=active ;;\n'
        '  disable) enabled=disabled; [ "$now" = yes ] && active=inactive ;;\n'
        "  start) active=active ;;\n"
        "  stop) active=inactive ;;\n"
        "esac\n"
        'printf "%s" "$enabled" > "$state/probe.enabled"\n'
        'printf "%s" "$active" > "$state/probe.active"\n'
        'case "$verb" in\n'
        "  is-enabled|is-active) ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
        'case "$unit" in\n'
        "  nhms-node22-refresh-timer-health.*)\n"
        r"""    kind=SERVICE; case "$unit" in *.timer) kind=TIMER ;; esac
    query=IS_ENABLED; [ "$verb" = is-active ] && query=IS_ACTIVE
    for name in "NHMS_FAKE_PROBE_${kind}_${query}" "NHMS_FAKE_PROBE_${query}"; do
      eval "isset=\${$name+set}"
      if [ -n "$isset" ]; then eval "printf '%s\n' \"\$$name\""; exit 0; fi
    done
"""
        '    if [ "$verb" = is-enabled ]; then printf "%s\\n" "$enabled"; '
        'else printf "%s\\n" "$active"; fi\n'
        "    exit 0 ;;\n"
        "esac\n"
        'value=enabled\n'
        '[ "$verb" = is-active ] && value=active\n'
        'key=$(printf "%s" "$unit.$verb" | tr -c "a-zA-Z0-9._-" "_")\n'
        'count=$(cat "$state/$key" 2>/dev/null || printf 0)\n'
        'count=$((count + 1))\n'
        'printf "%s" "$count" > "$state/$key"\n'
        'if [ "$unit.$verb" = "${NHMS_FAKE_DIVERGE:-}" ] && [ "$count" -ge 2 ]; then\n'
        '  value=${NHMS_FAKE_DIVERGE_VALUE:-}\n'
        "fi\n"
        'printf "%s\\n" "$value"\n'
        "exit 0\n"
    )
    script.chmod(0o755)
    return script, log


def _run_installer(
    tmp_path: Path,
    action: str,
    *,
    diverge: str = "",
    diverge_value: str = "",
    fail_verb: str = "",
    probe_answers: dict[str, str] | None = None,
    timer_answers: dict[str, str] | None = None,
    service_answers: dict[str, str] | None = None,
    installer: Path = INSTALLER,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    script, log = _installer_fake_systemctl(tmp_path)
    # Divergence is scoped to ONE installer invocation: the per-(unit, query)
    # counters reset so the "before" snapshot is call 1 and the assertion's
    # re-read is call 2, which is the real mid-run flip being modelled.
    for counter in (tmp_path / "fake-state").iterdir():
        if counter.name not in {"probe.enabled", "probe.active"}:
            counter.unlink()
    environment = dict(os.environ)
    environment.update(
        {
            "NHMS_REFRESH_HEALTH_REPO": str(Path(__file__).resolve().parents[1]),
            "NHMS_REFRESH_HEALTH_UNIT_DIR": str(tmp_path / "units"),
            "NHMS_REFRESH_HEALTH_INSTALL_STATE_ROOT": str(tmp_path / "install-state"),
            "NHMS_REFRESH_HEALTH_RECEIPT_ROOT": str(tmp_path / "receipts"),
            "NHMS_REFRESH_HEALTH_SYSTEMCTL": str(script),
            "NHMS_FAKE_DIVERGE": diverge,
            "NHMS_FAKE_DIVERGE_VALUE": diverge_value,
            "NHMS_FAKE_FAIL_VERB": fail_verb,
        }
    )
    for scope in ("", "TIMER_", "SERVICE_"):
        for query in ("IS_ENABLED", "IS_ACTIVE"):
            environment.pop(f"NHMS_FAKE_PROBE_{scope}{query}", None)
    for scope, answers in (
        ("", probe_answers),
        ("TIMER_", timer_answers),
        ("SERVICE_", service_answers),
    ):
        for query, answer in (answers or {}).items():
            environment[f"NHMS_FAKE_PROBE_{scope}{query.upper().replace('-', '_')}"] = answer
    completed = subprocess.run(
        ["bash", str(installer), action],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    return completed, log


def _probe_timer_state(tmp_path: Path) -> tuple[str, str]:
    state = tmp_path / "fake-state"
    enabled = (state / "probe.enabled").read_text() if (state / "probe.enabled").exists() else "disabled"
    active = (state / "probe.active").read_text() if (state / "probe.active").exists() else "inactive"
    return enabled, active


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
# R11c -- the watchdog's own liveness, and the comment that used to lie about it
# ---------------------------------------------------------------------------

RUNBOOK = (
    Path(__file__).resolve().parents[1] / "docs" / "runbooks" / "current-production-ops.md"
)
PROBE_TIMER_UNIT = (
    Path(__file__).resolve().parents[1]
    / "infra"
    / "systemd"
    / "nhms-node22-refresh-timer-health.timer"
)


def _probe_runbook_section() -> str:
    """The probe's own section of the runbook, sliced by its headings."""
    text = RUNBOOK.read_text()
    start = text.index("##### refresh timer 健康探针")
    end = text.index("#### 3.1.4", start)
    return text[start:end]


def test_r11c_the_probe_timer_claims_no_self_catch_up() -> None:
    """`Persistent=` does NOT catch up a timer left `enabled` + `inactive`.

    It replays a missed tick only when the timer transitions to active -- boot,
    or an explicit `start`.  The unit used to comment that "a missed probe tick
    is caught up", which is the watchdog telling the operator it covers the one
    geometry it demonstrably does not.  A comment is what an operator reads
    when deciding whether the probe needs its own check, so a false one here is
    worse than none.
    """
    unit = PROBE_TIMER_UNIT.read_text()
    comments = "\n".join(
        line for line in unit.splitlines() if line.lstrip().startswith("#")
    )

    assert "Persistent=true" in unit
    for claim in (
        "a missed probe tick is caught up",
        "catches itself up",
        "catch itself up",
    ):
        assert claim not in comments, f"the unit still claims self-catch-up: {claim!r}"
    # The real precondition has to be stated where the directive is, or the
    # next reader re-derives the wrong one.
    assert "boot" in comments
    assert "`enabled` + `inactive`" in comments


def test_r11c_the_runbook_verdict_rows_5_and_7_name_what_grade_does() -> None:
    """Row 5 must name the unparseable-`NEXT` case `grade` routes there, and
    row 7 must name every timer verdict that an unresolvable manifest leaves
    unmasked -- each is what `grade` still returns with no manifest age."""
    section = _probe_runbook_section()

    def row(number: int) -> str:
        (line,) = [line for line in section.splitlines() if line.startswith(f"| {number} | ")]
        return line

    thresholds = probe.load_thresholds({})

    def graded(properties: dict[str, str], age: float | None) -> str:
        return probe.grade(
            now=NOW,
            properties=properties,
            manifest_age_hours=age,
            thresholds=thresholds,
            systemd_error=None,
        )

    unparseable = _properties(active_state="active", next_elapse="not-a-timestamp")
    assert graded(unparseable, 5.0) == probe.VERDICT_TIMER_NOT_SCHEDULED
    assert f"`{probe.VERDICT_TIMER_NOT_SCHEDULED}`" in row(5)
    assert "不可解析" in row(5)

    unmasked = {
        graded(properties, None)
        for properties in (
            _properties(
                active_state="inactive",
                sub_state="dead",
                inactive_enter=_systemd_timestamp(NOW - timedelta(days=6)),
                next_elapse="",
            ),
            _properties(unit_file_state="disabled"),
            unparseable,
        )
    }
    timer_verdicts = {
        value for name, value in vars(probe).items() if name.startswith("VERDICT_TIMER_")
    }
    assert unmasked == timer_verdicts
    assert f"`{probe.VERDICT_MANIFEST_UNAVAILABLE}`" in row(7)
    for verdict in timer_verdicts:
        assert f"`{verdict}`" in row(7), f"row 7 does not name {verdict}"


def test_r11c_the_runbook_carries_the_probe_timers_own_steady_state_row() -> None:
    """The probe timer gets the same steady-state check the refresh timer has.

    Reading `list-units --failed` reports a probe that ran and found something;
    it reports nothing about a probe that never ran.  These two columns are the
    only in-repo answer to "is the watchdog alive", so they are pinned to the
    unit name and the receipt path the probe actually writes.
    """
    section = _probe_runbook_section()

    assert (
        "systemctl --user list-timers nhms-node22-refresh-timer-health.timer --no-pager"
        in section
    )
    assert "generated_at" in section
    assert f"{probe.DEFAULT_HEALTH_RECEIPT_ROOT}/latest.json" in section
    # Mirrors the refresh timer's own table: `NEXT` must not be `-`.
    assert "`list-timers` 的 `NEXT`" in section


def test_r11c_the_runbook_states_the_probe_sections_verdicts_thresholds_and_fields() -> None:
    """P2: the runbook is the operator's copy of the probe's closed sets.

    Every verdict name, every threshold default AND ceiling, every receipt
    field and the receipt root are read from the MODULE and looked for in the
    section -- so renaming a verdict, retuning a default, adding a receipt field
    or moving the receipt root reds here instead of leaving an operator reading
    a document about a different program.
    """
    section = _probe_runbook_section()

    verdicts = {
        value
        for name, value in vars(probe).items()
        if name.startswith("VERDICT_") and isinstance(value, str)
    }
    assert len(verdicts) == 8
    for verdict in verdicts:
        assert f"`{verdict}`" in section, f"verdict {verdict} is undocumented"

    for env_name, default, ceiling in (
        (
            probe.ENV_MAX_NEXT_DWELL_HOURS,
            probe.DEFAULT_MAX_NEXT_DWELL_HOURS,
            probe.MAX_THRESHOLD_HOURS,
        ),
        (
            probe.ENV_MAX_MANIFEST_AGE_HOURS,
            probe.DEFAULT_MAX_MANIFEST_AGE_HOURS,
            probe.MAX_THRESHOLD_HOURS,
        ),
        (
            probe.ENV_STOPPED_DWELL_HOURS,
            probe.DEFAULT_STOPPED_DWELL_HOURS,
            probe.MAX_STOPPED_DWELL_HOURS,
        ),
    ):
        assert f"| `{env_name}` | {default} | {ceiling} |" in section, (
            f"{env_name} is not documented at default={default}, ceiling={ceiling}"
        )

    # The receipt's field set, taken from a real `build_receipt` call rather
    # than restated: a new field with no runbook line reds here.
    receipt = probe.build_receipt(
        now=NOW,
        unit=UNIT,
        verdict=probe.VERDICT_OK,
        properties=_properties(),
        manifest_age_hours=5.0,
        manifest_source=probe.MANIFEST_SOURCE_LATEST,
        thresholds=probe.load_thresholds({}),
    )
    for field in receipt:
        assert f"`{field}`" in section, f"receipt field {field} is undocumented"

    assert probe.DEFAULT_HEALTH_RECEIPT_ROOT in section


# ---------------------------------------------------------------------------
# B1 / R11b -- the clock has no environment seam at all
# ---------------------------------------------------------------------------


def test_b1_the_probe_reads_no_environment_variable_for_its_clock() -> None:
    """`--now` is CLI-only (design D4).

    An env default for the clock is a false-green vector: a pinned PAST instant
    grades the exact stopped geometry this change exists to catch as `ok`/exit
    0.  A flag that reads no environment cannot be poisoned by any environment,
    which is strictly better than an `UnsetEnvironment=` list to maintain.
    """
    assert not hasattr(probe, "ENV_NOW")
    assert "NHMS_REFRESH_HEALTH_NOW" not in PROBE_SOURCE.read_text()


# The complete environment surface, enumerated HERE rather than derived from
# the module, so the test below is double-sided: one half reads what the source
# actually looks up, the other reads what the module declares, and this literal
# table is the third party both are compared against.  Deriving `known` from
# `vars(probe)` alone would be single-sided -- adding `ENV_CLOCK` and reading it
# would satisfy it.
PROBE_ENV_SURFACE = {
    "ENV_UNIT": "NHMS_REFRESH_HEALTH_UNIT",
    "ENV_SYSTEMCTL": "NHMS_REFRESH_HEALTH_SYSTEMCTL",
    "ENV_REFRESH_RECEIPT": "NHMS_REFRESH_HEALTH_REFRESH_RECEIPT",
    "ENV_HEALTH_RECEIPT_ROOT": "NHMS_REFRESH_HEALTH_RECEIPT_ROOT",
    "ENV_JSON": "NHMS_REFRESH_HEALTH_JSON",
    "ENV_MAX_NEXT_DWELL_HOURS": "NHMS_REFRESH_HEALTH_MAX_NEXT_DWELL_HOURS",
    "ENV_MAX_MANIFEST_AGE_HOURS": "NHMS_REFRESH_HEALTH_MAX_MANIFEST_AGE_HOURS",
    "ENV_STOPPED_DWELL_HOURS": "NHMS_REFRESH_HEALTH_STOPPED_DWELL_HOURS",
}


def _environment_keys_read_by(source: str) -> set[str]:
    """Every environment key the probe's source actually looks up.

    Walks `os.environ[...]`, `os.environ.get(...)`, `os.getenv(...)` **and any
    name bound from `os.environ`** -- `load_thresholds` reads its three
    thresholds through the local alias `source = os.environ if env is None else
    env`, so a walk that only knows the literal `os.environ` spelling finds
    five of eight and would call an unlisted threshold seam clean.

    The `os` module and its `environ`/`getenv` members are resolved through
    every import spelling -- `import os as _o`, `from os import environ as _e`,
    `from os import getenv` -- because a walk that only knows the literal
    `os.environ` spelling calls a ninth seam behind `from os import environ as
    _env` clean.  `test_r11b_the_probe_binds_os_only_through_a_bare_import`
    additionally forbids those spellings outright, so a form this walk still
    does not model has to get past both.

    Keys are `ast.Name` nodes, not string literals, so each is resolved back
    through the module's own constant -- which is the point: the source and the
    declared surface have to agree.
    """
    tree = ast.parse(source)

    os_names: set[str] = set()
    aliases: set[str] = set()
    getenv_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name == "os":
                    os_names.add(imported.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            for imported in node.names:
                if imported.name == "environ":
                    aliases.add(imported.asname or imported.name)
                elif imported.name == "getenv":
                    getenv_names.add(imported.asname or imported.name)

    def _is_os_environ(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id in os_names
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(_is_os_environ(child) for child in ast.walk(node.value)):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                aliases.add(target.id)

    def _is_environ_source(node: ast.AST) -> bool:
        return _is_os_environ(node) or (isinstance(node, ast.Name) and node.id in aliases)

    key_nodes: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and _is_environ_source(node.value):
            key_nodes.append(node.slice)
        elif isinstance(node, ast.Call):
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "get"
                and _is_environ_source(function.value)
                and node.args
            ):
                key_nodes.append(node.args[0])
            elif (
                isinstance(function, ast.Attribute)
                and function.attr == "getenv"
                and isinstance(function.value, ast.Name)
                and function.value.id in os_names
                and node.args
            ) or (
                isinstance(function, ast.Name) and function.id in getenv_names and node.args
            ):
                key_nodes.append(node.args[0])

    keys: set[str] = set()
    for key in key_nodes:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            keys.add(key.value)
        elif isinstance(key, ast.Name):
            resolved = getattr(probe, key.id, None)
            assert isinstance(resolved, str), (
                f"the probe reads the environment through `{key.id}`, which is not "
                "a module-level string constant -- the surface cannot be enumerated"
            )
            keys.add(resolved)
        else:
            raise AssertionError(
                f"unresolvable environment key expression at line {key.lineno}: "
                f"{ast.dump(key)}"
            )
    return keys


def test_r11b_the_probe_binds_os_only_through_a_bare_import() -> None:
    """The enumeration above can only see names it knows are bound to the
    environment.  Pin the binding itself: exactly one `import os`, no alias, and
    no `from os import ...` anywhere in the file (function scope included), so
    an environment read cannot hide behind an import spelling."""
    tree = ast.parse(PROBE_SOURCE.read_text())
    os_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for imported in node.names:
                if imported.name == "os" or imported.name.startswith("os."):
                    os_imports.append(ast.unparse(node))
        elif isinstance(node, ast.ImportFrom) and (
            node.module == "os" or (node.module or "").startswith("os.")
        ):
            os_imports.append(ast.unparse(node))

    assert os_imports == ["import os"], os_imports


def test_r11b_the_probe_reads_exactly_the_enumerated_environment_surface() -> None:
    """B1's real claim: not "no `ENV_NOW`", but "nothing beyond these eight".

    The previous clock test asserted `not hasattr(probe, "ENV_NOW")` plus three
    guessed names, so adding `ENV_CLOCK = "NHMS_PROBE_AT"` and honouring it
    passed everything.  This reads BOTH sides -- what the source looks up, and
    what the module declares -- against one enumerated table, so a ninth seam
    reds here whatever it is called.
    """
    read = _environment_keys_read_by(PROBE_SOURCE.read_text())
    declared = {name for name in vars(probe) if name.startswith("ENV_")}

    assert read == set(PROBE_ENV_SURFACE.values())
    assert declared == set(PROBE_ENV_SURFACE)
    for name, value in PROBE_ENV_SURFACE.items():
        assert getattr(probe, name) == value
    # Every declared constant is also actually read: a dead `ENV_*` constant is
    # a seam someone will wire up later without touching this table.
    assert {getattr(probe, name) for name in declared} == read


@pytest.mark.parametrize(
    "poison_name",
    [
        "NHMS_REFRESH_HEALTH_NOW",
        "NHMS_REFRESH_HEALTH_CLOCK",
        "NHMS_REFRESH_HEALTH_UTC_NOW",
    ],
)
def test_b1_a_poisoned_clock_environment_cannot_grade_a_stopped_timer_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, poison_name: str
) -> None:
    """A pinned-PAST clock in the environment + the 08-28 stopped geometry.

    Run with NO `--now`, so the only clock that could be inherited is an env
    one.  The verdict must still be `timer_stopped`; if any env name were
    honoured the pinned instant would sit inside the dwell and grade `ok`.
    """
    real_now = datetime.now(UTC)
    became_inactive = real_now - timedelta(days=6)
    poisoned = (became_inactive + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")

    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        properties=_properties(
            active_state="inactive",
            sub_state="dead",
            inactive_enter=_systemd_timestamp(became_inactive),
            next_elapse="",
        ),
        receipt=_write_refresh_receipt(tmp_path, manifest_age_hours=1.0, anchor=real_now),
        environment={poison_name: poisoned},
        now=None,
    )

    assert status != 0
    assert _verdict(root) == "timer_stopped"


def test_r11b_the_production_invocation_shape_resolves_unit_receipt_and_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped unit runs `python node22_refresh_timer_health.py` with NO CLI
    flags: every input comes from the environment except the clock, which comes
    from nowhere but the system clock.  This drives that exact shape.
    """
    real_now = datetime.now(UTC)
    script, log = _write_fake_systemctl(
        tmp_path,
        properties=_properties(
            inactive_enter=_systemd_timestamp(real_now - timedelta(days=15)),
            next_elapse=_systemd_timestamp(real_now + timedelta(hours=11)),
            last_trigger=_systemd_timestamp(real_now - timedelta(hours=13)),
        ),
    )
    refresh_receipt = _write_refresh_receipt(
        tmp_path, manifest_age_hours=5.0, anchor=real_now
    )
    root = tmp_path / "production-shape-receipts"
    monkeypatch.setenv(probe.ENV_SYSTEMCTL, str(script))
    monkeypatch.setenv(probe.ENV_UNIT, UNIT)
    monkeypatch.setenv(probe.ENV_REFRESH_RECEIPT, str(refresh_receipt))
    monkeypatch.setenv(probe.ENV_HEALTH_RECEIPT_ROOT, str(root))
    monkeypatch.setenv(probe.ENV_MAX_MANIFEST_AGE_HOURS, "96")
    monkeypatch.delenv(probe.ENV_MAX_NEXT_DWELL_HOURS, raising=False)
    monkeypatch.delenv(probe.ENV_STOPPED_DWELL_HOURS, raising=False)
    monkeypatch.delenv(probe.ENV_JSON, raising=False)

    status = probe.main([])
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    # Unit resolved from the env, and actually queried.
    assert payload["unit"] == UNIT
    assert UNIT in log.read_text()
    # Receipt root resolved from the env.
    assert (root / "latest.json").exists()
    # Refresh receipt resolved from the env.
    assert payload["manifest_source"] == probe.MANIFEST_SOURCE_LATEST
    assert payload["manifest_age_hours"] == pytest.approx(5.0, abs=0.01)
    # Threshold resolved from the env, defaults for the two not set.
    assert payload["max_manifest_age_hours"] == 96
    assert payload["max_next_dwell_hours"] == probe.DEFAULT_MAX_NEXT_DWELL_HOURS
    assert payload["stopped_dwell_hours"] == probe.DEFAULT_STOPPED_DWELL_HOURS
    # Clock: the real one, because there is no seam for anything else.
    generated = datetime.fromisoformat(payload["generated_at"].replace("Z", "+00:00"))
    assert abs((generated - datetime.now(UTC)).total_seconds()) < 120


# ---------------------------------------------------------------------------
# R4b -- a PRESENT but unparseable next elapse
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "next_elapse",
    [
        pytest.param("Sun 2026-13-45 99:99:99 UTC", id="out_of_range_fields"),
        pytest.param("tomorrow-ish", id="not_a_timestamp_at_all"),
        pytest.param("Sun 2026-09-13 10:23 CST", id="truncated_time"),
    ],
)
def test_r4b_active_timer_with_an_unparseable_next_elapse_is_not_scheduled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, next_elapse: str
) -> None:
    """A distinct branch from R4's empty value: present-but-garbage must not
    fall through to healthy just because it is not the empty string."""
    status, root, _log = _run(
        tmp_path, monkeypatch, properties=_properties(next_elapse=next_elapse)
    )

    assert status != 0
    assert _verdict(root) == "timer_not_scheduled"


# ---------------------------------------------------------------------------
# D2 (test strength) -- grading ORDER, not just the cross-product
# ---------------------------------------------------------------------------


def test_a_disabled_timer_with_a_stale_manifest_reports_timer_not_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`timer_not_enabled` (4) outranks `manifest_stale` (6).

    A permutation of those two rows produces the same non-zero exit, so the
    cross-product scan cannot catch it -- only a case that pins WHICH verdict
    wins can.  The actionable fact is the dead timer, not the symptom.
    """
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        properties=_properties(unit_file_state="disabled"),
        receipt=_write_refresh_receipt(tmp_path, manifest_age_hours=130.0),
    )

    assert status != 0
    assert _verdict(root) == "timer_not_enabled"


# ---------------------------------------------------------------------------
# R8c -- zone-token handling, asserted independently of the test host's zone
# ---------------------------------------------------------------------------

# `systemctl show --timestamp=utc` is deliberately NOT adopted: measured on
# node-22 (systemd 255, Asia/Shanghai) it converts `InactiveEnterTimestamp` but
# leaves `NextElapseUSecRealtime` in local time, so it produces a MIXED-zone
# surface rather than a uniform one.  The zone-token parser below handles both.


@contextmanager
def _host_zone(name: str) -> Iterator[None]:
    """Pin the process's local zone, so the assertion does not depend on the
    zone the test host happens to be in."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


@pytest.mark.parametrize("host_zone", ["Asia/Shanghai", "America/New_York", "UTC"])
@pytest.mark.parametrize("token", ["UTC", "GMT"])
def test_r8c_a_utc_token_is_read_as_utc_on_any_host(host_zone: str, token: str) -> None:
    with _host_zone(host_zone):
        parsed = probe.parse_systemd_timestamp(f"Fri 2026-08-28 00:11:18 {token}")

    assert parsed == datetime(2026, 8, 28, 0, 11, 18, tzinfo=UTC)


def test_r8c_a_non_local_zone_abbreviation_is_read_as_the_emitting_hosts_zone() -> None:
    """`CST` is what systemd prints on node-22 (Asia/Shanghai).

    `%Z` in `strptime` only accepts the RUNNING machine's abbreviations, so the
    probe reads any non-UTC token as the local zone instead -- which is what
    systemd emitted.  Both instants below are hardcoded, not derived from the
    test host.
    """
    string = "Fri 2026-08-28 00:11:18 CST"

    with _host_zone("Asia/Shanghai"):
        shanghai = probe.parse_systemd_timestamp(string)
    with _host_zone("America/New_York"):
        new_york = probe.parse_systemd_timestamp(string)

    assert shanghai == datetime(2026, 8, 27, 16, 11, 18, tzinfo=UTC)
    assert new_york == datetime(2026, 8, 28, 4, 11, 18, tzinfo=UTC)


def test_r8c_a_hardcoded_stopped_geometry_grades_the_same_in_any_host_zone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live 08-28 capture, verbatim, with a `--now` pinned in UTC."""
    properties = {
        "UnitFileState": "enabled",
        "ActiveState": "inactive",
        "SubState": "dead",
        "InactiveEnterTimestamp": "Fri 2026-08-28 00:11:18 CST",
        "NextElapseUSecRealtime": "",
        "LastTriggerUSec": "Sat 2026-09-12 10:35:58 CST",
    }
    with _host_zone("Asia/Shanghai"):
        status, root, _log = _run(
            tmp_path,
            monkeypatch,
            properties=properties,
            now=datetime(2026, 9, 12, 15, 34, tzinfo=UTC),
            receipt=_write_refresh_receipt(tmp_path, manifest_age_hours=5.0),
        )

    assert status != 0
    assert _verdict(root) == "timer_stopped"


# ---------------------------------------------------------------------------
# R10 -- the THIRD tunable is bounded too (B2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["168", "200", "10000"])
def test_r10_a_stopped_dwell_at_or_over_the_consumer_bound_is_rejected(value: str) -> None:
    """All three tunables are bounded, not two.

    A stopped-dwell at or beyond 168 h means a stopped timer can never be
    reported inside the consumer's whole freshness budget -- the green facade
    this change exists to close, reachable through a drop-in.  These values are
    refused a fortiori now that the ceiling is one refresh cadence; the case is
    kept because the consumer-bound values are the measured regression.
    """
    with pytest.raises(probe.ConfigError):
        probe.load_thresholds({probe.ENV_STOPPED_DWELL_HOURS: value})


def test_r10_an_out_of_bounds_stopped_dwell_writes_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, root, _log = _run(
        tmp_path, monkeypatch, thresholds={probe.ENV_STOPPED_DWELL_HOURS: "168"}
    )

    assert status != 0
    assert not (root / "latest.json").exists()


# ---------------------------------------------------------------------------
# B3 -- the probe's own unit ships no env file
# ---------------------------------------------------------------------------


def test_b3_the_probe_service_unit_carries_no_environment_file() -> None:
    service = (
        Path(__file__).resolve().parents[1]
        / "infra/systemd/nhms-node22-refresh-timer-health.service"
    ).read_text()

    assert not any(line.strip().startswith("EnvironmentFile") for line in service.splitlines())


# ---------------------------------------------------------------------------
# R9 / R9b / R9c / R9d -- the bounded history fallback (design D3b)
# ---------------------------------------------------------------------------


def _receipt_root(tmp_path: Path) -> Path:
    root = tmp_path / "provider-refresh" / "receipts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _history_root(tmp_path: Path) -> Path:
    history = _receipt_root(tmp_path) / "history"
    history.mkdir(parents=True, exist_ok=True)
    return history


def _history_name(hour: int, marker: int = 0) -> str:
    """The runner's own name shape (`scheduler_file_provider_refresh.py:611`):
    ``refresh_<YYYYmmddTHHMMSSZ>_<uuid12>.json``."""
    return f"refresh_202609{11:02d}T{hour:02d}0000Z_{marker:012x}.json"


def _write_history_receipt(
    history: Path,
    name: str,
    *,
    manifest_age_hours: float | None = 20.0,
    raw: str | None = None,
    outcome: str = "published",
    providers: list[dict[str, object]] | None = None,
) -> Path:
    path = history / name
    if raw is not None:
        path.write_text(raw)
        return path
    path.write_text(
        json.dumps(
            _refresh_receipt_payload(
                manifest_age_hours=manifest_age_hours,
                providers=providers,
                outcome=outcome,
            )
        )
    )
    return path


def _unresolvable_latest(tmp_path: Path, shape: str) -> Path:
    """Every way the configured `latest.json` can fail to yield a manifest age.

    All four are shapes the live lane reaches: `latest.json` is overwritten by
    EVERY run, including one that fails before assembling a provider list.
    """
    latest = _receipt_root(tmp_path) / "latest.json"
    if shape == "missing":
        return latest
    if shape == "malformed":
        latest.write_text("{not json")
        return latest
    if shape == "schema_invalid":
        latest.write_text(json.dumps({"schema_version": "nhms.other.v1", "providers": []}))
        return latest
    if shape == "empty_providers":
        latest.write_text(
            json.dumps(_refresh_receipt_payload(providers=[], outcome="failed"))
        )
        return latest
    raise AssertionError(f"unknown shape {shape!r}")


UNRESOLVABLE_SHAPES = ["missing", "malformed", "schema_invalid", "empty_providers"]


@pytest.mark.parametrize("shape", UNRESOLVABLE_SHAPES)
def test_r9_history_answers_when_latest_cannot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """A failed rehearsal receipt no longer produces an alarm at all: the
    previous receipt in the runner's own history still answers the question."""
    latest = _unresolvable_latest(tmp_path, shape)
    history = _history_root(tmp_path)
    name = _history_name(10, 1)
    _write_history_receipt(history, name, manifest_age_hours=20.0)

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    assert payload["verdict"] == "ok"
    assert payload["manifest_source"] == f"history:{name}"
    assert payload["manifest_age_hours"] == pytest.approx(20.0)


def test_r9_a_dry_run_history_receipt_is_an_ordinary_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `dry_run` history receipt answers with its `after_generated_at`.

    `dry_run` is one of the three outcomes whose `after_generated_at` the
    probe trusts (design D3b).  The fixture carries no `before_generated_at`,
    so an answer at all proves `after_generated_at` was the field read.
    """
    latest = _unresolvable_latest(tmp_path, "empty_providers")
    history = _history_root(tmp_path)
    name = _history_name(9, 2)
    _write_history_receipt(history, name, manifest_age_hours=11.0, outcome="dry_run")

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    assert payload["manifest_source"] == f"history:{name}"
    assert payload["manifest_age_hours"] == pytest.approx(11.0)


# Design D3b, restated here rather than read from the probe: the outcomes whose
# registry `after_generated_at` the probe trusts.
DESIGN_TRUSTED_AFTER_OUTCOMES = frozenset({"published", "published_receipt_failed", "dry_run"})


def _registry_evidence(
    *, after_hours: float | None, before_hours: float | None
) -> list[dict[str, object]]:
    registry: dict[str, object] = {"name": "registry", "entry_count": 18}
    for field, hours in (("after_generated_at", after_hours), ("before_generated_at", before_hours)):
        if hours is not None:
            registry[field] = (NOW - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
    return [registry]


@pytest.mark.parametrize("where", ["latest", "history"])
def test_r9e_a_replace_uncertain_receipt_never_grades_ok_from_its_after_generated_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """A `replace_uncertain` receipt can carry a fresh registry
    `after_generated_at` for bytes that were rolled back
    (`test_replace_uncertain_receipt_carries_after_evidence_for_restored_registry_bytes`
    in the runner suite).  Fresh after (1 h), stale before (150 h): the probe
    answers with before, from `latest.json` and from a history candidate alike.
    """
    providers = _registry_evidence(after_hours=1.0, before_hours=150.0)
    if where == "latest":
        latest = _receipt_root(tmp_path) / "latest.json"
        latest.write_text(
            json.dumps(_refresh_receipt_payload(providers=providers, outcome="replace_uncertain"))
        )
        expected_source = "latest"
    else:
        latest = _unresolvable_latest(tmp_path, "empty_providers")
        name = _history_name(10, 1)
        _write_history_receipt(
            _history_root(tmp_path), name, providers=providers, outcome="replace_uncertain"
        )
        expected_source = f"history:{name}"

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert payload["verdict"] == "manifest_stale"
    assert payload["manifest_source"] == expected_source
    assert payload["manifest_age_hours"] == pytest.approx(150.0)


def test_r9e_a_replace_uncertain_latest_without_before_generated_at_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `before_generated_at` on an untrusted outcome: the candidate is
    unresolvable, its fresh `after_generated_at` is ignored, and history answers."""
    latest = _receipt_root(tmp_path) / "latest.json"
    latest.write_text(
        json.dumps(
            _refresh_receipt_payload(
                providers=_registry_evidence(after_hours=1.0, before_hours=None),
                outcome="replace_uncertain",
            )
        )
    )
    name = _history_name(10, 1)
    _write_history_receipt(_history_root(tmp_path), name, manifest_age_hours=20.0)

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    assert payload["verdict"] == "ok"
    assert payload["manifest_source"] == f"history:{name}"
    assert payload["manifest_age_hours"] == pytest.approx(20.0)


@pytest.mark.parametrize("outcome", sorted(runner_module.OUTCOMES))
def test_r9e_every_runner_outcome_lands_in_exactly_one_bucket(tmp_path: Path, outcome: str) -> None:
    """Every outcome the runner can write answers with exactly one of the two
    fields: `after_generated_at` for the design's trusted three, otherwise
    `before_generated_at`."""
    path = tmp_path / "receipt.json"
    path.write_text(
        json.dumps(
            _refresh_receipt_payload(
                providers=_registry_evidence(after_hours=1.0, before_hours=50.0),
                outcome=outcome,
            )
        )
    )

    answered = probe.read_manifest_generated_at(path)

    after, before = NOW - timedelta(hours=1.0), NOW - timedelta(hours=50.0)
    assert answered == (after if outcome in DESIGN_TRUSTED_AFTER_OUTCOMES else before)


def test_r9e_the_trusted_outcomes_are_the_designs_and_a_subset_of_the_runners() -> None:
    """A renamed or dropped runner outcome reds here instead of silently
    falling into the untrusted bucket."""
    assert probe.TRUSTED_AFTER_GENERATED_AT_OUTCOMES == DESIGN_TRUSTED_AFTER_OUTCOMES
    assert probe.TRUSTED_AFTER_GENERATED_AT_OUTCOMES <= runner_module.OUTCOMES
    # The probe comment on the constant cites this test, and the reader's
    # docstring cites this family.
    assert "``test_r9e_*``" in PROBE_SOURCE.read_text()
    assert (
        "test_r9e_the_trusted_outcomes_are_the_designs_and_a_subset_of_the_runners"
        in PROBE_SOURCE.read_text()
    )


def test_r9e_the_runbook_states_the_probes_outcome_rule() -> None:
    """Prose<->code: the runbook's manual `jq` recipe and its fallback bullet
    both name exactly the probe's trusted set, and the bullet names every other
    runner outcome as the `before_generated_at` bucket."""
    trusted = probe.TRUSTED_AFTER_GENERATED_AT_OUTCOMES
    recipes = [
        block
        for block in re.findall(r"```bash\n(.*?)```", RUNBOOK.read_text(), re.S)
        if 'select(.name == "registry")' in block
        and "provider-refresh/receipts/latest.json" in block
    ]
    assert len(recipes) == 1, recipes
    assert set(re.findall(r'\$o == "([^"]+)"', recipes[0])) == trusted
    assert "then .after_generated_at else .before_generated_at end" in recipes[0]

    section = _probe_runbook_section()
    start = section.index("按 receipt 的 `outcome` 取字段")
    bullet = section[start : section.index("\n\n", start)]
    trusted_part, rest = bullet.split("时取 `registry.after_generated_at`", 1)
    assert set(re.findall(r"`([a-z_]+)`", trusted_part)) - {"outcome"} == trusted
    others = re.search(r"其余 outcome（([^）]*)）", rest)
    assert others is not None, bullet
    assert set(re.findall(r"`([a-z_]+)`", others.group(1))) == runner_module.OUTCOMES - trusted
    assert "取 `registry.before_generated_at`" in rest


def test_r9b_an_unresolvable_latest_never_masks_a_stopped_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The A1 regression.

    Grading an unresolvable manifest at precedence 1 hid a genuinely stopped
    timer behind a generic `probe_failed` for as long as the failed receipt
    stayed newest -- up to ~24 h on the daily cadence.  This must fail if the
    manifest arm is ever moved back above the timer arms.
    """
    latest = _unresolvable_latest(tmp_path, "empty_providers")
    _history_root(tmp_path)  # present but empty: nothing resolves an age

    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        receipt=latest,
        properties=_properties(
            active_state="inactive",
            sub_state="dead",
            inactive_enter=_systemd_timestamp(NOW - timedelta(days=6)),
            next_elapse="",
        ),
    )
    payload = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert payload["verdict"] == "timer_stopped"
    assert payload["manifest_source"] == "unavailable"
    assert payload["manifest_age_hours"] is None


def test_r9b_an_unresolvable_latest_never_masks_a_disabled_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = _unresolvable_latest(tmp_path, "missing")

    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        receipt=latest,
        properties=_properties(unit_file_state="disabled"),
    )

    assert status != 0
    assert _verdict(root) == "timer_not_enabled"


@pytest.mark.parametrize(
    "next_elapse",
    ["", "n/a", "not-a-timestamp"],
    ids=["empty", "systemd-absent", "unparseable"],
)
def test_r9b_an_unresolvable_latest_never_masks_an_unscheduled_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, next_elapse: str
) -> None:
    """The THIRD timer arm, and the one the other two did not cover.

    The spec says the manifest arm "SHALL NOT mask ANY timer verdict", but only
    `timer_stopped` and `timer_not_enabled` were pinned under an unresolvable
    `latest.json`.  Verified: with just those two present, hoisting the
    precedence-7 `manifest_unavailable` arm ABOVE the `timer_not_scheduled`
    check leaves the whole suite green -- an `active` timer that will never
    tick again would have been reported as a missing receipt.
    """
    latest = _unresolvable_latest(tmp_path, "empty_providers")
    # No `history/` sibling at all: nothing can resolve an age, so precedence 7
    # is genuinely armed and is what this test proves does NOT win.
    assert not (latest.parent / "history").exists()

    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        receipt=latest,
        properties=_properties(active_state="active", next_elapse=next_elapse),
    )
    payload = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert payload["verdict"] == "timer_not_scheduled"
    assert payload["manifest_source"] == "unavailable"
    assert payload["manifest_age_hours"] is None


def test_r9c_no_history_directory_at_all_is_manifest_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = _unresolvable_latest(tmp_path, "empty_providers")

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert payload["verdict"] == "manifest_unavailable"
    assert payload["manifest_source"] == "unavailable"
    assert payload["manifest_age_hours"] is None


def test_r9c_history_present_but_every_candidate_unresolvable_is_manifest_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = _unresolvable_latest(tmp_path, "malformed")
    history = _history_root(tmp_path)
    _write_history_receipt(history, _history_name(8, 1), raw="{nope")
    _write_history_receipt(history, _history_name(9, 2), providers=[])
    _write_history_receipt(
        history,
        _history_name(10, 3),
        providers=[{"name": "readiness", "after_generated_at": "2026-09-11T10:00:00Z"}],
    )

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert payload["verdict"] == "manifest_unavailable"
    assert payload["manifest_source"] == "unavailable"


def test_r9d_the_directory_listing_is_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A history directory far larger than the runner's own `MAX_HISTORY = 32`
    must not be walked end to end by an hourly watchdog."""
    latest = _unresolvable_latest(tmp_path, "missing")
    history = _history_root(tmp_path)
    for index in range(250):
        _write_history_receipt(history, _history_name(index % 24, index), raw="{nope")

    yielded = [0]
    real_scandir = os.scandir

    def counting_scandir(path):  # type: ignore[no-untyped-def]
        iterator = real_scandir(path)
        # `os.scandir` is global once patched, and pytest's own tmp_path
        # teardown calls it with a directory FILE DESCRIPTOR; only count the
        # history directory and hand everything else straight back.
        if not isinstance(path, (str, os.PathLike)) or Path(path) != history:
            return iterator

        def counted():  # type: ignore[no-untyped-def]
            try:
                for entry in iterator:
                    yielded[0] += 1
                    yield entry
            finally:
                iterator.close()

        return counted()

    monkeypatch.setattr(probe.os, "scandir", counting_scandir)

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)

    assert status != 0
    assert _verdict(root) == "manifest_unavailable"
    assert yielded[0] <= probe.MAX_HISTORY_ENTRIES_LISTED
    assert probe.MAX_HISTORY_ENTRIES_LISTED == 200


def test_r9d_at_most_ten_candidates_are_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = _unresolvable_latest(tmp_path, "missing")
    history = _history_root(tmp_path)
    for index in range(30):
        _write_history_receipt(history, _history_name(index % 24, index), raw="{nope")

    opened: list[Path] = []
    real_read = probe.read_bounded_no_follow

    def spy(path: Path, *, max_bytes: int) -> bytes:
        opened.append(Path(path))
        return real_read(path, max_bytes=max_bytes)

    monkeypatch.setattr(probe, "read_bounded_no_follow", spy)

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    from_history = [path for path in opened if path.parent == history]

    assert status != 0
    assert _verdict(root) == "manifest_unavailable"
    assert len(from_history) <= probe.MAX_HISTORY_CANDIDATES_OPENED
    assert probe.MAX_HISTORY_CANDIDATES_OPENED == 10


def test_r9d_off_shape_filenames_are_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the runner's own `refresh_<UTC>_<uuid12>.json` shape is a candidate.

    The off-shape files below all sort ABOVE the legitimate one, so a scan that
    did not filter would read them first.
    """
    latest = _unresolvable_latest(tmp_path, "missing")
    history = _history_root(tmp_path)
    legitimate = _history_name(10, 1)
    _write_history_receipt(history, legitimate, manifest_age_hours=31.0)
    for off_shape in (
        "zzz-operator-copy.json",
        "refresh_backup.json",
        "refresh_20260911T110000Z_short.json",
        "refresh_20260911T110000Z_0123456789ab.json.bak",
    ):
        (history / off_shape).write_text(
            json.dumps(_refresh_receipt_payload(manifest_age_hours=1.0))
        )

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    assert payload["manifest_source"] == f"history:{legitimate}"
    assert payload["manifest_age_hours"] == pytest.approx(31.0)


def test_r9d_a_symlinked_candidate_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    latest = _unresolvable_latest(tmp_path, "missing")
    history = _history_root(tmp_path)
    older = _history_name(9, 1)
    newer = _history_name(11, 2)
    _write_history_receipt(history, older, manifest_age_hours=42.0)
    target = tmp_path / "outside-the-store.json"
    target.write_text(json.dumps(_refresh_receipt_payload(manifest_age_hours=1.0)))
    (history / newer).symlink_to(target)

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    # The symlink was the newest candidate and was refused, not followed.
    assert payload["manifest_source"] == f"history:{older}"
    assert payload["manifest_age_hours"] == pytest.approx(42.0)


def test_r9d_ordering_is_lexical_by_filename_and_never_by_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixed-width UTC prefix makes a lexical DESCENDING sort chronological,
    so no timestamp is parsed and no `mtime` is trusted.  Here `mtime`
    deliberately contradicts the filename order."""
    latest = _unresolvable_latest(tmp_path, "missing")
    history = _history_root(tmp_path)
    older_name = _history_name(9, 1)
    newer_name = _history_name(11, 2)
    older = _write_history_receipt(history, older_name, manifest_age_hours=90.0)
    newer = _write_history_receipt(history, newer_name, manifest_age_hours=30.0)
    # `newer_name` is lexically greatest but is given the OLDEST mtime.
    os.utime(newer, (1_000_000, 1_000_000))
    os.utime(older, (2_000_000_000, 2_000_000_000))

    status, root, _log = _run(tmp_path, monkeypatch, receipt=latest)
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    assert payload["manifest_source"] == f"history:{newer_name}"
    assert payload["manifest_age_hours"] == pytest.approx(30.0)


def test_the_history_filename_shape_matches_the_runner_that_writes_it() -> None:
    """Pins the probe's filter to the runner's own name, so a rename on either
    side reds here instead of silently emptying the fallback.

    Also pins the receipt SCHEMA VERSION the probe demands (audit F7).  The
    probe rejects any receipt whose `schema_version` differs, uniformly across
    `latest.json` AND every history candidate, so a runner schema bump with no
    pin here silently kills the whole manifest arm and leaves the probe at a
    permanent `manifest_unavailable`.  D4 forbids the PROBE importing repo
    packages; it says nothing about this test, so the comparison is against the
    runner's live constant rather than a restated literal.
    """
    runner = (
        Path(__file__).resolve().parents[1] / "scripts" / "scheduler_file_provider_refresh.py"
    ).read_text()

    assert (
        "run_id = f\"refresh_{started.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:12]}\""
        in runner
    )
    assert probe.REFRESH_RECEIPT_SCHEMA_VERSION == runner_module.SCHEMA_VERSION

    # The directory the runner writes history receipts into, next to
    # `latest.json`, is the one the probe falls back to.  The runner exposes no
    # constant for it, so its one literal is read from source.
    history_dirs = re.findall(r'^\s*history = root / "([^"]+)"$', runner, re.M)
    assert history_dirs == [probe.HISTORY_DIRECTORY_NAME]
    assert 'history / f"{run_id}.json"' in runner
    assert 'latest_path = root / "latest.json"' in runner


def test_the_consumer_bound_is_the_consumers_own_constant() -> None:
    """Audit F5: `CONSUMER_MAX_MANIFEST_AGE_HOURS` is a COPY, and until now the
    only thing joining the copy to the original was a comment.

    If the consumer's bound drops, the probe keeps grading against 168 and
    reports `ok` for a manifest the consumer has already fail-closed on -- the
    precise failure this probe exists to make impossible, reintroduced one
    level up.  D4 forbids the probe importing repo packages; the test is not
    the probe, so it reads both sides directly.
    """
    from services.orchestrator import scheduler_file_providers

    assert (
        probe.CONSUMER_MAX_MANIFEST_AGE_HOURS
        == scheduler_file_providers.DEFAULT_MAX_MANIFEST_AGE_HOURS
    )
    # And the derived ceilings move with it, so the margin rule cannot be left
    # describing a bound that no longer exists.
    assert (
        probe.MAX_THRESHOLD_HOURS
        == scheduler_file_providers.DEFAULT_MAX_MANIFEST_AGE_HOURS
        - probe.REFRESH_CADENCE_HOURS
    )


def test_the_history_listing_cap_is_above_the_runners_history_cap() -> None:
    """The probe comment and the runbook both say the 200-entry listing cap is
    above the runner's `MAX_HISTORY`; read the runner's constant for both."""
    assert probe.MAX_HISTORY_ENTRIES_LISTED > runner_module.MAX_HISTORY
    assert "test_the_history_listing_cap_is_above_the_runners_history_cap" in PROBE_SOURCE.read_text()
    assert f"`MAX_HISTORY = {runner_module.MAX_HISTORY}`" in PROBE_SOURCE.read_text()
    assert (
        f"目录列举封顶 {probe.MAX_HISTORY_ENTRIES_LISTED} 条"
        f"（高于 runner 自己的 `MAX_HISTORY = {runner_module.MAX_HISTORY}`），"
        f"最多打开最新的 {probe.MAX_HISTORY_CANDIDATES_OPENED} 份"
    ) in _probe_runbook_section()


def test_the_stopped_dwell_is_three_times_the_refresh_oneshots_start_timeout() -> None:
    """The runbook justifies the 6 h stopped-dwell as three times the refresh
    oneshot's `TimeoutStartSec=`; read that value from the refresh unit."""
    refresh_service = (
        Path(__file__).resolve().parents[1]
        / "infra"
        / "systemd"
        / "nhms-scheduler-file-provider-refresh.service"
    )
    timeouts = re.findall(r"^TimeoutStartSec=(\d+)$", refresh_service.read_text(), re.M)
    assert len(timeouts) == 1, timeouts
    seconds = int(timeouts[0])

    assert probe.DEFAULT_STOPPED_DWELL_HOURS * 3600 == 3 * seconds
    assert seconds == 2 * 3600
    section = _probe_runbook_section()
    assert f"`TimeoutStartSec={seconds}` 意味着合法窗口可以跑满两小时" in section
    assert f"{probe.DEFAULT_STOPPED_DWELL_HOURS} 小时是它的三倍" in section


def test_the_probe_units_start_timeout_comment_counts_every_receipt_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe unit's `TimeoutStartSec=` comment counts one receipt read plus
    up to `MAX_HISTORY_CANDIDATES_OPENED` on the fallback; count the reads a
    fully unresolvable run actually makes."""
    unit = Path(__file__).resolve().parents[1] / "infra" / "systemd" / PROBE_UNITS[0]
    comments = " ".join(
        line.lstrip("#").strip() for line in unit.read_text().splitlines() if line.startswith("#")
    )
    assert (
        "one bounded receipt read, and up to "
        f"{probe.MAX_HISTORY_CANDIDATES_OPENED} more on the history fallback"
    ) in comments

    latest = _unresolvable_latest(tmp_path, "malformed")
    history = _history_root(tmp_path)
    for index in range(probe.MAX_HISTORY_CANDIDATES_OPENED + 5):
        _write_history_receipt(history, _history_name(index, index), raw="{nope")
    opened: list[Path] = []
    real_read = probe.read_manifest_generated_at

    def counting_read(path: Path) -> object:
        opened.append(path)
        return real_read(path)

    monkeypatch.setattr(probe, "read_manifest_generated_at", counting_read)

    generated_at, source, _errors = probe.resolve_manifest_generated_at(latest)

    assert generated_at is None and source == probe.MANIFEST_SOURCE_UNAVAILABLE
    assert len(opened) == 1 + probe.MAX_HISTORY_CANDIDATES_OPENED


def test_the_production_path_defaults_match_every_file_that_states_them() -> None:
    """Audit P2: the probe's two production paths are restated in three other
    places, and nothing read both sides.

    `DEFAULT_HEALTH_RECEIPT_ROOT` is where the probe writes and where its
    installer creates a 0700 directory; `DEFAULT_REFRESH_RECEIPT` is the file
    the probe reads, inside the receipt root the refresh runner's env template
    names.  A drift on either makes the probe watch a path nothing writes --
    `manifest_unavailable` forever, or a receipt root the installer never made
    private.
    """
    repo = Path(__file__).resolve().parents[1]
    probe_installer = (repo / "scripts" / "install_node22_refresh_timer_health.sh").read_text()
    runbook = (repo / "docs" / "runbooks" / "current-production-ops.md").read_text()
    env_example = (
        repo / "infra" / "env" / "compute.scheduler-provider-refresh.env.example"
    ).read_text()

    assert (
        f"receipt_root=${{{probe.ENV_HEALTH_RECEIPT_ROOT}:-{probe.DEFAULT_HEALTH_RECEIPT_ROOT}}}"
        in probe_installer
    )
    assert probe.DEFAULT_HEALTH_RECEIPT_ROOT in runbook
    # The runner's env template names the RECEIPT ROOT; the probe reads
    # `latest.json` inside it, so the pin is the parent, not the file.
    assert (
        "NHMS_SCHEDULER_PROVIDER_REFRESH_RECEIPT_ROOT="
        f"{Path(probe.DEFAULT_REFRESH_RECEIPT).parent}" in env_example
    )
    assert Path(probe.DEFAULT_REFRESH_RECEIPT).name == "latest.json"
    assert probe.HISTORY_RECEIPT_NAME.match("refresh_20260912T103558Z_0123456789ab.json")
    assert not probe.HISTORY_RECEIPT_NAME.match("refresh_20260912T103558Z_0123456789ab.json.bak")
    assert not probe.HISTORY_RECEIPT_NAME.match("latest.json")


def test_the_manifest_source_is_always_from_the_closed_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R14: `manifest_source` is `latest` | `history:<filename>` | `unavailable`
    and nothing else, on every path that writes a receipt."""
    history = _history_root(tmp_path)
    name = _history_name(10, 7)
    _write_history_receipt(history, name, manifest_age_hours=20.0)

    observed = set()
    for shape, prepare in (
        ("latest", lambda: _write_refresh_receipt(tmp_path, name="provider-refresh/receipts/latest.json")),
        ("history", lambda: _unresolvable_latest(tmp_path, "malformed")),
    ):
        del shape
        receipt = prepare()
        _status, root, _log = _run(tmp_path, monkeypatch, receipt=receipt)
        observed.add(json.loads((root / "latest.json").read_text())["manifest_source"])
    for candidate in history.iterdir():
        candidate.unlink()
    _status, root, _log = _run(
        tmp_path, monkeypatch, receipt=_unresolvable_latest(tmp_path, "malformed")
    )
    observed.add(json.loads((root / "latest.json").read_text())["manifest_source"])

    assert observed == {
        probe.MANIFEST_SOURCE_LATEST,
        f"history:{name}",
        probe.MANIFEST_SOURCE_UNAVAILABLE,
    }


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


# ---------------------------------------------------------------------------
# R11b -- each tunable is EFFECTIVE through its env path, not merely rejectable
# ---------------------------------------------------------------------------

# A rejection test proves the bound; it does not prove the value is read. These
# three drive each threshold through the environment to a value that FLIPS the
# verdict, which is what "exercised through the env path" has to mean for the
# env-surface inventory to be worth anything.


def test_r11b_the_next_dwell_threshold_is_effective_through_its_env_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`NEXT` 40 h out: `timer_not_scheduled` at the 36 h default, `ok` at 48."""
    properties = _properties(next_elapse=_systemd_timestamp(NOW + timedelta(hours=40)))

    status, root, _log = _run(tmp_path, monkeypatch, properties=properties)
    assert status != 0
    assert _verdict(root) == "timer_not_scheduled"

    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        properties=properties,
        thresholds={probe.ENV_MAX_NEXT_DWELL_HOURS: "48"},
    )
    payload = json.loads((root / "latest.json").read_text())

    assert status == 0, payload
    assert payload["verdict"] == "ok"
    assert payload["max_next_dwell_hours"] == 48


def test_r11b_the_stopped_dwell_threshold_is_effective_through_its_env_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idle 3 h: inside the 6 h default dwell, past a 2 h one.

    Note the direction: a SHORTER dwell makes the probe noisier, never quieter.
    A longer one is capped at one refresh cadence (`MAX_STOPPED_DWELL_HOURS`,
    24 h) at config time -- not at the consumer bound.
    """
    properties = _properties(
        active_state="inactive",
        sub_state="dead",
        inactive_enter=_systemd_timestamp(NOW - timedelta(hours=3)),
        next_elapse="",
    )

    status, root, _log = _run(tmp_path, monkeypatch, properties=properties)
    assert status == 0
    assert _verdict(root) == "ok"

    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        properties=properties,
        thresholds={probe.ENV_STOPPED_DWELL_HOURS: "2"},
    )
    payload = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert payload["verdict"] == "timer_stopped"
    assert payload["stopped_dwell_hours"] == 2


def test_r11b_the_manifest_age_threshold_is_effective_through_its_env_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manifest 100 h old: fresh at the 120 h default, stale at 96."""
    receipt = _write_refresh_receipt(tmp_path, manifest_age_hours=100.0)

    status, root, _log = _run(tmp_path, monkeypatch, receipt=receipt)
    assert status == 0
    assert _verdict(root) == "ok"

    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        receipt=receipt,
        thresholds={probe.ENV_MAX_MANIFEST_AGE_HOURS: "96"},
    )
    payload = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert payload["verdict"] == "manifest_stale"
    assert payload["max_manifest_age_hours"] == 96
