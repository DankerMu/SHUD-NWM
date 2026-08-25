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
    main,
    resolve_renames,
    run_item,
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


# --------------------------------------------------------------------------
# Round-2 pins: the three remaining shapes of silent under-coverage (design
# D5) plus the receipt's own survival.  D5 named the failure mode; the first
# implementation only guarded the one instance D5 happened to describe.


def _partial_target(root: Path, cycle: str, model_id: str) -> Path:
    """What a producer killed mid-write leaves behind.

    Producer writes are atomic per FILE, never per directory, so an interrupted
    run leaves the model directory present holding only some members.
    """

    directory = root / "gfs" / cycle / "basins_heihe_vbasins" / model_id
    directory.mkdir(parents=True)
    (directory / "forcing.tsd.forc").write_text(
        "valid_time,variable,dg-gfs-bbbbbbbbbbbb::cell:1001\n", encoding="utf-8"
    )
    return directory


def _renames(tmp_path: Path):
    previous = _write(tmp_path / "prev.json", _manifest([_model("dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")]))
    current = _write(tmp_path / "cur.json", _manifest([_model("dg_" + "b" * 32, package_id="dg-gfs-bbbbbbbbbbbb")]))
    renames, _ = resolve_renames(previous, current)
    return previous, current, renames


def test_a_partial_target_directory_is_discovered_not_silently_skipped(tmp_path: Path) -> None:
    """An existing target that does not verify is work, not a completed item.

    Gating discovery on ``target_dir.is_dir()`` alone makes an interrupted
    producer indistinguishable, in the receipt, from a correct backfill -- and
    makes it unreachable forever, because every later run skips it too.
    """

    _, _, renames = _renames(tmp_path)
    root = tmp_path / "forcing"
    _seed_forcing(root, "2026082300", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")
    _partial_target(root, "2026082300", "dg_" + "b" * 32)

    items = discover_work(renames, root, ["2026082300"])

    assert [(item.cycle, item.model_id) for item in items] == [("2026082300", "dg_" + "b" * 32)]
    assert items[0].existing_target is True
    assert items[0].status == "existing_target_unverified"
    assert items[0].detail["verification"]["verified"] is False


def test_a_partial_target_is_not_replaced_without_the_opt_in_flag(tmp_path: Path) -> None:
    _, _, renames = _renames(tmp_path)
    root = tmp_path / "forcing"
    _seed_forcing(root, "2026082300", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")
    target = _partial_target(root, "2026082300", "dg_" + "b" * 32)
    item = discover_work(renames, root, ["2026082300"])[0]

    run_item(item, ["/usr/bin/env", "true"], dry_run=False, replace_unverified_target=False)

    assert item.status == "existing_target_unverified"
    assert (target / "forcing.tsd.forc").is_file()
    assert "command" not in item.detail


def test_the_opt_in_flag_replaces_the_partial_target(tmp_path: Path) -> None:
    _, _, renames = _renames(tmp_path)
    root = tmp_path / "forcing"
    _seed_forcing(root, "2026082300", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")
    target = _partial_target(root, "2026082300", "dg_" + "b" * 32)
    item = discover_work(renames, root, ["2026082300"])[0]

    run_item(item, ["/usr/bin/env", "true"], dry_run=False, replace_unverified_target=True)

    assert not (target / "forcing.tsd.forc").exists()
    assert item.detail["replaced_target"] is True
    assert Path(item.detail["replaced_target_quarantine_path"]).is_dir()


def test_a_forcing_root_that_holds_nothing_refuses_naming_the_probed_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Total under-coverage must fail closed.

    ``renamed_model_count: N, work_item_count: 0`` is ALSO the steady state of a
    fully-covered rerun, so it cannot discriminate a wrong ``--forcing-root``
    (or an unmounted NFS share) from "nothing to do".
    """

    previous, current, _ = _renames(tmp_path)
    root = tmp_path / "not-the-forcing-root"
    root.mkdir()

    code = main(
        [
            "--previous-manifest",
            str(previous),
            "--current-manifest",
            str(current),
            "--forcing-root",
            str(root),
        ]
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_code"] == "BACKFILL_FORCING_ROOT_UNCOVERED"
    assert payload["context"]["source_dirs_probed"] == [str(root / "gfs")]
    assert payload["context"]["source_dirs_found"] == []


def test_a_verification_failure_leaves_nothing_live_under_the_model_path(tmp_path: Path) -> None:
    """A package that failed its own acceptance oracle must not stay readable.

    The forecast stage reads the model path directly; leaving an unverified
    package there would feed SHUD silently.
    """

    root = tmp_path / "forcing"
    _seed_forcing(root, "2026082300", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")
    target = _seed_forcing(root, "2026082300", "dg_" + "b" * 32, package_id="dg-gfs-bbbbbbbbbbbb")
    (target / "shud" / "station_00001.csv").write_text("valid_time,PRCP\n2026-08-23T00:00:00Z,999\n", encoding="utf-8")
    item = _item(root)

    run_item(item, ["/usr/bin/env", "true"], dry_run=False)

    assert item.status == "verification_failed"
    assert not item.target_dir.exists()
    quarantine = Path(item.as_dict()["detail"]["quarantine_path"])
    assert quarantine.is_dir()
    assert (quarantine / "shud" / "station_00001.csv").is_file()
    assert quarantine.parent.name == "_backfill_quarantine"
    assert not quarantine.name.startswith("dg_")


def test_one_items_exception_does_not_discard_the_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A per-item fault must cost that item's status, not every item's."""

    import scripts.node22_backfill_forcing_for_model_ids as backfill

    previous, current, _ = _renames(tmp_path)
    root = tmp_path / "forcing"
    for cycle in ("2026082212", "2026082300"):
        _seed_forcing(root, cycle, "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")

    def _explode(item: WorkItem) -> dict[str, object]:
        if item.cycle == "2026082212":
            raise PermissionError("member unreadable")
        return {"verified": True, "shud_files_compared": 1}

    monkeypatch.setattr(backfill, "verify_item", _explode)
    receipt_path = tmp_path / "receipt.json"

    code = backfill.main(
        [
            "--previous-manifest",
            str(previous),
            "--current-manifest",
            str(current),
            "--forcing-root",
            str(root),
            "--producer",
            "/usr/bin/env true",
            "--execute",
            "--output",
            str(receipt_path),
        ]
    )
    capsys.readouterr()

    assert code == 1
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status_counts"] == {"errored": 1, "verified": 1}
    statuses = {item["cycle"]: item["status"] for item in receipt["work_items"]}
    assert statuses == {"2026082212": "errored", "2026082300": "verified"}
    errored = next(item for item in receipt["work_items"] if item["status"] == "errored")
    assert "member unreadable" in errored["detail"]["error"]


# --------------------------------------------------------------------------
# Round-3 pins: the fix round's own two holes.  A quarantine that cannot
# complete left the unverified artifact live while the status claimed it had
# been quarantined; and the replacement PREVIEW flipped the exit code to 0 for
# a state that exits 1 without the flag.


def _block_quarantine(target: Path) -> Path:
    """Make the quarantine rename impossible, the way storage really does it.

    A plain FILE occupying the quarantine directory's name is the cheapest
    faithful stand-in for the real faults on this tool's NFS-backed ``/scratch``
    (ESTALE, permission drift, ENOSPC on the ``mkdir``): ``mkdir(exist_ok=True)``
    raises ``FileExistsError`` because the path is not a directory.
    """

    blocker = target.parent / "_backfill_quarantine"
    blocker.write_text("not a directory\n", encoding="utf-8")
    return blocker


def test_a_failed_quarantine_is_not_reported_as_a_plain_verification_failure(tmp_path: Path) -> None:
    """A quarantine that could not complete must be its own, louder outcome.

    ``verification_failed`` with a successful quarantine and
    ``verification_failed`` with a failed one were byte-identical statuses, so
    nothing told the operator that an unverified package was still standing on
    the path the forecast stage reads directly.
    """

    root = tmp_path / "forcing"
    _seed_forcing(root, "2026082300", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")
    target = _seed_forcing(root, "2026082300", "dg_" + "b" * 32, package_id="dg-gfs-bbbbbbbbbbbb")
    (target / "shud" / "station_00001.csv").write_text("valid_time,PRCP\n2026-08-23T00:00:00Z,999\n", encoding="utf-8")
    _block_quarantine(target)
    item = _item(root)

    run_item(item, ["/usr/bin/env", "true"], dry_run=False)

    assert item.status == "quarantine_failed"
    assert item.detail["quarantine_failed_after"] == "verification_failed"
    assert item.detail["unverified_artifact_live"] is True
    assert item.detail["live_target_dir"] == str(target)
    assert (target / "shud" / "station_00001.csv").is_file()
    assert [entry["label"] for entry in item.detail["quarantine_errors"]] == ["verification_failed"]


def test_a_failed_quarantine_on_the_replace_path_exits_non_zero_and_does_not_produce(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """If the unverified directory could not be moved aside, do not produce into it.

    Writes are atomic per file and never clear the target first, so producing
    into a still-present partial package overwrites same-named members and
    leaves the strays.
    """

    previous, current, _ = _renames(tmp_path)
    root = tmp_path / "forcing"
    _seed_forcing(root, "2026082300", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")
    target = _partial_target(root, "2026082300", "dg_" + "b" * 32)
    _block_quarantine(target)

    code = main(
        [
            "--previous-manifest",
            str(previous),
            "--current-manifest",
            str(current),
            "--forcing-root",
            str(root),
            "--producer",
            "/usr/bin/env true",
            "--execute",
            "--replace-unverified-target",
        ]
    )

    assert code == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status_counts"] == {"quarantine_failed": 1}
    detail = receipt["work_items"][0]["detail"]
    assert detail["unverified_artifact_live"] is True
    assert detail["quarantine_failed_after"] == "replaced_unverified"
    assert "command" not in detail
    assert "replaced_target" not in detail
    assert (target / "forcing.tsd.forc").is_file()


def test_the_replace_preview_still_reports_the_unverified_target_and_exits_non_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A preview must not be a greener light than the state it previews.

    Without the flag this state exits 1.  Adding ``--replace-unverified-target``
    to a dry run must not turn it into an exit 0 that a gating script reads as
    "clean".
    """

    previous, current, _ = _renames(tmp_path)
    root = tmp_path / "forcing"
    _seed_forcing(root, "2026082300", "dg_" + "a" * 32, package_id="dg-gfs-aaaaaaaaaaaa")
    target = _partial_target(root, "2026082300", "dg_" + "b" * 32)

    code = main(
        [
            "--previous-manifest",
            str(previous),
            "--current-manifest",
            str(current),
            "--forcing-root",
            str(root),
            "--replace-unverified-target",
        ]
    )

    assert code == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["executed"] is False
    assert receipt["status_counts"] == {"existing_target_unverified": 1}
    assert receipt["work_items"][0]["detail"]["would_replace_target"] is True
    assert (target / "forcing.tsd.forc").is_file()
