"""Discriminating tests for #1895 G0/G2/G3/G4 process/G8 ordering defects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packages.common.compressed_chunk_cold_probe.types import OWNED_NAME_RE, PROBE_NAME_PREFIX
from packages.common.node27_issue1895_gates import assert_ancestry_uses_exit_status, python_uses_json_load_on_path
from packages.common.node27_issue1895_probe import (
    assert_report_within_command_bracket,
    owned_probe_name,
    owned_probe_root,
    owned_probe_token,
    parse_bracket_instant,
)
from packages.common.node27_issue1895_process import (
    DISPLAY_API_UNIT,
    QUIESCED_UNITS,
    assert_exact_unit_pid,
    assert_unit_quiesced,
    assert_writers_quiesced,
    resolve_display_api_pid,
)
from packages.common.node27_issue1895_rollback import (
    POINT_AFTER_DRAIN,
    POINT_AFTER_MOVEMENT,
    POINT_BEFORE_MUTATION,
    assert_row_state_consistency,
    rollback_action,
)
from packages.common.node27_issue1895_sql import (
    EXTERNAL_TABLESPACE_SQL,
    assert_external_tablespace_sql,
    parse_external_tablespace_rows,
)
from packages.common.node27_issue1895_timer import assert_baseline_before_start
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from tests.test_issue1895_runbook_contract import _gate, _gate_bash, _gate_lines

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_owned_name_re_and_prefix_are_the_shipping_constants() -> None:
    from packages.common.compressed_chunk_cold_probe.types import OWNED_NAME_RE as shipping_re
    from packages.common.compressed_chunk_cold_probe.types import PROBE_NAME_PREFIX as shipping_prefix

    assert shipping_prefix == "nhms-1892-probe-"
    assert shipping_re.pattern == r"^nhms-1892-probe-[0-9a-f]{8,32}$"
    assert PROBE_NAME_PREFIX == shipping_prefix
    assert OWNED_NAME_RE.pattern == shipping_re.pattern


def test_g0_ancestry_gate_uses_exit_status_not_stdout() -> None:
    g0 = _gate("G0")
    assert_ancestry_uses_exit_status(g0)
    assert 'test "$(git merge-base --is-ancestor' not in g0
    assert "git merge-base --is-ancestor HEAD origin/master" in g0


def test_g0_ancestry_stdout_comparison_is_refused() -> None:
    bad = 'test "$(git merge-base --is-ancestor HEAD origin/master)" = "0" || exit 1'
    with pytest.raises(Issue1895ReadinessError) as caught:
        assert_ancestry_uses_exit_status(bad)
    assert caught.value.code == "ANCESTRY_STDOUT_COMPARISON"
    assert_ancestry_uses_exit_status("git merge-base --is-ancestor HEAD origin/master || { echo NO-GO; exit 1; }")


def test_g3_probe_basename_matches_shipping_parser_not_parent_prefix() -> None:
    token = owned_probe_token()
    name = owned_probe_name(token)
    root = owned_probe_root("/home/nwm/.nhms-1892-probe", name)
    assert OWNED_NAME_RE.fullmatch(name)
    assert name.startswith(PROBE_NAME_PREFIX)
    assert PROBE_NAME_PREFIX in root.name
    assert root.name == name
    g3 = _gate("G3")
    assert "owned_probe_name" in g3 or "PROBE_NAME_PREFIX" in g3
    assert 'PROBE_ROOT="/home/nwm/.nhms-1892-probe/$RUN_STAMP"' not in g3
    assert "$PROBE_NAME" in " ".join(_gate_lines("G3"))


def test_g3_timestamp_under_prefixed_parent_is_not_an_owned_basename() -> None:
    with pytest.raises(Issue1895ReadinessError) as caught:
        owned_probe_root("/home/nwm/.nhms-1892-probe", "20260904T123000Z")
    assert caught.value.code == "PROBE_ROOT_INVALID"
    with pytest.raises(Issue1895ReadinessError) as empty:
        owned_probe_name("")
    assert empty.value.code == "PROBE_NAME_INVALID"
    with pytest.raises(Issue1895ReadinessError) as upper:
        owned_probe_name("ABCDEF12")
    assert upper.value.code == "PROBE_NAME_INVALID"


def test_bracket_instants_preserve_subsecond_boundaries_and_close_parse_errors() -> None:
    start = parse_bracket_instant("2026-09-06T12:00:00.100000000+00:00")
    end = parse_bracket_instant("2026-09-06T12:00:00.200000000+00:00")
    assert_report_within_command_bracket(report_mtime=start.timestamp(), start=start, end=end)
    with pytest.raises(Issue1895ReadinessError) as outside:
        assert_report_within_command_bracket(
            report_mtime=end.timestamp() + 0.001,
            start=start,
            end=end,
        )
    assert outside.value.code == "REPORT_OUT_OF_BRACKET"
    for bad in ("2026-02-30T00:00:00Z", "not-a-date", "2026-09-06T12:00:00"):
        with pytest.raises(Issue1895ReadinessError) as invalid:
            parse_bracket_instant(bad)
        assert invalid.value.code == "BRACKET_INVALID"


def test_g3_report_mtime_is_bounded_by_start_and_end_not_bracket_file() -> None:
    start = datetime(2026, 9, 4, 4, 0, 0, tzinfo=UTC)
    end = datetime(2026, 9, 4, 4, 5, 0, tzinfo=UTC)
    report = (start + timedelta(seconds=30)).timestamp()
    assert_report_within_command_bracket(report_mtime=report, start=start, end=end)
    with pytest.raises(Issue1895ReadinessError) as before:
        assert_report_within_command_bracket(report_mtime=start.timestamp() - 1, start=start, end=end)
    assert before.value.code == "REPORT_OUT_OF_BRACKET"
    with pytest.raises(Issue1895ReadinessError) as after:
        assert_report_within_command_bracket(report_mtime=end.timestamp() + 1, start=start, end=end)
    assert after.value.code == "REPORT_OUT_OF_BRACKET"
    g3 = _gate("G3")
    assert "assert mtime >= os.stat(bracket).st_mtime" not in g3
    assert "assert_report_within_command_bracket" in g3


def test_g3_report_gte_bracket_file_mtime_direction_is_absent() -> None:
    g3 = _gate("G3")
    assert "st_mtime" in g3 or "assert_report_within_command_bracket" in g3
    assert ">= os.stat(bracket).st_mtime" not in g3


def test_g2_and_g5_alias_location_before_order_by_and_allow_empty_targets() -> None:
    assert_external_tablespace_sql(EXTERNAL_TABLESPACE_SQL)
    empty = parse_external_tablespace_rows("", command_ok=True)
    assert empty == ()
    rows = parse_external_tablespace_rows("cold|/data/GHDC/nhms-cold\n", command_ok=True)
    assert rows == ({"spcname": "cold", "location": "/data/GHDC/nhms-cold"},)
    with pytest.raises(Issue1895ReadinessError) as failed:
        parse_external_tablespace_rows("", command_ok=False)
    assert failed.value.code == "SQL_COMMAND_FAILED"
    with pytest.raises(Issue1895ReadinessError):
        assert_external_tablespace_sql(
            "SELECT spcname, pg_tablespace_location(oid) FROM pg_tablespace "
            "WHERE pg_tablespace_location(oid) <> '' ORDER BY location, spcname"
        )
    g2 = _gate("G2")
    g5 = _gate("G5")
    for block in (g2, g5):
        assert "pg_tablespace_location(oid) AS location" in block
        assert "test -s \"$STAGE/external-targets.txt\"" not in block
        assert "test -s \"$STAGE2/external-targets.txt\"" not in block


def test_g4_and_g7_reject_pgrep_script_name_matching() -> None:
    g4 = _gate("G4")
    g7 = _gate("G7")
    assert "pgrep -f" not in g4
    for _opening, body in _gate_bash("G7"):
        assert "pgrep -f" not in body
    assert "never `pgrep -f`" in g7
    assert "MainPID" in g4
    assert DISPLAY_API_UNIT in g7
    assert "systemctl --user show" in g4 and "MainPID" in g4


def test_process_owner_rejects_zero_multiple_stale_and_wrapper_names() -> None:
    dead = {
        "Id": "nhms-node27-autopipe.service",
        "MainPID": 0,
        "ActiveState": "inactive",
        "ControlGroup": "",
    }
    assert_unit_quiesced("nhms-node27-autopipe.service", dead)
    live = {
        "Id": DISPLAY_API_UNIT,
        "MainPID": 4242,
        "ActiveState": "active",
        "ControlGroup": f"/user.slice/user-1000.slice/{DISPLAY_API_UNIT}",
    }
    assert_exact_unit_pid(
        DISPLAY_API_UNIT,
        live,
        cgroup_text=f"0::/user.slice/{DISPLAY_API_UNIT}",
    )
    with pytest.raises(Issue1895ReadinessError) as zero:
        assert_exact_unit_pid(
            DISPLAY_API_UNIT,
            {**live, "MainPID": 0},
            cgroup_text=f"0::/{DISPLAY_API_UNIT}",
        )
    assert zero.value.code == "PROCESS_PID_ZERO"
    with pytest.raises(Issue1895ReadinessError) as mismatch:
        assert_exact_unit_pid(
            DISPLAY_API_UNIT,
            live,
            cgroup_text="0::/user.slice/editor-node27_autopipeline.py",
        )
    assert mismatch.value.code == "PROCESS_CGROUP_MISMATCH"
    with pytest.raises(Issue1895ReadinessError):
        assert_unit_quiesced("nhms-node27-autopipe.service", {**dead, "MainPID": 9, "ActiveState": "active"})


def test_quiescence_requires_every_shipping_unit() -> None:
    shows = {
        unit: {"Id": unit, "MainPID": 0, "ActiveState": "inactive", "ControlGroup": ""}
        for unit in QUIESCED_UNITS
    }
    assert_writers_quiesced(shows)
    incomplete = dict(shows)
    incomplete.pop(QUIESCED_UNITS[0])
    with pytest.raises(Issue1895ReadinessError):
        assert_writers_quiesced(incomplete)


def test_display_pid_resolver_uses_unit_mainpid_not_head_of_pgrep() -> None:
    show = {
        "Id": DISPLAY_API_UNIT,
        "MainPID": 77,
        "ActiveState": "active",
        "ControlGroup": f"/user.slice/{DISPLAY_API_UNIT}",
    }
    pid = resolve_display_api_pid(show, read_cgroup=lambda _pid: f"0::/{DISPLAY_API_UNIT}")
    assert pid == 77
    with pytest.raises(Issue1895ReadinessError):
        resolve_display_api_pid(show, read_cgroup=lambda _pid: "0::/user.slice/vim node27_autopipeline.py")


def test_g8_baseline_is_captured_before_systemctl_start() -> None:
    g8 = _gate("G8")
    assert_baseline_before_start(g8)
    lines = "\n".join(_gate_lines("G8"))
    start_at = lines.find("systemctl --user start")
    if start_at < 0:
        start_at = lines.find("/usr/bin/systemctl --user start")
    baseline_at = lines.find("LastTriggerUSec")
    assert baseline_at >= 0 and start_at >= 0
    assert baseline_at < start_at


def test_g8_baseline_after_start_is_refused() -> None:
    bad = """
