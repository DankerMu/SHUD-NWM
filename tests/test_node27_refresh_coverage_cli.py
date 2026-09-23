"""``scripts/node27_refresh_coverage.py``: the #1446 refusal is an operator outcome.

The script is called two ways on node-27: per run by the autopipeline (which
branches on the return code) and as ``--all --skip-fresh`` by the cron backstop
(whose step is non-fatal). Both have to survive the overwrite guard firing:

* ``--run-id <legacy run>`` -> exit 3 and one structured stderr line, no
  traceback and no JSON report on stdout;
* ``--force`` -> the zeroing is performed and reported normally;
* ``--all`` -> refusals are counted in the JSON report and the exit code stays
  0, so the cron backstop is not turned into a failure by a protected run.

The library itself is covered by tests/test_display_coverage_refresh.py; here it
is stubbed so the assertions are about the CLI's contract only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import pytest

from packages.common import display_coverage
from packages.common.display_coverage import DisplayCoverageRefreshRefused
from scripts import node27_refresh_coverage

DSN = "postgresql://u:p@127.0.0.1:55432/nhms"
LEGACY_RUN_ID = "fcst_gfs_2026061312_basins_qhh_shud"


class _Cursor:
    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def execute(self, *args: Any, **kwargs: Any) -> None:
        return None

    def fetchone(self) -> Any:
        return None


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def cursor(self, *args: Any, **kwargs: Any) -> _Cursor:
        return _Cursor()

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def cli(monkeypatch: pytest.MonkeyPatch) -> list[_FakeConnection]:
    """The script's own connection, stubbed; the coverage table always exists."""
    connections: list[_FakeConnection] = []

    def _connect(*_args: Any, **_kwargs: Any) -> _FakeConnection:
        connection = _FakeConnection()
        connections.append(connection)
        return connection

    monkeypatch.setattr(psycopg2, "connect", _connect)
    monkeypatch.setattr(node27_refresh_coverage, "run_display_coverage_available", lambda _cursor: True)
    return connections


