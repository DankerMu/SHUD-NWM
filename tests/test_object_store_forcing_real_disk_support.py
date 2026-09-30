"""Local proof of the real-disk suite's run-time cycle selection (#2595).

``latest_complete_cycle`` decides which cycle the node-27 ``real_disk`` suite
reads, so its choice is pinned here without that environment: temporary
``OBJECT_STORE_ROOT`` trees laid out exactly as the production resolver expects,
and a fake station lookup in place of ``PsycopgStationLookup``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.object_store_forcing_real_disk_support import MIN_SETTLED_AGE_SECONDS, latest_complete_cycle

COMBOS = (
    ("heihe_forc_001", "IFS", "basins_heihe_shud"),
    ("heihe_forc_001", "gfs", "basins_heihe_shud"),
    ("qhh_forc_001", "IFS", "basins_qhh_shud"),
    ("qhh_forc_001", "gfs", "basins_qhh_shud"),
)
_STATIONS = {
    "heihe_forc_001": ("heihe_bv_1", "heihe_forc_001.csv"),
    "qhh_forc_001": ("qhh_bv_1", "qhh_forc_001.csv"),
}
_NOW = time.time()
_SETTLED = _NOW - MIN_SETTLED_AGE_SECONDS - 60


@dataclass(frozen=True)
class _Station:
    basin_version_id: str
    forcing_filename: str


class _FakeLookup:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def lookup(self, station_id: str) -> _Station:
        self.calls.append(station_id)
        basin_version_id, forcing_filename = _STATIONS[station_id]
        return _Station(basin_version_id, forcing_filename)


def _csv_path(root: Path, cycle: str, station_id: str, source_id: str, model_id: str) -> Path:
    # Written out by hand, not through the resolver under test's own helper, so a
    # resolver drift (source case, segment order) reds here instead of agreeing
    # with itself.
    basin_version_id, forcing_filename = _STATIONS[station_id]
    return root / "forcing" / source_id.lower() / cycle / basin_version_id / model_id / "shud" / forcing_filename


def _write(root: Path, cycle: str, combos=COMBOS, *, mtime: float = _SETTLED) -> None:
    for station_id, source_id, model_id in combos:
        path = _csv_path(root, cycle, station_id, source_id, model_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("1\t6\t20260101\t20260101\n", encoding="utf-8")
        os.utime(path, (mtime, mtime))


def test_the_newest_cycle_with_every_combo_wins(tmp_path: Path) -> None:
    for cycle in ("2026092712", "2026092800", "2026092812"):
        _write(tmp_path, cycle)
    lookup = _FakeLookup()

    assert latest_complete_cycle(tmp_path, COMBOS, lookup, now=_NOW) == "2026-09-28T12:00:00Z"
    # One lookup per combo, not per candidate cycle: the real lookup is a DB round trip.
    assert lookup.calls == [station_id for station_id, _, _ in COMBOS]


def test_a_newer_cycle_missing_one_combo_is_skipped_over(tmp_path: Path) -> None:
    _write(tmp_path, "2026092800")
    _write(tmp_path, "2026092812", combos=COMBOS[:3])

    assert latest_complete_cycle(tmp_path, COMBOS, _FakeLookup(), now=_NOW) == "2026-09-28T00:00:00Z"


def test_a_cycle_whose_file_is_still_too_fresh_is_excluded(tmp_path: Path) -> None:
    _write(tmp_path, "2026092800")
    _write(tmp_path, "2026092812")
    fresh = _csv_path(tmp_path, "2026092812", *COMBOS[2])
    just_written = _NOW - MIN_SETTLED_AGE_SECONDS + 30
    os.utime(fresh, (just_written, just_written))

    assert latest_complete_cycle(tmp_path, COMBOS, _FakeLookup(), now=_NOW) == "2026-09-28T00:00:00Z"


def test_a_file_exactly_at_the_settle_age_counts(tmp_path: Path) -> None:
    _write(tmp_path, "2026092812", mtime=_NOW - MIN_SETTLED_AGE_SECONDS)

    assert latest_complete_cycle(tmp_path, COMBOS, _FakeLookup(), now=_NOW) == "2026-09-28T12:00:00Z"


def test_non_cycle_directories_are_not_candidates(tmp_path: Path) -> None:
    _write(tmp_path, "2026092800")
    # Same layout, but neither name is a YYYYMMDDHH cycle, so neither may win.
    _write(tmp_path, "latest")
    _write(tmp_path, "2026133100")

    assert latest_complete_cycle(tmp_path, COMBOS, _FakeLookup(), now=_NOW) == "2026-09-28T00:00:00Z"


def test_an_empty_intersection_fails_with_the_diagnostic(tmp_path: Path) -> None:
    # Every combo is present in SOME cycle, none in all of them.
    _write(tmp_path, "2026092800", combos=COMBOS[:2])
    _write(tmp_path, "2026092812", combos=COMBOS[2:])
    _write(tmp_path, "2026092900", combos=COMBOS[1:3])

    with pytest.raises(pytest.fail.Exception) as failure:
        latest_complete_cycle(tmp_path, COMBOS, _FakeLookup(), now=_NOW)

    message = str(failure.value)
    assert message.startswith("real store has no cycle where all 4 combos are present")
    assert f"root={tmp_path}" in message
    assert (
        "per-combo counts={'heihe_forc_001/IFS/basins_heihe_shud': 1, "
        "'heihe_forc_001/gfs/basins_heihe_shud': 2, "
        "'qhh_forc_001/IFS/basins_qhh_shud': 2, "
        "'qhh_forc_001/gfs/basins_qhh_shud': 1}"
    ) in message


def test_a_store_without_forcing_fails_rather_than_skips(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match=r"per-combo counts=\{.*: 0, .*: 0, .*: 0, .*: 0\}"):
        latest_complete_cycle(tmp_path, COMBOS, _FakeLookup(), now=_NOW)
