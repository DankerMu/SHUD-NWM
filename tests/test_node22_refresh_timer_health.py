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
) -> tuple[Path, Path]:
    """Create a fake ``systemctl`` that logs every invocation it receives."""
    log = tmp_path / "systemctl.log"
    show_output = "\n".join(f"{key}={value}" for key, value in properties.items()) + "\n"
    payload = tmp_path / "show.txt"
    payload.write_text(show_output)
    script = tmp_path / "fake-systemctl"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        f"if [ {exit_code} -ne 0 ]; then exit {exit_code}; fi\n"
        "for arg in \"$@\"; do\n"
        "  case \"$arg\" in\n"
        f'    show) cat {payload}; exit 0 ;;\n'
        "    list-timers) printf 'NEXT LEFT LAST PASSED UNIT ACTIVATES\\n'; exit 0 ;;\n"
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


def _write_refresh_receipt(
    tmp_path: Path,
    *,
    manifest_age_hours: float | None = 5.0,
    schema_version: str = probe.REFRESH_RECEIPT_SCHEMA_VERSION,
    providers: list[dict[str, object]] | None = None,
    raw: str | None = None,
) -> Path:
    path = tmp_path / "refresh-latest.json"
    if raw is not None:
        path.write_text(raw)
        return path
    if providers is None:
        generated_at = (NOW - timedelta(hours=manifest_age_hours or 0.0)).isoformat().replace(
            "+00:00", "Z"
        )
        providers = [
            {"name": "registry", "after_generated_at": generated_at, "entry_count": 18},
            {"name": "registry_worker_mirror", "after_generated_at": generated_at},
            {"name": "readiness", "after_generated_at": generated_at},
            {"name": "state", "after_generated_at": generated_at},
        ]
    path.write_text(
        json.dumps({"schema_version": schema_version, "outcome": "published", "providers": providers})
    )
    return path


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    properties: dict[str, str] | None = None,
    systemctl_exit_code: int = 0,
    systemctl_path: str | None = None,
    receipt: Path | None = None,
    receipt_root: Path | None = None,
    thresholds: dict[str, str] | None = None,
    now: datetime = NOW,
    extra_argv: list[str] | None = None,
) -> tuple[int, Path, Path]:
    """Run the probe end-to-end; return (exit status, receipt root, call log)."""
    script, log = _write_fake_systemctl(
        tmp_path,
        properties=properties if properties is not None else _properties(),
        exit_code=systemctl_exit_code,
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
        probe.ENV_NOW,
        probe.ENV_JSON,
    ):
        monkeypatch.delenv(name, raising=False)
    for key, value in (thresholds or {}).items():
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
        "--now",
        now.isoformat().replace("+00:00", "Z"),
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


def test_r9_missing_refresh_receipt_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, root, _log = _run(tmp_path, monkeypatch, receipt=tmp_path / "absent.json")

    assert status != 0
    assert _verdict(root) == "probe_failed"


def test_r9_malformed_refresh_receipt_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, root, _log = _run(
        tmp_path, monkeypatch, receipt=_write_refresh_receipt(tmp_path, raw="{not json")
    )

    assert status != 0
    assert _verdict(root) == "probe_failed"


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
def test_r9_schema_invalid_refresh_receipt_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, receipt_kwargs: dict[str, object]
) -> None:
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        receipt=_write_refresh_receipt(tmp_path, **receipt_kwargs),  # type: ignore[arg-type]
    )

    assert status != 0
    assert _verdict(root) == "probe_failed"


def test_r9_a_failed_refresh_receipt_with_no_providers_is_probe_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `failed` refresh writes `providers: []`; there is no manifest age to
    grade, so the probe reports missing evidence rather than health."""
    status, root, _log = _run(
        tmp_path,
        monkeypatch,
        receipt=_write_refresh_receipt(tmp_path, providers=[]),
    )

    assert status != 0
    assert _verdict(root) == "probe_failed"


def test_probe_failed_still_records_the_systemd_signals_it_could_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest signal and the systemd signals are collected
    independently, so an unreadable receipt does not blank the receipt's
    systemd fields."""
    status, root, _log = _run(tmp_path, monkeypatch, receipt=tmp_path / "absent.json")
    receipt = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert receipt["verdict"] == "probe_failed"
    assert receipt["unit_file_state"] == "enabled"
    assert receipt["active_state"] == "active"
    assert receipt["manifest_age_hours"] is None


def test_probe_failed_records_the_manifest_age_when_systemd_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, root, _log = _run(tmp_path, monkeypatch, systemctl_exit_code=3)
    receipt = json.loads((root / "latest.json").read_text())

    assert status != 0
    assert receipt["verdict"] == "probe_failed"
    assert receipt["manifest_age_hours"] == pytest.approx(5.0)
    assert receipt["unit_file_state"] == ""


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
        "max_next_dwell_hours",
        "max_manifest_age_hours",
        "stopped_dwell_hours",
    }
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
    assert _verdict(root) == "probe_failed"


def test_the_probe_refuses_an_oversize_refresh_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oversize = tmp_path / "oversize.json"
    oversize.write_bytes(b"{" + b" " * (probe.MAX_REFRESH_RECEIPT_BYTES + 16) + b"}")

    status, root, _log = _run(tmp_path, monkeypatch, receipt=oversize)

    assert status != 0
    assert _verdict(root) == "probe_failed"


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
                                evidence_error=None,
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
    """A fake systemctl that answers the read-only queries and logs mutations."""
    log = tmp_path / "installer-systemctl.log"
    script = tmp_path / "fake-systemctl"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        "shift\n"  # drop --user
        "case \"$1\" in\n"
        "  is-enabled) echo enabled ;;\n"
        "  is-active) echo active ;;\n"
        "esac\n"
        "exit 0\n"
    )
    script.chmod(0o755)
    return script, log


def _run_installer(tmp_path: Path, action: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    script, log = _installer_fake_systemctl(tmp_path)
    environment = dict(os.environ)
    environment.update(
        {
            "NHMS_REFRESH_HEALTH_REPO": str(Path(__file__).resolve().parents[1]),
            "NHMS_REFRESH_HEALTH_UNIT_DIR": str(tmp_path / "units"),
            "NHMS_REFRESH_HEALTH_INSTALL_STATE_ROOT": str(tmp_path / "install-state"),
            "NHMS_REFRESH_HEALTH_RECEIPT_ROOT": str(tmp_path / "receipts"),
            "NHMS_REFRESH_HEALTH_SYSTEMCTL": str(script),
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