def test_refused_single_run_exits_3_with_one_structured_stderr_line(
    cli: list[_FakeConnection],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The real advice text, not a stand-in: the one-line contract below is only
    # worth anything if it is the shipped string that has to stay newline-free.
    def _refuse(_connection: Any, run_id: str, *, force: bool = False, expired_cutoff: Any = None) -> bool:
        raise DisplayCoverageRefreshRefused(run_id, 12, display_coverage._REFUSAL_ADVICE)

    monkeypatch.setattr(node27_refresh_coverage, "refresh_run_display_coverage", _refuse)

    rc = node27_refresh_coverage.main(["--run-id", LEGACY_RUN_ID, "--database-url", DSN])

    assert rc == 3
    captured = capsys.readouterr()
    # No JSON report: the run was not refreshed, so nothing may claim it was.
    assert captured.out == ""
    assert captured.err.splitlines() == [
        f"DISPLAY_COVERAGE_REFRESH_REFUSED run_id={LEGACY_RUN_ID} "
        f"existing_segment_count=12 advice={display_coverage._REFUSAL_ADVICE}"
    ]
    # The refusal is a return, not a raise: the connection still gets closed.
    assert [connection.closed for connection in cli] == [True]


def test_force_reaches_the_library_and_reports_normally(
    cli: list[_FakeConnection],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[tuple[str, bool]] = []

    def _refresh(_connection: Any, run_id: str, *, force: bool = False, expired_cutoff: Any = None) -> bool:
        seen.append((run_id, force))
        return True

    monkeypatch.setattr(node27_refresh_coverage, "refresh_run_display_coverage", _refresh)

    rc = node27_refresh_coverage.main(["--run-id", LEGACY_RUN_ID, "--force", "--database-url", DSN])

    assert rc == 0
    assert seen == [(LEGACY_RUN_ID, True)]
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "run"
    assert report["run_id"] == LEGACY_RUN_ID
    assert report["refreshed"] is True


def test_single_run_without_force_asks_the_library_not_to_force(
    cli: list[_FakeConnection],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[tuple[str, bool]] = []

    def _refresh(_connection: Any, run_id: str, *, force: bool = False, expired_cutoff: Any = None) -> bool:
        seen.append((run_id, force))
        return True

    monkeypatch.setattr(node27_refresh_coverage, "refresh_run_display_coverage", _refresh)

    assert node27_refresh_coverage.main(["--run-id", LEGACY_RUN_ID, "--database-url", DSN]) == 0
    assert seen == [(LEGACY_RUN_ID, False)]
    assert capsys.readouterr().err == ""


def test_all_reports_refusals_and_still_exits_zero(
    cli: list[_FakeConnection],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The cron backstop must not be turned into a failure by a protected run."""
    seen: list[bool] = []

    def _refresh_all(_connection: Any, **kwargs: Any) -> dict[str, int]:
        seen.append(kwargs["force"])
        return {"refreshed": 4, "skipped": 1, "failed": 0, "refused": 2}

    monkeypatch.setattr(node27_refresh_coverage, "refresh_all_run_display_coverage", _refresh_all)

    rc = node27_refresh_coverage.main(["--all", "--skip-fresh", "--database-url", DSN])

    assert rc == 0
    assert seen == [False]
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "all"
    assert report["skip_fresh"] is True
    assert report["refused"] == 2
    assert report["refreshed"] == 4
    assert report["failed"] == 0
    assert report["skipped"] == 1


def test_all_force_passes_the_flag_through(
    cli: list[_FakeConnection],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[bool] = []

    def _refresh_all(_connection: Any, **kwargs: Any) -> dict[str, int]:
        seen.append(kwargs["force"])
        return {"refreshed": 1, "skipped": 0, "failed": 0, "refused": 0}

    monkeypatch.setattr(node27_refresh_coverage, "refresh_all_run_display_coverage", _refresh_all)

    assert node27_refresh_coverage.main(["--all", "--force", "--database-url", DSN]) == 0
    assert seen == [True]
    assert json.loads(capsys.readouterr().out)["refused"] == 0


# ---------------------------------------------------------------------------
# #2504 D6: the window comes from the refresh's own env, the cutoff is resolved
# once per invocation, and the audit is a read-only report.
# ---------------------------------------------------------------------------

_WINDOW_ENV = {"NODE27_TIMESERIES_RETENTION_WINDOW_DAYS": "21"}
_CUTOFF = datetime(2026, 9, 1, 6, tzinfo=UTC)


@pytest.fixture()
def cutoff_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def _resolve(dsn: str, window_days: Any, *, connect: Any = None) -> Any:
        calls.append((dsn, window_days, connect))
        return None if window_days is None else _CUTOFF

    monkeypatch.setattr(node27_refresh_coverage, "resolve_expired_cutoff", _resolve)
    return calls


def test_all_resolves_the_cutoff_once_from_the_window_and_hands_it_to_the_batch(
    cli: list[_FakeConnection],
    cutoff_calls: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[dict[str, Any]] = []

    def _refresh_all(_connection: Any, **kwargs: Any) -> dict[str, int]:
        seen.append(kwargs)
        return {"refreshed": 1, "skipped": 0, "failed": 0, "refused": 0}

    monkeypatch.setattr(node27_refresh_coverage, "refresh_all_run_display_coverage", _refresh_all)

    rc = node27_refresh_coverage.main(["--all", "--skip-fresh", "--database-url", DSN], env=_WINDOW_ENV)

    assert rc == 0
    assert cutoff_calls == [(DSN, 21, node27_refresh_coverage._attributed_connect)]
    assert [call["expired_cutoff"] for call in seen] == [_CUTOFF]
    assert seen[0]["expired_rescan_interval"] == timedelta(hours=24)
    report = json.loads(capsys.readouterr().out)
    assert report["expired_cutoff"] == "2026-09-01T06:00:00Z"


def test_without_the_window_the_refresh_is_not_relaxed(
    cli: list[_FakeConnection],
    cutoff_calls: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fail-closed (D6): no window in the refresh's env -> no cutoff, and the
    DEFAULT_RETENTION_WINDOW_DAYS fallback is never used."""
    seen: list[Any] = []

    def _refresh_all(_connection: Any, **kwargs: Any) -> dict[str, int]:
        seen.append(kwargs["expired_cutoff"])
        return {"refreshed": 0, "skipped": 0, "failed": 0, "refused": 1}

    monkeypatch.setattr(node27_refresh_coverage, "refresh_all_run_display_coverage", _refresh_all)

    assert node27_refresh_coverage.main(["--all", "--skip-fresh", "--database-url", DSN], env={}) == 0
    assert cutoff_calls == [(DSN, None, node27_refresh_coverage._attributed_connect)]
    assert seen == [None]
    assert json.loads(capsys.readouterr().out)["expired_cutoff"] is None


def test_single_run_refresh_gets_the_cutoff_too(
    cli: list[_FakeConnection],
    cutoff_calls: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[Any] = []

    def _refresh(_connection: Any, run_id: str, *, force: bool = False, expired_cutoff: Any = None) -> bool:
        seen.append(expired_cutoff)
        return True

    monkeypatch.setattr(node27_refresh_coverage, "refresh_run_display_coverage", _refresh)

    assert node27_refresh_coverage.main(["--run-id", LEGACY_RUN_ID, "--database-url", DSN], env=_WINDOW_ENV) == 0
    assert seen == [_CUTOFF]
    assert json.loads(capsys.readouterr().out)["expired_cutoff"] == "2026-09-01T06:00:00Z"


def test_expired_rescan_hours_reaches_the_batch(
    cli: list[_FakeConnection],
    cutoff_calls: list[tuple[Any, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def _refresh_all(_connection: Any, **kwargs: Any) -> dict[str, int]:
        seen.append(kwargs["expired_rescan_interval"])
        return {"refreshed": 0, "skipped": 0, "failed": 0, "refused": 0}

    monkeypatch.setattr(node27_refresh_coverage, "refresh_all_run_display_coverage", _refresh_all)

    argv = ["--all", "--skip-fresh", "--expired-rescan-hours", "6", "--database-url", DSN]
    assert node27_refresh_coverage.main(argv, env=_WINDOW_ENV) == 0
    assert seen == [timedelta(hours=6)]


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "x"])
def test_expired_rescan_hours_refuses_a_non_positive_or_non_finite_value(
    cli: list[_FakeConnection], value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        node27_refresh_coverage.main(["--all", "--expired-rescan-hours", value, "--database-url", DSN], env={})
    assert excinfo.value.code == 2
    assert "--expired-rescan-hours" in capsys.readouterr().err


def test_audit_prints_the_bucketed_report_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[tuple[Any, ...]] = []
    report = {
        "mode": "audit-populated-empty",
        "in_window": {"total": 3, "empty": 1, "probe_failed": 0, "sample_empty_run_ids": ["r1"]},
        "out_of_window": {"total": 5, "empty": 4, "probe_failed": 0, "sample_empty_run_ids": ["r2"]},
        "null_end": {"total": 2},
        "watermark": "2026-09-22T12:00:00Z",
        "cutoff": "2026-09-01T12:00:00Z",
        "window_days": 21,
    }

    def _audit(dsn: str, window_days: int, **_kwargs: Any) -> dict[str, Any]:
        seen.append((dsn, window_days))
        return dict(report)

    monkeypatch.setattr(node27_refresh_coverage, "audit_populated_empty", _audit)

    assert node27_refresh_coverage.main(["--audit-populated-empty", "--database-url", DSN], env=_WINDOW_ENV) == 0
    assert seen == [(DSN, 21)]
    printed = json.loads(capsys.readouterr().out)
    assert {key: printed[key] for key in report} == report


def test_audit_without_a_window_is_a_typed_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        node27_refresh_coverage, "audit_populated_empty", lambda *_a, **_k: pytest.fail("audit must not run")
    )

    assert node27_refresh_coverage.main(["--audit-populated-empty", "--database-url", DSN], env={}) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("DISPLAY_COVERAGE_AUDIT_FAILED reason=")
    assert "NODE27_TIMESERIES_RETENTION_WINDOW_DAYS" in captured.err


def test_audit_database_error_is_a_typed_exit_2_without_the_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _audit(dsn: str, window_days: int, **_kwargs: Any) -> dict[str, Any]:
        raise psycopg2.OperationalError(f'connection to "{dsn}" failed')

    monkeypatch.setattr(node27_refresh_coverage, "audit_populated_empty", _audit)

    assert node27_refresh_coverage.main(["--audit-populated-empty", "--database-url", DSN], env=_WINDOW_ENV) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert captured.err.startswith("DISPLAY_COVERAGE_AUDIT_FAILED reason=OperationalError")
    assert ":p@" not in captured.err
    assert "Traceback" not in captured.err


def test_audit_is_exclusive_with_the_refresh_modes(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        node27_refresh_coverage.main(["--audit-populated-empty", "--all", "--database-url", DSN], env=_WINDOW_ENV)
    assert excinfo.value.code == 2
