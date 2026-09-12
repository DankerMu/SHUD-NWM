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
      `assert_protected_unchanged` exists to catch.
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
        }
    )
    completed = subprocess.run(
        ["bash", str(INSTALLER), action],
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
    this change exists to close, reachable through a drop-in.
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
    """The fallback does NOT gate on the receipt's `outcome`.

    A receipt carrying `registry.after_generated_at` reports a manifest that
    really was published at that instant, whatever the run's terminal outcome
    was; one predicate, applied uniformly (design D3b).
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
    side reds here instead of silently emptying the fallback."""
    runner = (
        Path(__file__).resolve().parents[1] / "scripts" / "scheduler_file_provider_refresh.py"
    ).read_text()

    assert (
        "run_id = f\"refresh_{started.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:12]}\""
        in runner
    )
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


def test_r15_both_installers_compare_the_protected_units_per_unit_type() -> None:
    """C1(d) applies to BOTH installers: the sibling's own scheduler assertion
    compares the compute scheduler's `.service` on `UnitFileState` only, for the
    same reason -- a oneshot re-activating every 5 minutes is not a change."""
    sibling = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "install_node22_scheduler_file_provider_refresh.sh"
    ).read_text()

    assert "unit_file_state nhms-compute-scheduler.service" in sibling
    assert "unit_state nhms-compute-scheduler.timer" in sibling
    for installer_source in (INSTALLER.read_text(), sibling):
        assert "set -Eeuo pipefail" in installer_source


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
    A longer one is bounded under 168 h at config time.
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
