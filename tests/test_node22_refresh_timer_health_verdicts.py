"""Node-22 refresh-timer probe: the verdict table rows R1-R9c.

Partition (#2532 partition of the 3456-line / 202-case
tests/test_node22_refresh_timer_health.py).
The healthy lane, the stopped-vs-not-enabled split and
the operator dwell, the unscheduled active timer, manifest freshness and
precedence, and fail-closed grading of unreadable systemd or receipt evidence.

Every test drives the probe through a fake ``systemctl`` shim that records each
invocation, a pinned clock (``--now``), and a temporary receipt root.  No real
systemd, no production path, no network. Test anchors are the Invariant Matrix
rows of ``openspec/changes/harden-node22-scheduler-refresh-lane/design.md``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from tests.node22_refresh_timer_health_helpers import (
    NOW,
    _properties,
    _run,
    _systemd_timestamp,
    _verdict,
    _write_refresh_receipt,
)

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