/usr/bin/systemctl --user start "$UNIT"
TIMER_BEFORE="$(/usr/bin/systemctl --user show nhms-node27-timeseries-compression.timer -p LastTriggerUSec --value)"
SERVICE_BEFORE="$(/usr/bin/systemctl --user show nhms-node27-timeseries-compression.service \\
  -p ExecMainStartTimestamp --value)"
"""
    with pytest.raises(Issue1895ReadinessError) as caught:
        assert_baseline_before_start(bad)
    assert caught.value.code == "TIMER_BASELINE_AFTER_START"


def test_rollback_before_mutation_is_noop_and_does_not_reference_units_file() -> None:
    action = rollback_action(POINT_BEFORE_MUTATION, units_enable_state_exists=False)
    assert action["action"] == "no-op"
    assert action["reference_units_enable_state"] is False
    with pytest.raises(Issue1895ReadinessError) as premature:
        rollback_action(POINT_BEFORE_MUTATION, units_enable_state_exists=True)
    assert premature.value.code == "ROLLBACK_STATE_PREMATURE"
    after = rollback_action(
        POINT_AFTER_DRAIN,
        units_enable_state_exists=True,
        enablement_text="nhms-node27-autopipe.timer enabled\n",
    )
    assert after["action"] == "restore_captured_enablement"
    assert after["restore_units"] == (("nhms-node27-autopipe.timer", "enabled"),)
    moved = rollback_action(
        POINT_AFTER_MOVEMENT,
        units_enable_state_exists=True,
        enablement_text="nhms-node27-autopipe.timer enabled\n",
    )
    assert moved["action"] == "three_state_reverse_preserve"
    assert_row_state_consistency(
        point=POINT_BEFORE_MUTATION,
        units_enable_state_exists=False,
        enablement_text=None,
        expected_action="no-op",
    )
    table = _gate("G8") if "Before any mutation" in _gate("G8") else ""
    section = Path(REPO_ROOT / "docs/runbooks/tier-node27-timeseries-storage.md").read_text(encoding="utf-8")
    start = section.index("#### Rollback points")
    table = section[start : section.index("## Timer cadence order (UTC)")]
    before = [line for line in table.splitlines() if line.startswith("| Before any mutation")]
    assert before, table
    assert "no-op" in before[0].lower()
    assert "does not exist yet" in before[0]
    assert "must not be referenced" in before[0]


def test_json_load_open_path_is_the_current_form() -> None:
    assert python_uses_json_load_on_path('report = parse_probe_report(json.load(open(path)))')
    assert not python_uses_json_load_on_path('report = parse_probe_report(path)')
