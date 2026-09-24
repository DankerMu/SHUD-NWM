"""Node-22 refresh-timer probe: runbook liveness rows and the environment surface.

Partition (#2532 partition of the 3456-line / 202-case
tests/test_node22_refresh_timer_health.py).
R11c probe-timer liveness and the runbook's probe
section; B1 / R11b the clock has no environment seam and the probe reads
exactly the enumerated environment surface; R4b unparseable next elapse; D2
grading order; R8c host-zone independence; the stopped-dwell bound (B2); B3 no
env file on the probe unit; and R11b each tunable effective through its env
path.
"""

from __future__ import annotations

import ast
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import node22_refresh_timer_health as probe
from tests.node22_refresh_timer_health_helpers import (
    NOW,
    PROBE_SOURCE,
    PROBE_TIMER_UNIT,
    UNIT,
    _probe_runbook_section,
    _properties,
    _run,
    _systemd_timestamp,
    _verdict,
    _write_fake_systemctl,
    _write_refresh_receipt,
)

# ---------------------------------------------------------------------------
# R11c -- the watchdog's own liveness, and the comment that used to lie about it
# ---------------------------------------------------------------------------


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
