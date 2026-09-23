"""``scripts/node27_parse_failure_residency_alert.py`` (#2529 design D5).

The lane alerts on RESIDENCY, not on a tick's rc: a run that stays
``failed`` with an ``OUTPUT_PARSE_*`` code across observations for at least the
threshold (default 2 h) makes the unit exit 1 — the existing
``OnFailure=nhms-node27-unit-failure-alert@%n.service`` handler mails the
journal tail, so the report on stdout IS the mail body. A burst that heals
within the threshold (the 2026-09-19 57014 shape: failing for one or two ticks,
then ``parsed``) stays quiet.

Seams: ``main(argv, now=..., observe=..., env=...)`` with an injected
observation and clock; the real ``default_observe`` SQL is executed against
PostgreSQL by the one integration-marked test at the bottom. Expected values
come from the design's rules (threshold / re-alert / liveness arithmetic on the
injected clock), not from the implementation.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest

from scripts import node27_parse_failure_residency_alert as alert

T0 = datetime(2026, 9, 19, 5, 55, tzinfo=UTC)
TICK = timedelta(minutes=30)
DSN = "postgresql://nhms_display_ro:s3cretpw@127.0.0.1:55432/nhms"
_ROOT = Path(__file__).resolve().parents[1]
_SERVICE_PATH = _ROOT / "infra/systemd/nhms-node27-parse-failure-residency-alert.service"
_TIMER_PATH = _ROOT / "infra/systemd/nhms-node27-parse-failure-residency-alert.timer"
_ENV_EXAMPLE_PATH = _ROOT / "infra/env/node27-parse-failure-residency-alert.example"
_AUTOPIPE_SERVICE_PATH = _ROOT / "infra/systemd/nhms-node27-autopipe.service"

#: The unit-failure handler mails `journalctl -n ${NHMS_UNIT_FAILURE_JOURNAL_LINES:-30}`
#: of the failed unit; on the exit-1 path systemd itself contributes five
#: framing lines (Starting / Main process exited / Failed with result /
#: Failed to start / Triggering OnFailure=), the budget
#: scripts/node27_coverage_freshness_alert.py measured for the same handler.
JOURNAL_LINES = 30
SYSTEMD_FRAMING_LINES = 5


def _row(run_id: str, *, code: str = "OUTPUT_PARSE_DB_ERROR", touched: datetime | None = None) -> Any:
    return alert.FailingRun(run_id=run_id, run_key=1, error_code=code, updated_at=touched or T0)


class _Observer:
    """Answers each call with the next scripted observation (a list of rows)."""

    def __init__(self, *observations: list[Any]) -> None:
        self.observations = list(observations)
        self.floors: list[datetime] = []

    def __call__(self, config: Any, liveness_floor: datetime) -> list[Any]:
        self.floors.append(liveness_floor)
        return self.observations.pop(0)


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {"DATABASE_URL": DSN, "NHMS_PARSE_RESIDENCY_STATE_PATH": str(tmp_path / "state" / "state.json")}
    env.update(overrides)
    return env


def _tick(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    now: datetime,
    rows: list[Any] | Callable[..., Any],
    **env_overrides: str,
) -> tuple[int, list[str]]:
    observe = rows if callable(rows) else _Observer(rows)
    rc = alert.main([], now=now, observe=observe, env=_env(tmp_path, **env_overrides))
    return rc, capsys.readouterr().out.splitlines()


def _state(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# D5 criterion.
# ---------------------------------------------------------------------------


def test_a_run_failing_across_observations_for_the_threshold_alerts_and_is_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The #1781 shape: never heals. Exit 1 once residency reaches 2 h."""
    for step in range(4):  # 0, 30, 60, 90 min: failing, not yet resident
        rc, _ = _tick(tmp_path, capsys, T0 + step * TICK, [_row("run-perm", touched=T0 + step * TICK)])
        assert rc == 0, step

    rc, lines = _tick(tmp_path, capsys, T0 + 4 * TICK, [_row("run-perm", touched=T0 + 4 * TICK)])

    assert rc == 1
    assert lines[0].startswith("parse-failure-residency ")
    assert "resident=1 newly_alerted=1 already_alerted=0" in lines[0]
    assert any(
        line.startswith("resident run_id=run-perm error_code=OUTPUT_PARSE_DB_ERROR ")
        and "first_observed=2026-09-19T05:55:00Z" in line
        and "residency_h=2.0" in line
        for line in lines
    ), lines
    assert _state(tmp_path)["runs"]["run-perm"] == {
        "first_observed_failing_at": "2026-09-19T05:55:00Z",
        "last_alerted_at": "2026-09-19T07:55:00Z",
    }


