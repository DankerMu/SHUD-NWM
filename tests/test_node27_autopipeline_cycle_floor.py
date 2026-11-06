"""Cycle floor for the node-27 autopipe tick (#3062, ``AUTOPIPE_MAX_CYCLE_AGE_DAYS``).

Operator decision (2026-11-06): the cron tick only ingests cycles from the last
two weeks; older cycles are ingested by hand with ``--only-cycle``. Unset keeps
the historical no-floor behaviour; an invalid value blocks the tick at
preflight instead of silently ingesting (or silently dropping) everything.

The clock is pinned to 2026-07-04T12:00Z, so 14 days gives the floor cycle
``2026062012`` -- exactly ``RUN_A``'s cycle (the equal-boundary case).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts import node27_autopipeline as autopipe
from tests.test_node27_autopipeline_handoff import (
    RUN_A,
    RUN_B,
    _command_kinds,
    _prepare_autopipe,
    _run_main,
)

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
FLOOR_CYCLE = "2026062012"
OLD_LEGACY = "fcst_gfs_2026062006_basins_qhh_shud"
OLD_DIRECT_GRID = "fcst_gfs_2026061900_dg_0123456789abcdef"
OLD_OTHER_BASIN = "fcst_gfs_2026061512_basins_heihe_shud"


def _pin_clock(monkeypatch: pytest.MonkeyPatch, now: datetime = NOW) -> None:
    # raising=False: the red run against pre-#3062 source must fail on behaviour, not on a missing seam.
    monkeypatch.setattr(autopipe, "_utc_now", lambda: now, raising=False)


def _ingested_run_ids(summary: dict[str, object]) -> list[str]:
    runs = summary["runs"]
    assert isinstance(runs, dict)
    return [detail["run_id"] for detail in runs["details"] if detail["outcome"] == "ingested"]


def test_cycle_floor_drops_older_runs_and_keeps_the_boundary_and_newer_cycles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, calls, _published = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={OLD_LEGACY: True, OLD_DIRECT_GRID: True, RUN_A: True, RUN_B: True},
    )
    _pin_clock(monkeypatch)
    monkeypatch.setenv("AUTOPIPE_MAX_CYCLE_AGE_DAYS", "14")

    rc, summary = _run_main(capsys, root)

    assert rc == 0
    assert summary["cycle_floor"] == {
        "days": 14,
        "floor_cycle": FLOOR_CYCLE,
        "applied": True,
        "bypassed_by_only_cycle": False,
    }
    assert summary["skipped_older_than_floor"] == 2
    assert summary["discovered_runs"] == 2
    assert _ingested_run_ids(summary) == [RUN_A, RUN_B]
    assert _command_kinds(calls) == ["register", "parse", "coverage"] * 2
    assert not any(OLD_LEGACY in " ".join(argv) or OLD_DIRECT_GRID in " ".join(argv) for argv in calls)


def test_cycle_floor_truncates_to_the_hour_so_the_current_floor_cycle_stays_in_window(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, _calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={OLD_LEGACY: True, RUN_A: True})
    _pin_clock(monkeypatch, datetime(2026, 7, 4, 12, 59, 59, tzinfo=UTC))
    monkeypatch.setenv("AUTOPIPE_MAX_CYCLE_AGE_DAYS", "14")

    rc, summary = _run_main(capsys, root)

    assert rc == 0
    assert summary["cycle_floor"]["floor_cycle"] == FLOOR_CYCLE
    assert summary["skipped_older_than_floor"] == 1
    assert _ingested_run_ids(summary) == [RUN_A]


@pytest.mark.parametrize("raw", [None, "", "   "], ids=["unset", "empty", "blank"])
def test_unset_cycle_floor_keeps_every_discovered_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    raw: str | None,
) -> None:
    root, calls, _published = _prepare_autopipe(
        monkeypatch,
        tmp_path,
        runs={OLD_LEGACY: True, RUN_A: True, RUN_B: True},
    )
    _pin_clock(monkeypatch)
    if raw is None:
        monkeypatch.delenv("AUTOPIPE_MAX_CYCLE_AGE_DAYS", raising=False)
    else:
        monkeypatch.setenv("AUTOPIPE_MAX_CYCLE_AGE_DAYS", raw)

    rc, summary = _run_main(capsys, root)

    assert rc == 0
    assert summary["cycle_floor"] == {
        "days": None,
        "floor_cycle": None,
        "applied": False,
        "bypassed_by_only_cycle": False,
    }
    assert summary["skipped_older_than_floor"] == 0
    assert summary["discovered_runs"] == 3
    assert _ingested_run_ids(summary) == [OLD_LEGACY, RUN_A, RUN_B]
    assert _command_kinds(calls) == ["register", "parse", "coverage"] * 3


def test_explicit_only_cycle_bypasses_the_floor_for_manual_ingest_of_an_old_cycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={OLD_LEGACY: True, RUN_B: True})
    _pin_clock(monkeypatch)
    monkeypatch.setenv("AUTOPIPE_MAX_CYCLE_AGE_DAYS", "14")

    rc, summary = _run_main(capsys, root, "--only-cycle", "2026062006")

    assert rc == 0
    assert summary["cycle_floor"] == {
        "days": 14,
        "floor_cycle": FLOOR_CYCLE,
        "applied": False,
        "bypassed_by_only_cycle": True,
    }
    assert summary["skipped_older_than_floor"] == 0
    assert summary["discovered_runs"] == 1
    assert _ingested_run_ids(summary) == [OLD_LEGACY]
    assert _command_kinds(calls) == ["register", "parse", "coverage"]


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "14.5", "1_4", "+14", "14d"])
@pytest.mark.parametrize("only_cycle", [False, True], ids=["cron-tick", "only-cycle"])
def test_invalid_cycle_floor_blocks_the_tick_at_preflight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    raw: str,
    only_cycle: bool,
) -> None:
    root, calls, published = _prepare_autopipe(monkeypatch, tmp_path, runs={OLD_LEGACY: True, RUN_A: True})
    _pin_clock(monkeypatch)
    monkeypatch.setenv("AUTOPIPE_MAX_CYCLE_AGE_DAYS", raw)

    extra = ("--only-cycle", "2026062006") if only_cycle else ()
    rc, summary = _run_main(capsys, root, *extra)

    assert rc == autopipe.PREFLIGHT_BLOCKED_RC == 2
    assert summary["status"] == "preflight_blocked"
    blockers = summary["ingest"]["preflight"]["blockers"]
    assert [b["code"] for b in blockers] == ["AUTOPIPE_MAX_CYCLE_AGE_DAYS_INVALID"]
    assert blockers[0]["env_var"] == "AUTOPIPE_MAX_CYCLE_AGE_DAYS"
    assert summary["discovered_runs"] == 0
    assert calls == []
    assert published == []


def test_run_older_than_the_floor_cannot_drive_seed_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, calls, _published = _prepare_autopipe(monkeypatch, tmp_path, runs={OLD_OTHER_BASIN: True, RUN_A: True})
    _pin_clock(monkeypatch)
    monkeypatch.setenv("AUTOPIPE_MAX_CYCLE_AGE_DAYS", "14")
    seeded_checks: list[str] = []

    def basin_seeded(_database_url: str, basin_id: str) -> bool:
        seeded_checks.append(basin_id)
        return True

    monkeypatch.setattr(autopipe, "_basin_seeded", basin_seeded)

    rc, summary = _run_main(capsys, root)

    assert rc == 0
    assert summary["basins"] == ["qhh"]
    assert seeded_checks == ["basins_qhh"]
    assert [detail["basin"] for detail in summary["seed"]["details"]] == ["qhh"]
    assert summary["skipped_older_than_floor"] == 1
    assert _ingested_run_ids(summary) == [RUN_A]
    assert _command_kinds(calls) == ["register", "parse", "coverage"]
