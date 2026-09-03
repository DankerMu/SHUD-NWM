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
    def _refuse(_connection: Any, run_id: str, *, force: bool = False) -> bool:
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

    def _refresh(_connection: Any, run_id: str, *, force: bool = False) -> bool:
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

    def _refresh(_connection: Any, run_id: str, *, force: bool = False) -> bool:
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