def test_the_self_healing_burst_of_2026_09_19_never_alerts_and_is_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Failing in one or two observations, then parsed (absent from the
    failed set): exit 0 throughout, and the healed run leaves the state."""
    burst = [_row(f"run-{index}") for index in range(7)]
    assert _tick(tmp_path, capsys, T0, burst)[0] == 0
    assert _tick(tmp_path, capsys, T0 + TICK, burst[:4])[0] == 0
    assert sorted(_state(tmp_path)["runs"]) == [f"run-{index}" for index in range(4)]

    rc, lines = _tick(tmp_path, capsys, T0 + 2 * TICK, [])

    assert rc == 0
    assert "watched=0 resident=0" in lines[0]
    assert _state(tmp_path)["runs"] == {}


def test_an_already_alerted_resident_is_quiet_until_the_realert_interval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_path = tmp_path / "state" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runs": {"run-perm": {"first_observed_failing_at": "2026-09-19T02:55:00Z", "last_alerted_at": None}},
            }
        ),
        encoding="utf-8",
    )

    assert _tick(tmp_path, capsys, T0, [_row("run-perm")])[0] == 1

    within = T0 + timedelta(hours=23, minutes=30)
    rc, lines = _tick(tmp_path, capsys, within, [_row("run-perm", touched=within)])
    assert rc == 0
    # Still listed, so an operator reading any tick's journal sees it.
    assert "resident=1 newly_alerted=0 already_alerted=1" in lines[0]
    assert any(line.startswith("resident run_id=run-perm ") for line in lines)

    after = T0 + timedelta(hours=24)
    rc, lines = _tick(tmp_path, capsys, after, [_row("run-perm", touched=after)])
    assert rc == 1
    assert "newly_alerted=1" in lines[0]
    assert _state(tmp_path)["runs"]["run-perm"]["last_alerted_at"] == "2026-09-20T05:55:00Z"


def test_a_failed_row_not_touched_within_retry_liveness_is_not_watched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The 2 abandoned OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED rows of 2026-08-28:
    the autopipe stopped retrying them, so they are not a residency signal and
    must not mail every 24 h forever."""
    abandoned = _row("run-abandoned", code="OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED", touched=T0 - timedelta(days=26))
    edge = _row("run-edge", touched=T0 - timedelta(hours=6))
    observer = _Observer([abandoned, edge], [abandoned, edge])
    state_path = tmp_path / "state" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runs": {
                    "run-abandoned": {"first_observed_failing_at": "2026-08-28T08:42:56Z", "last_alerted_at": None}
                },
            }
        ),
        encoding="utf-8",
    )

    rc, lines = _tick(tmp_path, capsys, T0, observer)

    assert rc == 0
    assert "watched=0 resident=0" in lines[0]
    assert _state(tmp_path)["runs"] == {}
    # The SQL bound and the in-process bound are the same instant.
    assert observer.floors == [T0 - timedelta(hours=6)]


def test_the_liveness_bound_is_configurable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    observer = _Observer([_row("run-a", touched=T0 - timedelta(hours=7))])

    rc, lines = _tick(tmp_path, capsys, T0, observer, NHMS_PARSE_RESIDENCY_RETRY_LIVENESS_HOURS="8")

    assert rc == 0
    assert "watched=1" in lines[0]
    assert observer.floors == [T0 - timedelta(hours=8)]


