"""Requirement pins for the per-model forcing backfill after an id change.

The requirement: when a republish changes a direct-grid model's ``dg_*``
identity without moving any station, every cycle whose forcing exists only
under the OLD id must be replayed under the NEW id -- and the replay must be
provably equivalent to the old package.  The tool must refuse the two shapes
that are not identity churn: a changed topology, and moved stations.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.node22_backfill_forcing_for_model_ids import (
    BackfillError,
    WorkItem,
    discover_work,
    resolve_renames,
    verify_item,
)


def _station(index: int, package_id: str) -> dict[str, object]:
    return {
        "station_id": f"{package_id}::cell:{1000 + index}",
        "grid_cell_id": str(1000 + index),
        "shud_forcing_index": index,
        "forcing_filename": f"station_{index:05d}.csv",
        "latitude": 40.0 + index,
        "longitude": 100.0 + index,
    }


def _manifest(rows: list[dict[str, object]]) -> dict[str, object]:
    return {"models": rows}


def _model(
    model_id: str,
    *,
    package_id: str,
    sp_att: str = "heihe.sp.att",
    source_id: str = "gfs",
    basin_version_id: str = "basins_heihe_vbasins",
    station_count: int = 3,
) -> dict[str, object]:
    return {
        "model_id": model_id,
        "basin_version_id": basin_version_id,
        "resource_profile": {
            "direct_grid_forcing": {
                "applicable_source_ids": [source_id],
                "sp_att_path": sp_att,
                "model_input_package_id": package_id,
                "station_bindings": [_station(index, package_id) for index in range(1, station_count + 1)],
            }
        },
    }


def _write(path: Path, payload: dict[str, object]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_identity_only_change_is_a_rename(tmp_path: Path) -> None:
    previous = _write(tmp_path / "prev.json", _manifest([_model("dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")]))
    current = _write(tmp_path / "cur.json", _manifest([_model("dg_" + "b" * 32, package_id="dg-gfs-bbbbbbbbbbbb")]))

    renames, rebindings = resolve_renames(previous, current)

    assert rebindings == []
    assert [(rename.previous.model_id, rename.current.model_id) for rename in renames] == [
        ("dg_" + "a" * 32, "dg_" + "b" * 32)
    ]


def test_unchanged_model_is_not_a_rename(tmp_path: Path) -> None:
    payload = _manifest([_model("dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")])
    previous = _write(tmp_path / "prev.json", payload)
    current = _write(tmp_path / "cur.json", payload)

    renames, rebindings = resolve_renames(previous, current)

    assert renames == []
    assert rebindings == []


def test_moved_stations_are_skipped_not_backfilled(tmp_path: Path) -> None:
    previous = _write(tmp_path / "prev.json", _manifest([_model("dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")]))
    current = _write(
        tmp_path / "cur.json",
        _manifest([_model("dg_" + "b" * 32, package_id="dg-gfs-bbbbbbbbbbbb", station_count=4)]),
    )

    renames, rebindings = resolve_renames(previous, current)

    assert renames == []
    assert [entry["model_id"] for entry in rebindings] == ["dg_" + "b" * 32]


def test_topology_change_refuses(tmp_path: Path) -> None:
    previous = _write(tmp_path / "prev.json", _manifest([_model("dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")]))
    current = _write(
        tmp_path / "cur.json",
        _manifest([_model("dg_" + "b" * 32, package_id="dg-gfs-bbbbbbbbbbbb", sp_att="other.sp.att")]),
    )

    with pytest.raises(BackfillError) as excinfo:
        resolve_renames(previous, current)

    assert excinfo.value.code == "BACKFILL_MANIFEST_KEYSET_DIVERGED"


def test_basin_version_change_refuses(tmp_path: Path) -> None:
    previous = _write(tmp_path / "prev.json", _manifest([_model("dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")]))
    current = _write(
        tmp_path / "cur.json",
        _manifest(
            [_model("dg_" + "b" * 32, package_id="dg-gfs-bbbbbbbbbbbb", basin_version_id="basins_heihe_vbasins2")]
        ),
    )

    with pytest.raises(BackfillError) as excinfo:
        resolve_renames(previous, current)

    assert excinfo.value.code == "BACKFILL_BASIN_VERSION_CHANGED"


def _seed_forcing(root: Path, cycle: str, model_id: str, *, package_id: str) -> Path:
    directory = root / "gfs" / cycle / "basins_heihe_vbasins" / model_id
    (directory / "shud").mkdir(parents=True)
    (directory / "payloads").mkdir(parents=True)
    (directory / "shud" / "station_00001.csv").write_text("valid_time,PRCP\n2026-08-23T00:00:00Z,0\n", encoding="utf-8")
    (directory / "forcing.tsd.forc").write_text(f"valid_time,variable,{package_id}::cell:1001\n", encoding="utf-8")
    (directory / "forcing_debug.csv").write_text(f"2026,{package_id}::cell:1001,PRCP,0,mm/day\n", encoding="utf-8")
    for name in ("interp_weights.json", "station_inventory.json", "station_timeseries.json"):
        (directory / "payloads" / name).write_text(json.dumps({"model_id": model_id}), encoding="utf-8")
    return directory


def test_discover_work_targets_only_cycles_missing_the_new_id(tmp_path: Path) -> None:
    previous = _write(tmp_path / "prev.json", _manifest([_model("dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")]))
    current = _write(tmp_path / "cur.json", _manifest([_model("dg_" + "b" * 32, package_id="dg-gfs-bbbbbbbbbbbb")]))
    renames, _ = resolve_renames(previous, current)
    root = tmp_path / "forcing"
    _seed_forcing(root, "2026082212", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")
    _seed_forcing(root, "2026082212", "dg_" + "b" * 32, package_id="dg-gfs-bbbbbbbbbbbb")
    _seed_forcing(root, "2026082300", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")

    items = discover_work(renames, root, [])

    assert [(item.cycle, item.model_id) for item in items] == [("2026082300", "dg_" + "b" * 32)]


def test_discover_work_honours_the_cycle_filter(tmp_path: Path) -> None:
    previous = _write(tmp_path / "prev.json", _manifest([_model("dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")]))
    current = _write(tmp_path / "cur.json", _manifest([_model("dg_" + "b" * 32, package_id="dg-gfs-bbbbbbbbbbbb")]))
    renames, _ = resolve_renames(previous, current)
    root = tmp_path / "forcing"
    _seed_forcing(root, "2026082212", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")
    _seed_forcing(root, "2026082300", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")

    items = discover_work(renames, root, ["2026082300"])

    assert [item.cycle for item in items] == ["2026082300"]


def _item(root: Path, cycle: str = "2026082300") -> WorkItem:
    base = root / "gfs" / cycle / "basins_heihe_vbasins"
    return WorkItem(
        source_id="gfs",
        cycle=cycle,
        basin_version_id="basins_heihe_vbasins",
        previous_model_id="dg_" + "a" * 32,
        model_id="dg_" + "b" * 32,
        previous_dir=base / ("dg_" + "a" * 32),
        target_dir=base / ("dg_" + "b" * 32),
    )


def test_verify_accepts_a_replay_that_differs_only_in_identity(tmp_path: Path) -> None:
    root = tmp_path / "forcing"
    _seed_forcing(root, "2026082300", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")
    _seed_forcing(root, "2026082300", "dg_" + "b" * 32, package_id="dg-gfs-bbbbbbbbbbbb")

    result = verify_item(_item(root))

    assert result["verified"] is True
    assert result["shud_files_compared"] == 1


def test_verify_rejects_a_replay_whose_shud_bytes_moved(tmp_path: Path) -> None:
    root = tmp_path / "forcing"
    _seed_forcing(root, "2026082300", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")
    target = _seed_forcing(root, "2026082300", "dg_" + "b" * 32, package_id="dg-gfs-bbbbbbbbbbbb")
    (target / "shud" / "station_00001.csv").write_text(
        "valid_time,PRCP\n2026-08-23T00:00:00Z,999\n", encoding="utf-8"
    )

    result = verify_item(_item(root))

    assert result["verified"] is False
    assert result["shud_files_mismatched"] == ["station_00001.csv"]


def test_verify_rejects_a_replay_whose_values_moved_under_identical_identity(tmp_path: Path) -> None:
    root = tmp_path / "forcing"
    _seed_forcing(root, "2026082300", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")
    target = _seed_forcing(root, "2026082300", "dg_" + "b" * 32, package_id="dg-gfs-bbbbbbbbbbbb")
    (target / "forcing_debug.csv").write_text("2026,dg-gfs-bbbbbbbbbbbb::cell:1001,PRCP,7,mm/day\n", encoding="utf-8")

    result = verify_item(_item(root))

    assert result["verified"] is False
    assert result["normalised_members_mismatched"] == ["forcing_debug.csv"]


def test_verify_rejects_a_missing_target(tmp_path: Path) -> None:
    root = tmp_path / "forcing"
    _seed_forcing(root, "2026082300", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")

    result = verify_item(_item(root))

    assert result["verified"] is False


def test_discover_work_finds_an_upper_cased_source_under_its_lower_cased_path(tmp_path: Path) -> None:
    """Canonical ``IFS`` stores its forcing under ``forcing/ifs/``.

    Scanning by the canonical id would find nothing and silently under-cover
    half the work -- exactly the shape this backfill exists to catch.
    """

    previous = _write(
        tmp_path / "prev.json",
        _manifest([_model("dg_" + "a" * 32, package_id="dg-ifs-aaaaaaaaaaaa", source_id="IFS")]),
    )
    current = _write(
        tmp_path / "cur.json",
        _manifest([_model("dg_" + "b" * 32, package_id="dg-ifs-bbbbbbbbbbbb", source_id="IFS")]),
    )
    renames, _ = resolve_renames(previous, current)
    root = tmp_path / "forcing"
    directory = root / "ifs" / "2026082300" / "basins_heihe_vbasins" / ("dg_" + "a" * 32)
    directory.mkdir(parents=True)

    items = discover_work(renames, root, ["2026082300"])

    assert [(item.source_id, item.cycle, item.model_id) for item in items] == [
        ("IFS", "2026082300", "dg_" + "b" * 32)
    ]
