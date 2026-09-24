"""#1480 seed-station provenance backfill and rollback on a disposable database.

Runs ``scripts/ops/node27_1480_backfill_seed_station_provenance*.sql`` unchanged
through the node-27 runner (``scripts/ops/node27_oneshot_sql.py``). The seed
covers every predicate branch: heihe rows written before #1415 (both source
keys say ``qhh.tsd.forc``), a heihe row whose only wrong key is
``elevation_metadata.source``, a heihe row without ``elevation_metadata``, a
correct qhh row, a non-seed row with the same wrong literal, a seed row
without ``project_name``, a seed row whose ``elevation_metadata`` is not an
object and a wrong-``source`` row whose ``elevation_metadata`` is an array. Only
the heihe rows with a wrong key may change, and only in the two keys (just
``source`` where ``elevation_metadata`` is absent or not an object).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from psycopg2.extras import Json

from scripts.ops.node27_oneshot_sql import ScriptFailedError, run_sql_file
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection

pytestmark = pytest.mark.integration

_OPS = Path(__file__).resolve().parents[1] / "scripts" / "ops"
BACKFILL_SQL = _OPS / "node27_1480_backfill_seed_station_provenance.sql"
ROLLBACK_SQL = _OPS / "node27_1480_backfill_seed_station_provenance_rollback.sql"
BV = "basins_it1480_vbasins"
SEED = "qhh_production_bootstrap"


def _seed_properties(project: str, index: int, source: str, elevation: Any = "object") -> dict[str, Any]:
    """The `_seed_station_rows` payload shape (tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json)."""
    properties: dict[str, Any] = {
        "x": 110386.42323847 + index,
        "y": 3911708.47393155,
        "z": -9999.0,
        "seed": SEED,
        "source": source,
        "basin_id": f"basins_{project}",
        "model_id": f"basins_{project}_shud",
        "original_id": str(index),
        "source_file": f"/volume/data/nwm/Basins/{project}/input/{project}/{project}.tsd.forc",
        "project_name": project,
        "source_sha256": "1313f38bc1b52120c1f1c5f638611e4f3b177fd1689a1b5e6acd538545b294c2",
        "basin_version_id": BV,
        "forcing_filename": f"X100.{index}Y37.65.csv",
        "shud_forcing_index": index,
        "forcing_source_identity": f"{project}.tsd.forc:{index}:X100.{index}Y37.65.csv",
    }
    if elevation == "object":
        properties["elevation_metadata"] = {"raw_z": -9999.0, "source": source, "normalized_missing_to_zero": True}
    elif elevation is not None:
        properties["elevation_metadata"] = elevation
    return properties


def _rows() -> dict[str, dict[str, Any]]:
    heihe_elevation_only = _seed_properties("heihe", 4, "heihe.tsd.forc")
    heihe_elevation_only["elevation_metadata"]["source"] = "qhh.tsd.forc"
    no_project = _seed_properties("heihe", 8, "qhh.tsd.forc")
    del no_project["project_name"]
    non_seed = _seed_properties("heihe", 6, "qhh.tsd.forc")
    non_seed["seed"] = "forcing_domain_handoff"
    return {
        "heihe-001": _seed_properties("heihe", 1, "qhh.tsd.forc"),
        "heihe-002": _seed_properties("heihe", 2, "qhh.tsd.forc"),
        "heihe-003-no-elevation": _seed_properties("heihe", 3, "qhh.tsd.forc", elevation=None),
        "heihe-004-elevation-only": heihe_elevation_only,
        "qhh-005-correct": _seed_properties("qhh", 5, "qhh.tsd.forc"),
        "heihe-006-not-seed": non_seed,
        "heihe-007-elevation-not-object": _seed_properties("heihe", 7, "heihe.tsd.forc", elevation="qhh.tsd.forc"),
        "heihe-008-no-project": no_project,
        # A non-object elevation_metadata on a row matched through `source`:
        # jsonb_set would fail on the array path, so only `source` may change.
        "heihe-009-elevation-array": _seed_properties("heihe", 9, "qhh.tsd.forc", elevation=["qhh.tsd.forc"]),
    }


CHANGED = (
    "heihe-001",
    "heihe-002",
    "heihe-003-no-elevation",
    "heihe-004-elevation-only",
    "heihe-009-elevation-array",
)


def _seed(database_url: str) -> dict[str, dict[str, Any]]:
    apply_migrations_from_zero(database_url)
    rows = _rows()
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO core.basin (basin_id, basin_name, basin_group) VALUES ('basins_it1480', 'It1480', 'Basins')"
        )
        cursor.execute(
            "INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom, active_flag) "
            "VALUES (%s, 'basins_it1480', 'vbasins', ST_Multi(ST_MakeEnvelope(99, 37, 101, 39, 4490)), true)",
            (BV,),
        )
        for station_id, properties in rows.items():
            cursor.execute(
                "INSERT INTO met.met_station (station_id, basin_version_id, geom, station_role, properties_json) "
                "VALUES (%s, %s, ST_SetSRID(ST_MakePoint(100, 38), 4490), 'forcing_grid', %s)",
                (station_id, BV, Json(properties)),
            )
    return rows


def _state(database_url: str) -> dict[str, tuple[dict[str, Any], str]]:
    """station_id -> (properties_json, its canonical jsonb text)."""
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT station_id, properties_json, properties_json::text AS rendered FROM met.met_station "
            "WHERE basin_version_id = %s ORDER BY station_id",
            (BV,),
        )
        return {row["station_id"]: (row["properties_json"], row["rendered"]) for row in cursor.fetchall()}


def _without_the_two_keys(properties: dict[str, Any]) -> dict[str, Any]:
    stripped = copy.deepcopy(properties)
    stripped.pop("source", None)
    if isinstance(stripped.get("elevation_metadata"), dict):
        stripped["elevation_metadata"].pop("source", None)
    return stripped


def test_backfill_rewrites_exactly_two_keys_is_idempotent_and_rolls_back(
    throwaway_database_url: str, tmp_path: Path
) -> None:
    url = throwaway_database_url
    seeded = _seed(url)
    before = _state(url)
    first_dir, second_dir = tmp_path / "apply", tmp_path / "rerun"
    first_dir.mkdir()
    second_dir.mkdir()

    report = run_sql_file(
        BACKFILL_SQL,
        database_url=url,
        copy_dir=first_dir,
        settings={"nhms_1480.expected_rows": str(len(CHANGED))},
        apply=True,
    )
    assert report.committed
    assert any(notice.endswith(f"#1480 backfilled rows: {len(CHANGED)}") for notice in report.notices)
    after = _state(url)

    for station_id, (properties, rendered) in after.items():
        if station_id not in CHANGED:
            assert rendered == before[station_id][1], f"{station_id} must be byte-identical"
            continue
        project = seeded[station_id]["project_name"]
        assert properties["source"] == f"{project}.tsd.forc", station_id
        if isinstance(seeded[station_id].get("elevation_metadata"), dict):
            assert properties["elevation_metadata"]["source"] == f"{project}.tsd.forc", station_id
        else:
            assert properties.get("elevation_metadata") == seeded[station_id].get("elevation_metadata"), station_id
        # Every other key -- forcing_source_identity, source_file, project_name,
        # source_sha256, x/y/z, the rest of elevation_metadata -- is unchanged.
        assert _without_the_two_keys(properties) == _without_the_two_keys(before[station_id][0]), station_id
    assert after["qhh-005-correct"][0]["source"] == "qhh.tsd.forc"
    assert after["heihe-006-not-seed"][0]["source"] == "qhh.tsd.forc"

    backup_lines = (first_dir / "met_station_properties_json.copy").read_text(encoding="utf-8").splitlines()
    backed_up = {line.split("\t", 1)[0]: json.loads(line.split("\t", 1)[1]) for line in backup_lines}
    assert backed_up == {station_id: before[station_id][0] for station_id in CHANGED}

    rerun = run_sql_file(
        BACKFILL_SQL,
        database_url=url,
        copy_dir=second_dir,
        settings={"nhms_1480.expected_rows": "0"},
        apply=True,
    )
    assert any(notice.endswith("#1480 backfilled rows: 0") for notice in rerun.notices)
    assert (second_dir / "met_station_properties_json.copy").read_text(encoding="utf-8") == ""
    assert _state(url) == after

    # A legitimate write after the backfill must survive the rollback.
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE met.met_station SET properties_json = properties_json || '{\"later\": true}' "
            "WHERE station_id = 'heihe-002'"
        )
    later = _state(url)["heihe-002"]

    rolled_back = run_sql_file(ROLLBACK_SQL, database_url=url, copy_dir=first_dir, apply=True)
    assert any("restored 4 of 5 backed-up row(s); 1 skipped" in notice for notice in rolled_back.notices)
    restored = _state(url)
    for station_id in before:
        expected = later if station_id == "heihe-002" else before[station_id]
        assert restored[station_id] == expected, station_id


def test_a_row_count_other_than_expected_raises_and_changes_nothing(
    throwaway_database_url: str, tmp_path: Path
) -> None:
    url = throwaway_database_url
    _seed(url)
    before = _state(url)

    with pytest.raises(ScriptFailedError, match=r"backfill touched 5 row\(s\), expected 1709") as raised:
        run_sql_file(BACKFILL_SQL, database_url=url, copy_dir=tmp_path, apply=True)

    assert raised.value.pgcode == "P0001"
    assert _state(url) == before


def test_the_default_dry_run_rolls_back(throwaway_database_url: str, tmp_path: Path) -> None:
    url = throwaway_database_url
    _seed(url)
    before = _state(url)

    report = run_sql_file(
        BACKFILL_SQL, database_url=url, copy_dir=tmp_path, settings={"nhms_1480.expected_rows": str(len(CHANGED))}
    )

    assert not report.committed
    assert any("after: project_name=heihe source=heihe.tsd.forc" in notice for notice in report.notices)
    assert _state(url) == before