def test_a_zero_threshold_makes_the_first_observation_resident(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The live-receipt knob (5.7): a receipt cannot wait two hours."""
    rc, lines = _tick(tmp_path, capsys, T0, [_row("run-seeded")], NHMS_PARSE_RESIDENCY_THRESHOLD_HOURS="0")

    assert rc == 1
    assert any(line.startswith("resident run_id=run-seeded ") for line in lines)

    rc, lines = _tick(tmp_path, capsys, T0 + TICK, [], NHMS_PARSE_RESIDENCY_THRESHOLD_HOURS="0")
    assert rc == 0
    assert _state(tmp_path)["runs"] == {}


def test_the_report_fits_the_handler_journal_budget_with_fifty_residents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [_row(f"run-{index:02d}") for index in range(50)]
    rc, lines = _tick(tmp_path, capsys, T0, rows, NHMS_PARSE_RESIDENCY_THRESHOLD_HOURS="0")

    assert rc == 1
    assert len(lines) + SYSTEMD_FRAMING_LINES <= JOURNAL_LINES
    assert len([line for line in lines if line.startswith("resident run_id=")]) == 10
    assert lines[-1] == "... and 40 more"
    assert "resident=50 newly_alerted=50" in lines[0]


# ---------------------------------------------------------------------------
# Exit 2: config / database / state, typed and traceback-free.
# ---------------------------------------------------------------------------


def _assert_typed(lines: list[str], code: str) -> None:
    assert len(lines) == 1, lines
    assert lines[0].startswith(f"PARSE_FAILURE_RESIDENCY_{code} reason="), lines
    assert "Traceback" not in lines[0]
    assert "s3cretpw" not in lines[0]


def test_a_corrupt_state_file_is_exit_2_and_is_left_for_the_operator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state_path = tmp_path / "state" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not json", encoding="utf-8")

    rc, lines = _tick(tmp_path, capsys, T0, [_row("run-a")])

    assert rc == 2
    _assert_typed(lines, "STATE_CORRUPT")
    assert state_path.read_text(encoding="utf-8") == "{not json"


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "runs": {}},
        {"schema_version": 1, "runs": []},
        {"schema_version": 1, "runs": {"r": {"first_observed_failing_at": "yesterday", "last_alerted_at": None}}},
        {"schema_version": 1, "runs": {"r": {"last_alerted_at": None}}},
        [],
    ],
)
def test_a_schema_mismatched_state_is_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], payload: Any
) -> None:
    state_path = tmp_path / "state" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    rc, lines = _tick(tmp_path, capsys, T0, [])

    assert rc == 2
    _assert_typed(lines, "STATE_CORRUPT")


def test_a_database_error_is_exit_2_with_the_password_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _broken(_config: Any, _floor: datetime) -> list[Any]:
        raise psycopg2.OperationalError(f'connection to server failed: "{DSN}"')

    rc, lines = _tick(tmp_path, capsys, T0, _broken)

    assert rc == 2
    _assert_typed(lines, "OBSERVATION_FAILED")
    assert "OperationalError" in lines[0]
    # A failed observation must not wipe the residency clocks.
    assert not (tmp_path / "state" / "state.json").exists()


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"DATABASE_URL": ""}, "DATABASE_URL"),
        ({"NHMS_PARSE_RESIDENCY_THRESHOLD_HOURS": "-1"}, "THRESHOLD_HOURS"),
        ({"NHMS_PARSE_RESIDENCY_THRESHOLD_HOURS": "nan"}, "THRESHOLD_HOURS"),
        ({"NHMS_PARSE_RESIDENCY_REALERT_HOURS": "0"}, "REALERT_HOURS"),
        ({"NHMS_PARSE_RESIDENCY_RETRY_LIVENESS_HOURS": "inf"}, "RETRY_LIVENESS_HOURS"),
        ({"NHMS_PARSE_RESIDENCY_REPORT_RUNS": "0"}, "REPORT_RUNS"),
        ({"NHMS_PARSE_RESIDENCY_STATE_PATH": "relative/state.json"}, "STATE_PATH"),
    ],
)
def test_an_invalid_config_is_exit_2_before_observing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], override: dict[str, str], fragment: str
) -> None:
    def _never(_config: Any, _floor: datetime) -> list[Any]:
        raise AssertionError("observed despite an invalid config")

    rc, lines = _tick(tmp_path, capsys, T0, _never, **override)

    assert rc == 2
    _assert_typed(lines, "CONFIG_INVALID")
    assert fragment in lines[0]


def test_an_unwritable_state_directory_is_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("", encoding="utf-8")

    rc = alert.main(
        [],
        now=T0,
        observe=_Observer([_row("run-a")]),
        env={"DATABASE_URL": DSN, "NHMS_PARSE_RESIDENCY_STATE_PATH": str(blocker / "state.json")},
    )

    assert rc == 2
    _assert_typed(capsys.readouterr().out.splitlines(), "CONFIG_INVALID")


def test_cli_flags_override_the_env(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    other = tmp_path / "cli-state.json"
    observer = _Observer([_row("run-a")])

    rc = alert.main(
        ["--threshold-hours", "0", "--retry-liveness-hours", "1", "--state-path", str(other)],
        now=T0,
        observe=observer,
        env=_env(tmp_path, NHMS_PARSE_RESIDENCY_THRESHOLD_HOURS="5"),
    )

    assert rc == 1
    assert observer.floors == [T0 - timedelta(hours=1)]
    assert "run-a" in json.loads(other.read_text(encoding="utf-8"))["runs"]
    capsys.readouterr()


def test_a_held_lock_skips_the_tick_without_alerting(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = alert.config_from_env(_env(tmp_path))
    fd = alert.acquire_lock(config.lock_path)
    assert fd is not None
    try:
        rc, lines = _tick(tmp_path, capsys, T0, [_row("run-a")], NHMS_PARSE_RESIDENCY_THRESHOLD_HOURS="0")
    finally:
        alert.release_lock(fd)

    assert rc == 0
    assert lines == ["parse-failure-residency: another instance holds the lock; tick skipped"]


def test_the_state_file_is_replaced_atomically_and_private(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _tick(tmp_path, capsys, T0, [_row("run-a")])
    state_path = tmp_path / "state" / "state.json"

    assert oct(os.stat(state_path).st_mode & 0o777) == "0o600"
    assert not list(state_path.parent.glob(".*.tmp"))


# ---------------------------------------------------------------------------
# Units (design D5): journal stdio, OnFailure= handler, 30-min timer.
# ---------------------------------------------------------------------------


def _directives(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]


def test_the_service_mails_through_the_unit_failure_handler_with_the_journal_as_body() -> None:
    directives = _directives(_SERVICE_PATH)

    for directive in (
        "OnFailure=nhms-node27-unit-failure-alert@%n.service",
        "Type=oneshot",
        "WorkingDirectory=/home/nwm/NWM",
        # The read-only nhms_display_ro DSN, shared with the frontier and
        # coverage-freshness lanes (one copy of that secret).
        "EnvironmentFile=%h/NWM/infra/env/node27-frontier-alert.env",
        "EnvironmentFile=-%h/NWM/infra/env/node27-parse-failure-residency-alert.env",
        "Environment=PYTHONPATH=/home/nwm/NWM",
        "ExecStart=/home/nwm/NWM/.venv/bin/python /home/nwm/NWM/scripts/node27_parse_failure_residency_alert.py",
        "TimeoutStartSec=300",
    ):
        assert directive in directives, directive
    # The report is the mail body only while stdout/stderr reach the journal.
    assert not [line for line in directives if line.startswith(("StandardOutput=", "StandardError="))]


def test_the_timer_fires_every_thirty_minutes() -> None:
    directives = _directives(_TIMER_PATH)

    assert "OnCalendar=*:15/30" in directives
    assert "Persistent=true" in directives
    assert "Unit=nhms-node27-parse-failure-residency-alert.service" in directives
    assert "WantedBy=timers.target" in directives


def test_the_env_example_documents_every_knob_and_ships_them_commented() -> None:
    text = _ENV_EXAMPLE_PATH.read_text(encoding="utf-8")

    for key in (
        "NHMS_PARSE_RESIDENCY_THRESHOLD_HOURS",
        "NHMS_PARSE_RESIDENCY_REALERT_HOURS",
        "NHMS_PARSE_RESIDENCY_RETRY_LIVENESS_HOURS",
        "NHMS_PARSE_RESIDENCY_REPORT_RUNS",
        "NHMS_PARSE_RESIDENCY_STATE_PATH",
    ):
        assert f"# {key}=" in text, key
    # No active assignment: the defaults are the deployed values, and the DSN
    # lives in node27-frontier-alert.env.
    assert not [line for line in text.splitlines() if line and not line.startswith("#")]
    assert "nhms_display_ro" in text


def test_the_autopipe_unit_still_has_no_on_failure() -> None:
    """D5: every transient rc=1 tick would mail, into an `append:` body."""
    assert not [line for line in _directives(_AUTOPIPE_SERVICE_PATH) if line.startswith("OnFailure=")]


# ---------------------------------------------------------------------------
# The real observation statement, on PostgreSQL.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_the_observation_query_selects_live_output_parse_failures_only(throwaway_database_url: str) -> None:
    from tests.integration_helpers import apply_migrations_from_zero

    apply_migrations_from_zero(throwaway_database_url)
    connection = psycopg2.connect(throwaway_database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO core.basin (basin_id, basin_name) VALUES ('b-2529', 'b')")
            cursor.execute(
                "INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom, active_flag) "
                "VALUES ('bv-2529', 'b-2529', 'v1', ST_SetSRID(ST_GeomFromText("
                "'MULTIPOLYGON(((99 37, 99 39, 101 39, 101 37, 99 37)))'), 4490), true)"
            )
            cursor.execute(
                "INSERT INTO core.river_network_version (river_network_version_id, basin_version_id, "
                "version_label, segment_count) VALUES ('rnv-2529', 'bv-2529', 'v1', 1)"
            )
            cursor.execute(
                "INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id, "
                "mesh_version_id, calibration_version_id, shud_code_version, model_package_uri, active_flag, "
                "lifecycle_state) "
                "VALUES ('m-2529', 'bv-2529', 'rnv-2529', 'mesh', 'cal', '1.0', 's3://nhms/m', true, 'active')"
            )
            for run_id, status, code, age in (
                ("run-live", "failed", "OUTPUT_PARSE_DB_ERROR", "10 minutes"),
                ("run-live-blocked", "failed", "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED", "5 hours"),
                ("run-abandoned", "failed", "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED", "26 days"),
                ("run-other-stage", "failed", "FORCING_HANDOFF_FAILED", "10 minutes"),
                # `_` is a LIKE wildcard: this code must not match 'OUTPUT_PARSE\_%'.
                ("run-lookalike", "failed", "OUTPUT_PARSEXDB", "10 minutes"),
                ("run-healed", "parsed", "OUTPUT_PARSE_DB_ERROR", "10 minutes"),
            ):
                cursor.execute(
                    "INSERT INTO hydro.hydro_run (run_id, run_type, scenario_id, model_id, basin_version_id, "
                    "cycle_time, start_time, end_time, status, run_manifest_uri, error_code, updated_at) "
                    "VALUES (%s, 'forecast', 'sc', 'm-2529', 'bv-2529', now(), now(), now(), %s, 's3://m', %s, "
                    "now() - %s::interval)",
                    (run_id, status, code, age),
                )
    finally:
        connection.close()

    config = alert.config_from_env({"DATABASE_URL": throwaway_database_url})
    floor = datetime.now(UTC) - timedelta(hours=6)
    rows = alert.default_observe(config, floor)

    assert sorted((row.run_id, row.error_code) for row in rows) == [
        ("run-live", "OUTPUT_PARSE_DB_ERROR"),
        ("run-live-blocked", "OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED"),
    ]
    assert all(isinstance(row.run_key, int) and row.updated_at.tzinfo is not None for row in rows)
