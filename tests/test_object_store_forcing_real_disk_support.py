"""Local proof of the real-disk suite's run-time selection (#2595, #2699).

``latest_complete_cycle`` decides which cycle, models and stations the node-27
``real_disk`` suite reads, so its choice is pinned here without that
environment: temporary ``OBJECT_STORE_ROOT`` trees laid out exactly as the
production resolver expects, a fake ``met.interp_weight`` resolver and a fake
station lookup in place of ``PsycopgStationLookup``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.object_store_forcing_real_disk_support import (
    MIN_SETTLED_AGE_SECONDS,
    ResolvedCombo,
    Selection,
    latest_complete_cycle,
)

COMBOS = (
    ("heihe_bv_1", "IFS"),
    ("heihe_bv_1", "gfs"),
    ("qhh_bv_1", "IFS"),
    ("qhh_bv_1", "gfs"),
)
# One Direct Grid model per combo; the station prefix is not derivable from the
# model id (nor the reverse), which is why the suite needs the resolver.
_MODELS = {
    ("heihe_bv_1", "IFS"): "dg_1111",
    ("heihe_bv_1", "gfs"): "dg_2222",
    ("qhh_bv_1", "IFS"): "dg_3333",
    ("qhh_bv_1", "gfs"): "dg_4444",
}
_STATION_BY_MODEL = {
    "dg_1111": "dg-ifs-aaaa::cell:11",
    "dg_2222": "dg-gfs-bbbb::cell:22",
    "dg_3333": "dg-ifs-cccc::cell:33",
    "dg_4444": "dg-gfs-dddd::cell:44",
}
_STATIONS = {
    "dg-ifs-aaaa::cell:11": ("heihe_bv_1", "station_00011.csv"),
    "dg-gfs-bbbb::cell:22": ("heihe_bv_1", "station_00022.csv"),
    "dg-ifs-cccc::cell:33": ("qhh_bv_1", "station_00033.csv"),
    "dg-gfs-dddd::cell:44": ("qhh_bv_1", "station_00044.csv"),
}
_NOW = time.time()
_SETTLED = _NOW - MIN_SETTLED_AGE_SECONDS - 60


@dataclass(frozen=True)
class _Station:
    basin_version_id: str
    forcing_filename: str
    active_flag: bool = False


class _FakeLookup:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def lookup(self, station_id: str) -> _Station:
        self.calls.append(station_id)
        basin_version_id, forcing_filename = _STATIONS[station_id]
        return _Station(basin_version_id, forcing_filename)


class _FakeResolver:
    """``model_id -> station_id | None``, as ``min(station_id)`` over ``met.interp_weight``."""

    def __init__(self, stations: dict[str, str] | None = None) -> None:
        self.stations = _STATION_BY_MODEL if stations is None else stations
        self.calls: list[str] = []

    def __call__(self, model_id: str) -> str | None:
        self.calls.append(model_id)
        return self.stations.get(model_id)


def _model_dir(root: Path, cycle: str, combo: tuple[str, str], model_id: str) -> Path:
    # Written out by hand, not through the resolver under test's own helper, so a
    # resolver drift (source case, segment order) reds here instead of agreeing
    # with itself.
    basin_version_id, source_id = combo
    return root / "forcing" / source_id.lower() / cycle / basin_version_id / model_id


def _csv_path(root: Path, cycle: str, combo: tuple[str, str], model_id: str | None = None) -> Path:
    model = _MODELS[combo] if model_id is None else model_id
    _basin_version_id, forcing_filename = _STATIONS[_STATION_BY_MODEL[_MODELS[combo]]]
    return _model_dir(root, cycle, combo, model) / "shud" / forcing_filename


def _write(root: Path, cycle: str, combos=COMBOS, *, mtime: float = _SETTLED) -> None:
    for combo in combos:
        _write_csv(_csv_path(root, cycle, combo), mtime=mtime)


def _write_csv(path: Path, *, mtime: float = _SETTLED) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1\t6\t20260101\t20260101\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


def _select(root: Path) -> Selection:
    return latest_complete_cycle(root, COMBOS, _FakeResolver(), _FakeLookup(), now=_NOW)


def _failure(root: Path, resolver: _FakeResolver | None = None) -> str:
    with pytest.raises(pytest.fail.Exception) as failure:
        latest_complete_cycle(root, COMBOS, resolver or _FakeResolver(), _FakeLookup(), now=_NOW)
    return str(failure.value)


def test_the_newest_cycle_with_every_combo_wins_and_names_what_it_resolved(tmp_path: Path) -> None:
    for cycle in ("2026092712", "2026092800", "2026092812"):
        _write(tmp_path, cycle)
    resolver = _FakeResolver()
    lookup = _FakeLookup()

    selection = latest_complete_cycle(tmp_path, COMBOS, resolver, lookup, now=_NOW)

    assert selection.cycle == "2026-09-28T12:00:00Z"
    assert selection.combos == (
        ResolvedCombo(
            basin_version_id="heihe_bv_1",
            source_id="IFS",
            model_id="dg_1111",
            station_id="dg-ifs-aaaa::cell:11",
            active_flag=False,
            path=tmp_path / "forcing/ifs/2026092812/heihe_bv_1/dg_1111/shud/station_00011.csv",
        ),
        ResolvedCombo(
            basin_version_id="heihe_bv_1",
            source_id="gfs",
            model_id="dg_2222",
            station_id="dg-gfs-bbbb::cell:22",
            active_flag=False,
            path=tmp_path / "forcing/gfs/2026092812/heihe_bv_1/dg_2222/shud/station_00022.csv",
        ),
        ResolvedCombo(
            basin_version_id="qhh_bv_1",
            source_id="IFS",
            model_id="dg_3333",
            station_id="dg-ifs-cccc::cell:33",
            active_flag=False,
            path=tmp_path / "forcing/ifs/2026092812/qhh_bv_1/dg_3333/shud/station_00033.csv",
        ),
        ResolvedCombo(
            basin_version_id="qhh_bv_1",
            source_id="gfs",
            model_id="dg_4444",
            station_id="dg-gfs-dddd::cell:44",
            active_flag=False,
            path=tmp_path / "forcing/gfs/2026092812/qhh_bv_1/dg_4444/shud/station_00044.csv",
        ),
    )
    # One round trip per model and per station, not per candidate cycle: both
    # are DB queries on node-27 and three cycles hold the same four models.
    assert sorted(resolver.calls) == ["dg_1111", "dg_2222", "dg_3333", "dg_4444"]
    assert sorted(lookup.calls) == sorted(_STATIONS)


def test_a_newer_cycle_missing_one_combo_is_skipped_over(tmp_path: Path) -> None:
    _write(tmp_path, "2026092800")
    _write(tmp_path, "2026092812", combos=COMBOS[:3])

    assert _select(tmp_path).cycle == "2026-09-28T00:00:00Z"


def test_the_model_is_whatever_single_dg_directory_the_cycle_holds(tmp_path: Path) -> None:
    # The newer cycle carries a newer generation of the heihe/IFS model; the
    # suite must follow the store, not remember a model from another cycle.
    _write(tmp_path, "2026092800")
    _write(tmp_path, "2026092812", combos=COMBOS[1:])
    newer = _csv_path(tmp_path, "2026092812", COMBOS[0], model_id="dg_9999")
    _write_csv(newer)
    resolver = _FakeResolver({**_STATION_BY_MODEL, "dg_9999": "dg-ifs-aaaa::cell:11"})

    selection = latest_complete_cycle(tmp_path, COMBOS, resolver, _FakeLookup(), now=_NOW)

    assert selection.cycle == "2026-09-28T12:00:00Z"
    assert selection.combos[0].model_id == "dg_9999"
    assert selection.combos[0].path == newer


def test_a_combo_with_two_dg_directories_is_absent_not_guessed(tmp_path: Path) -> None:
    _write(tmp_path, "2026092800")
    _write(tmp_path, "2026092812")
    # A second generation next to the first, complete and settled: picking
    # either would "work", which is exactly the guess the suite must not make.
    _write_csv(_csv_path(tmp_path, "2026092812", COMBOS[2], model_id="dg_3334"))

    assert _select(tmp_path).cycle == "2026-09-28T00:00:00Z"


def test_two_dg_directories_are_both_named_in_the_diagnostic(tmp_path: Path) -> None:
    _write(tmp_path, "2026092812")
    _write_csv(_csv_path(tmp_path, "2026092812", COMBOS[2], model_id="dg_3334"))

    message = _failure(tmp_path, _FakeResolver({**_STATION_BY_MODEL, "dg_3334": "dg-ifs-cccc::cell:33"}))

    assert (
        "reasons={'qhh_bv_1/IFS': '2 dg_* directories under forcing/ifs/2026092812/qhh_bv_1, "
        "not guessing: dg_3333, dg_3334'}"
    ) in message


def test_a_non_dg_model_directory_does_not_stand_in_for_a_dg_one(tmp_path: Path) -> None:
    _write(tmp_path, "2026092812", combos=COMBOS[1:])
    _write_csv(_csv_path(tmp_path, "2026092812", COMBOS[0], model_id="basins_heihe_shud"))

    message = _failure(tmp_path)

    assert "reasons={'heihe_bv_1/IFS': 'no dg_* directory under forcing/ifs/2026092812/heihe_bv_1'}" in message


def test_a_model_without_an_interp_weight_row_is_absent_with_that_reason(tmp_path: Path) -> None:
    _write(tmp_path, "2026092800")
    _write(tmp_path, "2026092812")
    stations = {model: station for model, station in _STATION_BY_MODEL.items() if model != "dg_4444"}

    message = _failure(tmp_path, _FakeResolver(stations))

    assert "'qhh_bv_1/gfs': 0}" in message
    assert "reasons={'qhh_bv_1/gfs': 'no interp_weight row for dg_4444'}" in message


def test_a_dg_directory_without_the_station_file_is_absent(tmp_path: Path) -> None:
    _write(tmp_path, "2026092800")
    _write(tmp_path, "2026092812", combos=COMBOS[:3])
    # The model directory is there, the station's own CSV is not.
    _write_csv(_model_dir(tmp_path, "2026092812", COMBOS[3], "dg_4444") / "shud" / "station_00001.csv")

    assert _select(tmp_path).cycle == "2026-09-28T00:00:00Z"

    (tmp_path / "forcing" / "gfs" / "2026092800").rename(tmp_path / "forcing" / "gfs" / "retired")
    assert (
        "reasons={'qhh_bv_1/gfs': 'station file missing: "
        "forcing/gfs/2026092812/qhh_bv_1/dg_4444/shud/station_00044.csv'}"
    ) in _failure(tmp_path)


def test_a_cycle_whose_file_is_still_too_fresh_is_excluded(tmp_path: Path) -> None:
    _write(tmp_path, "2026092800")
    _write(tmp_path, "2026092812")
    fresh = _csv_path(tmp_path, "2026092812", COMBOS[2])
    just_written = _NOW - MIN_SETTLED_AGE_SECONDS + 30
    os.utime(fresh, (just_written, just_written))

    assert _select(tmp_path).cycle == "2026-09-28T00:00:00Z"

    (tmp_path / "forcing" / "ifs" / "2026092800").rename(tmp_path / "forcing" / "ifs" / "retired")
    assert (
        "reasons={'qhh_bv_1/IFS': 'station file younger than 600 s: "
        "forcing/ifs/2026092812/qhh_bv_1/dg_3333/shud/station_00033.csv'}"
    ) in _failure(tmp_path)


def test_a_file_exactly_at_the_settle_age_counts(tmp_path: Path) -> None:
    _write(tmp_path, "2026092812", mtime=_NOW - MIN_SETTLED_AGE_SECONDS)

    assert _select(tmp_path).cycle == "2026-09-28T12:00:00Z"


def test_non_cycle_directories_are_not_candidates(tmp_path: Path) -> None:
    _write(tmp_path, "2026092800")
    # Same layout, but neither name is a YYYYMMDDHH cycle, so neither may win.
    _write(tmp_path, "latest")
    _write(tmp_path, "2026133100")

    assert _select(tmp_path).cycle == "2026-09-28T00:00:00Z"


def test_an_empty_intersection_fails_with_the_diagnostic(tmp_path: Path) -> None:
    # Every combo is present in SOME cycle, none in all of them.
    _write(tmp_path, "2026092800", combos=COMBOS[:2])
    _write(tmp_path, "2026092812", combos=COMBOS[2:])
    _write(tmp_path, "2026092900", combos=COMBOS[1:3])

    message = _failure(tmp_path)

    assert message == (
        "real store has no cycle where all 4 combos are present "
        f"(root={tmp_path}, "
        "per-combo counts={'heihe_bv_1/IFS': 1, 'heihe_bv_1/gfs': 2, 'qhh_bv_1/IFS': 2, 'qhh_bv_1/gfs': 1}, "
        "newest cycle examined=2026092900, "
        "reasons={'heihe_bv_1/IFS': 'no dg_* directory under forcing/ifs/2026092900/heihe_bv_1', "
        "'qhh_bv_1/gfs': 'no dg_* directory under forcing/gfs/2026092900/qhh_bv_1'})"
    )


def test_a_store_without_forcing_fails_rather_than_skips(tmp_path: Path) -> None:
    message = _failure(tmp_path)

    assert message == (
        "real store has no cycle where all 4 combos are present "
        f"(root={tmp_path}, "
        "per-combo counts={'heihe_bv_1/IFS': 0, 'heihe_bv_1/gfs': 0, 'qhh_bv_1/IFS': 0, 'qhh_bv_1/gfs': 0}, "
        "newest cycle examined=none, "
        "reasons={'heihe_bv_1/IFS': 'no cycle directory under forcing/ifs', "
        "'heihe_bv_1/gfs': 'no cycle directory under forcing/gfs', "
        "'qhh_bv_1/IFS': 'no cycle directory under forcing/ifs', "
        "'qhh_bv_1/gfs': 'no cycle directory under forcing/gfs'})"
    )
