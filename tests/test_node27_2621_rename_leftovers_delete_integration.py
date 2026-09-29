"""#2621 rename-leftover delete / rollback scripts on a disposable database.

Runs ``scripts/ops/node27_2621_delete_rename_leftovers{,_rollback}.sql``
through the runner the operator uses on node-27
(``scripts/ops/node27_oneshot_sql.py``), unchanged. The seed reproduces the
node-27 read-only inventory of 2026-09-29 with the REAL ids and the REAL row
counts -- there is no count-override path to test against a smaller seed:

* seven target basins (``basins_dnzh_{mdzh,mj,mnzh,qtj}``,
  ``basins_xinan_{dulongjiang,lancangjiang,nujiang}``), one basin_version /
  river_network_version / mesh_version and three inactive model_instance rows
  each (21 real model ids), and per basin the inventoried river_segment /
  river_segment_crosswalk / met_station counts (85196 / 85471 / 2290 in total),
  generated with ``generate_series``;
* the seven successors (``basins_se_*`` / ``basins_sw_*``), each with one active
  model, and a control basin that owns a hydro run, a forcing version and rows
  in all three hypertables -- inside COMPRESSED chunks, so the zero-reference
  probes scan real compressed data and must still find nothing for the targets.

Every table the delete touches is fingerprinted (row count + md5 over each
row's full text, identity keys and the generated ``stream_type`` included), so
"nothing deleted" and "restored byte for byte" are exact comparisons.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extensions import make_dsn

from scripts.ops.node27_oneshot_sql import ScriptFailedError, run_sql_file
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection

pytestmark = pytest.mark.integration

_OPS = Path(__file__).resolve().parents[1] / "scripts" / "ops"
DELETE_SQL = _OPS / "node27_2621_delete_rename_leftovers.sql"
ROLLBACK_SQL = _OPS / "node27_2621_delete_rename_leftovers_rollback.sql"

# basin_id, successor, model ids, river_segment, river_segment_crosswalk, met_station
# (node-27 read-only inventory, 2026-09-29).
TARGETS: tuple[tuple[str, str, tuple[str, str, str], int, int, int], ...] = (
    ("basins_dnzh_mdzh", "basins_se_mdzh",
     ("basins_dnzh_mdzh_shud", "dg_09fce53ac1464cc52f57e1900d09d89b", "dg_f4cbe11623cbb6e134d473be9ae6e7aa"),
     1332, 2429, 68),
    ("basins_dnzh_mj", "basins_se_mj",
     ("basins_dnzh_mj_shud", "dg_3790e742fa58b26fda46daa0d5d7161a", "dg_a2b9466955265a94a267973e6b6784ab"),
     5676, 7552, 230),
    ("basins_dnzh_mnzh", "basins_se_mnzh",
     ("basins_dnzh_mnzh_shud", "dg_090c71585b08bee7c92f9fe98fba1c54", "dg_761c02aad9cb7bbec0ef4eba742dfb73"),
     2810, 3933, 140),
    ("basins_dnzh_qtj", "basins_se_qtj",
     ("basins_dnzh_qtj_shud", "dg_58ad77fa681132a4bbab8315ae54b238", "dg_946f928581f452bc30d10ce8e1eafc29"),
     4860, 8791, 202),
    ("basins_xinan_dulongjiang", "basins_sw_dulongjiang",
     ("basins_xinan_dulongjiang_shud", "dg_b5047053411ae5aafd0dd5dad58d5535", "dg_b5dc14495aa8878fa1095107b34ca114"),
     16772, 14371, 318),
    ("basins_xinan_lancangjiang", "basins_sw_lancangjiang",
     ("basins_xinan_lancangjiang_shud", "dg_cc6b9d6ba0de0cfafb2b29de0bcf398a", "dg_f2f293b4936f68cef46482cbc8c7ed2f"),
     26376, 24725, 700),
    ("basins_xinan_nujiang", "basins_sw_nujiang",
     ("basins_xinan_nujiang_shud", "dg_ba0a40ec23bd1171345eb8f3e597a9e3", "dg_f36cbd64aee01fe535920fd6e2facdcb"),
     27370, 23670, 632),
)  # fmt: skip
BASINS = tuple(row[0] for row in TARGETS)
SUCCESSORS = tuple(row[1] for row in TARGETS)
CONTROL = "basins_it2621_control"
# What the operator passes from manifest-last.json: the successors are scheduled.
MANIFEST = ",".join((*SUCCESSORS, CONTROL))
SETTINGS = {"nhms.manifest_basins": MANIFEST}


def _ids(basin_id: str) -> tuple[str, str, str]:
    return f"{basin_id}_vbasins", f"{basin_id}_rivnet_vbasins", f"{basin_id}_mesh_vbasins"


BVS = [_ids(basin)[0] for basin in BASINS]
RNVS = [_ids(basin)[1] for basin in BASINS]

# relation, order key, the target predicate the delete uses.
_TABLES: tuple[tuple[str, str, str], ...] = (
    ("core.basin", "basin_id", "basin_id = ANY (%(basins)s)"),
    ("core.basin_version", "basin_version_id", "basin_id = ANY (%(basins)s)"),
    ("core.river_network_version", "river_network_version_id", "basin_version_id = ANY (%(bvs)s)"),
    ("core.mesh_version", "mesh_version_id", "basin_version_id = ANY (%(bvs)s)"),
    ("core.model_instance", "model_id", "basin_version_id = ANY (%(bvs)s)"),
    ("met.met_station", "station_id", "basin_version_id = ANY (%(bvs)s)"),
    ("core.river_segment", "river_segment_key", "river_network_version_id = ANY (%(rnvs)s)"),
    ("core.river_segment_crosswalk", "crosswalk_id", "river_network_version_id = ANY (%(rnvs)s)"),
)
INVENTORY = (7, 7, 7, 7, 21, 2290, 85196, 85471)


def test_the_pinned_inventory_adds_up() -> None:
    """The per-basin seed literals and the totals are two independent readings of node-27."""
    assert sum(row[3] for row in TARGETS) == 85196
    assert sum(row[4] for row in TARGETS) == 85471
    assert sum(row[5] for row in TARGETS) == 2290
    assert sum(len(row[2]) for row in TARGETS) == 21


def _seed_basin(cursor: Any, basin_id: str, models: tuple[tuple[str, bool], ...], segments: int, crosswalk: int,
                stations: int, station_prefix: str) -> None:  # fmt: skip
    bv, rnv, mesh = _ids(basin_id)
    cursor.execute(
        "INSERT INTO core.basin (basin_id, basin_name, basin_group, description) VALUES (%s, %s, 'Basins', %s)",
        (basin_id, basin_id.replace("_", " ").title(), "改名残影 — rename\tleftover"),
    )
    cursor.execute(
        """
        INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom, active_flag, valid_from,
                                        source_uri, checksum)
        VALUES (%s, %s, 'basins',
                ST_GeomFromText('MULTIPOLYGON(((98.1 25.2, 99.3 25.2, 99.3 26.4000000000001, 98.1 25.2)))', 4490),
                false, '2026-09-22 02:24:48.596691+00', 'file:///home/ghdc/nwm/Basins/x', 'sha256:ab')
        """,
        (bv, basin_id),
    )
    cursor.execute(
        "INSERT INTO core.river_network_version (river_network_version_id, basin_version_id, version_label, "
        "segment_count, source_uri, geometry_generation) VALUES (%s, %s, 'basins', %s, 's3://nhms/rivnet', 2)",
        (rnv, bv, segments),
    )
    cursor.execute(
        "INSERT INTO core.mesh_version (mesh_version_id, basin_version_id, version_label, mesh_uri, properties_json) "
        """VALUES (%s, %s, 'basins', 's3://nhms/mesh', '{"cells": 1234, "nested": {"n": [1, 2.5, null]}}')""",
        (mesh, bv),
    )
    for model_id, active in models:
        cursor.execute(
            """
            INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id, mesh_version_id,
                                             calibration_version_id, shud_code_version, model_package_uri,
                                             active_flag, lifecycle_state, resource_profile)
            VALUES (%s, %s, %s, %s, 'calib', 'shud-2.0', 's3://nhms/pkg/', %s, %s,
                    '{"forcing_mapping_mode": "direct_grid", "basin_slug": "SE-MDZH"}')
            """,
            (model_id, bv, rnv, mesh, active, "active" if active else "inactive"),
        )
    cursor.execute(
        """
        INSERT INTO core.river_segment (river_segment_id, river_network_version_id, segment_order,
                                        downstream_segment_id, length_m, geom, properties_json)
        SELECT format('%%s_shud_reach_%%s', %(basin)s::text, lpad(i::text, 6, '0')), %(rnv)s, i,
               CASE WHEN i < %(n)s THEN format('%%s_shud_reach_%%s', %(basin)s::text, lpad((i + 1)::text, 6, '0')) END,
               i * 1.2345678901234567,
               ST_SetSRID(ST_GeomFromText(format('MULTILINESTRING((%%s 25.2, %%s 25.3000000000001))',
                                                 98 + i * 0.0001, 98 + i * 0.0001)), 4490),
               jsonb_build_object('Type', (i %% 7)::text, 'name', '河段 ' || i)
        FROM generate_series(1, %(n)s) AS i
        """,
        {"basin": basin_id, "rnv": rnv, "n": segments},
    )
    # More crosswalk rows than segments needs a second source per segment
    # (UNIQUE (river_network_version_id, river_segment_id, source)).
    cursor.execute(
        """
        INSERT INTO core.river_segment_crosswalk (river_network_version_id, river_segment_id, source, external_id,
                                                  properties_json)
        SELECT %(rnv)s, format('%%s_shud_reach_%%s', %(basin)s::text, lpad((((i - 1) %% %(n)s) + 1)::text, 6, '0')),
               'basins_seg_shp' || CASE WHEN i > %(n)s THEN '_' || ((i - 1) / %(n)s) ELSE '' END,
               '1:' || i, jsonb_build_object('w', i / 3.0)
        FROM generate_series(1, %(cw)s) AS i
        """,
        {"basin": basin_id, "rnv": rnv, "n": segments, "cw": crosswalk},
    )
    cursor.execute(
        """
        INSERT INTO met.met_station (station_id, basin_version_id, station_name, geom, elevation_m, station_role,
                                     active_flag, properties_json)
        SELECT format('%%s::cell:%%s', %(prefix)s::text, 47930 + i), %(bv)s, NULL,
               ST_SetSRID(ST_MakePoint(98 + i / 3.0, 25 + i / 7.0), 4490), 1234.5678901234567 + i,
               'direct_grid_cache', false, jsonb_build_object('cell', i)
        FROM generate_series(1, %(n)s) AS i
        """,
        {"prefix": station_prefix, "bv": bv, "n": stations},
    )


def _seed(database_url: str) -> dict[str, int]:
    """Seed the inventory; return the control run / forcing keys the hypertable rows hang off."""
    apply_migrations_from_zero(database_url)
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        # The control basin first, so no target identity key is 1.
        _seed_basin(cursor, CONTROL, ((f"{CONTROL}_shud", True),), 5, 5, 3, "control")
        for index, (basin_id, successor, models, segments, crosswalk, stations) in enumerate(TARGETS):
            _seed_basin(cursor, successor, ((f"{successor}_shud", True),), 3, 3, 2, f"successor-{index}")
            _seed_basin(cursor, basin_id, tuple((model, False) for model in models), segments, crosswalk, stations,
                        f"dg-gfs-{index:02d}")  # fmt: skip
        cursor.execute(
            "INSERT INTO met.data_source (source_id, source_name, source_type, status, native_format, adapter_name) "
            "VALUES ('gfs', 'GFS Integration', 'forecast', 'mock', 'netcdf', 'gfs')"
        )
        cursor.execute(
            """
            INSERT INTO hydro.hydro_run (run_id, run_type, scenario_id, model_id, basin_version_id,
                                         cycle_time, start_time, end_time, status, run_manifest_uri)
            VALUES ('it2621_control_run', 'forecast', 'forecast_gfs_deterministic', %s, %s,
                    '2026-09-01', '2026-09-01', '2026-09-02', 'parsed', 'integration://manifest.json')
            RETURNING run_key
            """,
            (f"{CONTROL}_shud", _ids(CONTROL)[0]),
        )
        run_key = cursor.fetchone()["run_key"]
        cursor.execute(
            """
            INSERT INTO met.forcing_version (forcing_version_id, model_id, source_id, start_time, end_time,
                                             station_count, forcing_package_uri)
            VALUES ('it2621_control_forcing', %s, 'gfs', '2026-09-01', '2026-09-02', 3, 'integration://forcing')
            RETURNING forcing_version_key
            """,
            (f"{CONTROL}_shud",),
        )
        forcing_key = cursor.fetchone()["forcing_version_key"]
        cursor.execute(
            "SELECT basin_version_key FROM core.basin_version WHERE basin_version_id = %s", (_ids(CONTROL)[0],)
        )
        bv_key = cursor.fetchone()["basin_version_key"]
        cursor.execute(
            "SELECT river_network_version_key FROM core.river_network_version WHERE river_network_version_id = %s",
            (_ids(CONTROL)[1],),
        )
        rnv_key = cursor.fetchone()["river_network_version_key"]
        cursor.execute(
            "SELECT river_segment_key FROM core.river_segment WHERE river_network_version_id = %s "
            "ORDER BY river_segment_key LIMIT 1",
            (_ids(CONTROL)[1],),
        )
        segment_key = cursor.fetchone()["river_segment_key"]
        cursor.execute(
            "SELECT station_id, station_key FROM met.met_station WHERE basin_version_id = %s "
            "ORDER BY station_key LIMIT 1",
            (_ids(CONTROL)[0],),
        )
        station = cursor.fetchone()
    keys = {"run_key": run_key, "forcing_key": forcing_key, "bv_key": bv_key, "rnv_key": rnv_key}
    # Control rows in every hypertable, compressed: the probes must scan them and find no target key.
    _insert_hypertable_row(database_url, "hydro.river_timeseries", keys, segment_key, station)
    _insert_hypertable_row(database_url, "met.forcing_station_timeseries", keys, segment_key, station)
    _insert_hypertable_row(database_url, "met.forcing_station_timeseries_legacy", keys, segment_key, station)
    return keys


def _insert_hypertable_row(
    database_url: str, relation: str, keys: dict[str, int], segment_key: int, station: dict[str, Any]
) -> None:
    """One row in ``relation`` hanging off the CONTROL run/forcing, then compress every chunk."""
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        if relation == "hydro.river_timeseries":
            cursor.execute(
                "INSERT INTO hydro.river_timeseries (run_key, basin_version_key, river_network_version_key, "
                "river_segment_key, valid_time, lead_time_hours, variable_e, value, unit_e, quality_flag_e) "
                "VALUES (%s, %s, %s, %s, '2026-09-01 06:00+00', 6, 'q_down', 1.5, 'm3/s', 'ok')",
                (keys["run_key"], keys["bv_key"], keys["rnv_key"], segment_key),
            )
        elif relation == "met.forcing_station_timeseries":
            cursor.execute(
                "INSERT INTO met.forcing_station_timeseries (forcing_version_key, station_key, valid_time, "
                "variable_e, value, unit_e, quality_flag_e) VALUES (%s, %s, '2026-09-01 06:00+00', 'PRCP', 1.0, "
                "'mm/day', 'ok')",
                (keys["forcing_key"], station["station_key"]),
            )
        else:
            cursor.execute(
                "INSERT INTO met.forcing_station_timeseries_legacy (forcing_version_id, basin_version_id, station_id, "
                "valid_time, source_id, variable, value, unit, quality_flag) VALUES ('it2621_control_forcing', %s, "
                "%s, '2026-09-01 06:00+00', 'gfs', 'PRCP', 1.0, 'mm/day', 'ok')",
                (_ids(CONTROL)[0], station["station_id"]),
            )
    _compress_all(database_url, relation)


def _compress_all(database_url: str, relation: str) -> None:
    """Compress every chunk of ``relation`` and prove it: one chunk, none left uncompressed."""
    connection = psycopg2.connect(database_url)
    connection.autocommit = True  # compress_chunk cannot run inside a transaction block
    try:
        with connection.cursor() as cursor:
            schema, table = relation.split(".")
            cursor.execute(
                "SELECT format('%%I.%%I', chunk_schema, chunk_name) FROM timescaledb_information.chunks "
                "WHERE hypertable_schema = %s AND hypertable_name = %s AND NOT is_compressed",
                (schema, table),
            )
            for (chunk,) in cursor.fetchall():
                cursor.execute("SELECT compress_chunk(%s::regclass)", (chunk,))
            cursor.execute(
                "SELECT count(*) FILTER (WHERE NOT is_compressed), count(*) FROM timescaledb_information.chunks "
                "WHERE hypertable_schema = %s AND hypertable_name = %s",
                (schema, table),
            )
            uncompressed, total = cursor.fetchone()
            assert (uncompressed, total) == (0, 1), (relation, uncompressed, total)
    finally:
        connection.close()


def _digest(database_url: str, *, targets: bool) -> dict[str, tuple[int, str]]:
    """Per table: (rows, md5 of every row's full text in key order), for the target rows or all others."""
    params = {"basins": list(BASINS), "bvs": BVS, "rnvs": RNVS}
    fingerprint: dict[str, tuple[int, str]] = {}
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SET TimeZone = 'UTC'")
        for relation, key, predicate in _TABLES:
            scope = predicate if targets else f"NOT ({predicate})"
            cursor.execute(
                f"SELECT count(*) AS n, md5(coalesce(string_agg(t::text, E'\\n' ORDER BY t.{key}), '')) AS h "
                f"FROM {relation} t WHERE {scope}",
                params,
            )
            row = cursor.fetchone()
            fingerprint[relation] = (row["n"], row["h"])
    return fingerprint


def _counts(fingerprint: dict[str, tuple[int, str]]) -> tuple[int, ...]:
    return tuple(fingerprint[relation][0] for relation, _, _ in _TABLES)


def _copy_files(directory: Path) -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(directory.iterdir())}


def _mkdir(tmp_path: Path, name: str) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    return directory


def test_apply_then_rollback_restores_every_row_byte_exact(throwaway_database_url: str, tmp_path: Path) -> None:
    url = throwaway_database_url
    _seed(url)
    targets, others = _digest(url, targets=True), _digest(url, targets=False)
    assert _counts(targets) == INVENTORY
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT min(station_key) AS s FROM met.met_station WHERE basin_version_id = ANY (%s)", (BVS,))
        assert cursor.fetchone()["s"] > 1
    dry_dir, apply_dir = _mkdir(tmp_path, "dry-run"), _mkdir(tmp_path, "apply")

    dry_run = run_sql_file(DELETE_SQL, database_url=url, copy_dir=dry_dir, settings=SETTINGS)
    assert not dry_run.committed
    assert _digest(url, targets=True) == targets, "the default dry-run must roll back"
    notices = "\n".join(dry_run.notices)
    for expected in (
        "#2621 target basins_dnzh_mdzh -> successor basins_se_mdzh",
        "#2621 target basins_xinan_nujiang -> successor basins_sw_nujiang",
        "#2621 locked basin=7 basin_version=7 river_network_version=7 mesh_version=7 model_instance=21 "
        "met_station=2290 river_segment=85196 river_segment_crosswalk=85471",
        "#2621 probe hydro.river_timeseries.river_segment_key: 0 row(s) for 85196 key(s) in",
        "#2621 probe met.forcing_station_timeseries.station_key: 0 row(s) for 2290 key(s) in",
        "#2621 probe met.forcing_station_timeseries_legacy.station_id: 0 row(s) for 2290 id(s) in",
        "#2621 deleted core.river_segment=85196 (session_replication_role=replica) in",
        "#2621 deleted met.met_station=2290 (session_replication_role=replica) in",
        "#2621 deleted core.basin=7 in",
        "#2621 gate B: no orphan references",
    ):
        assert expected in notices, (expected, dry_run.notices)
    assert sorted(_copy_files(dry_dir)) == sorted(f"{relation}.copy" for relation, _, _ in _TABLES)

    applied = run_sql_file(DELETE_SQL, database_url=url, copy_dir=apply_dir, settings=SETTINGS, apply=True)
    assert applied.committed
    assert _counts(_digest(url, targets=True)) == (0,) * len(_TABLES)
    assert _digest(url, targets=False) == others, "only the target rows may go"
    written = _copy_files(apply_dir)
    assert tuple(len(written[f"{relation}.copy"].splitlines()) for relation, _, _ in _TABLES) == INVENTORY
    assert written == _copy_files(dry_dir), "nothing changed between the runs, so neither may their backups"

    restored = run_sql_file(ROLLBACK_SQL, database_url=url, copy_dir=apply_dir, apply=True)
    assert restored.committed
    assert _digest(url, targets=True) == targets
    assert _digest(url, targets=False) == others


def test_the_delete_refuses_to_run_without_a_copy_dir(throwaway_database_url: str) -> None:
    url = throwaway_database_url
    _seed(url)
    before = _digest(url, targets=True)

    with pytest.raises(ValueError, match="copy directory is required"):
        run_sql_file(DELETE_SQL, database_url=url, settings=SETTINGS, apply=True)
    assert _digest(url, targets=True) == before


def test_a_dry_run_copy_dir_cannot_be_reused_for_the_apply(throwaway_database_url: str, tmp_path: Path) -> None:
    url = throwaway_database_url
    _seed(url)
    run_sql_file(DELETE_SQL, database_url=url, copy_dir=tmp_path, settings=SETTINGS)
    dry_files = _copy_files(tmp_path)
    before = _digest(url, targets=True)

    with pytest.raises(FileExistsError):
        run_sql_file(DELETE_SQL, database_url=url, copy_dir=tmp_path, settings=SETTINGS, apply=True)
    assert _digest(url, targets=True) == before
    assert _copy_files(tmp_path) == dry_files


_TARGET_BV = _ids("basins_xinan_nujiang")[0]
_TARGET_RNV = _ids("basins_xinan_nujiang")[1]


@pytest.mark.parametrize(
    ("mutation", "settings", "message"),
    [
        (
            f"INSERT INTO met.met_station (station_id, basin_version_id, geom) "
            f"VALUES ('late-station', '{_TARGET_BV}', ST_SetSRID(ST_MakePoint(98, 25), 4490))",
            SETTINGS,
            r"basins_xinan_nujiang: met_station=633 \(want 632\)",
        ),
        (
            "DELETE FROM core.river_segment_crosswalk WHERE crosswalk_id = (SELECT max(crosswalk_id) "
            f"FROM core.river_segment_crosswalk WHERE river_network_version_id = '{_TARGET_RNV}')",
            SETTINGS,
            r"basins_xinan_nujiang: river_segment_crosswalk=23669 \(want 23670\)",
        ),
        (
            "INSERT INTO hydro.hydro_run (run_id, run_type, scenario_id, model_id, basin_version_id, cycle_time, "
            "start_time, end_time, status, run_manifest_uri) VALUES ('it2621_run', 'forecast', 'forecast_gfs', "
            f"'basins_xinan_nujiang_shud', '{_TARGET_BV}', '2026-09-22', '2026-09-22', '2026-09-23', 'parsed', 'x')",
            SETTINGS,
            r"hydro\.hydro_run has 1 row\(s\) by basin_version_id",
        ),
        (
            "UPDATE core.model_instance SET active_flag = true, lifecycle_state = 'active' "
            "WHERE model_id = 'dg_b5047053411ae5aafd0dd5dad58d5535'",
            SETTINGS,
            r"1 active model\(s\) among the targets",
        ),
        (
            # A model under a successor's basin_version that points at a target river network.
            "INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id, mesh_version_id, "
            "calibration_version_id, shud_code_version, model_package_uri, active_flag) VALUES ('dg_stray', "
            f"'basins_sw_nujiang_vbasins', '{_TARGET_RNV}', 'basins_sw_nujiang_mesh_vbasins', 'c', 's', 'u', false)",
            SETTINGS,
            r"model_instance set .*dg_stray",
        ),
        (
            "UPDATE core.model_instance SET active_flag = false, lifecycle_state = 'inactive' "
            "WHERE model_id = 'basins_sw_nujiang_shud'",
            SETTINGS,
            r"successor basins_sw_nujiang of basins_xinan_nujiang: 1 basin row\(s\), 0 active model\(s\)",
        ),
        (
            "CREATE TABLE ops.it2621_unexpected (basin_id text REFERENCES core.basin (basin_id))",
            SETTINGS,
            r"unexpected=\{\"ops\.it2621_unexpected\|core\.basin\|",
        ),
        (
            "SELECT 1",
            {"nhms.manifest_basins": f"{MANIFEST},basins_dnzh_qtj"},
            r"basins_dnzh_qtj is in nhms\.manifest_basins",
        ),
        (
            "SELECT 1",
            {"nhms.manifest_basins": "basins_dnzh_qtj_typo"},
            r"successor basins_se_mdzh .* not in nhms\.manifest_basins",
        ),
        ("SELECT 1", {}, r"nhms\.manifest_basins is unset or empty"),
        ("SELECT 1", {**SETTINGS, "nhms.probe_timeout_s": "59"}, r"nhms\.probe_timeout_s=59 .*60\.\.3600"),
        ("SELECT 1", {**SETTINGS, "nhms.probe_timeout_s": "3601"}, r"nhms\.probe_timeout_s=3601 .*60\.\.3600"),
        ("SELECT 1", {**SETTINGS, "nhms.probe_timeout_s": "1h"}, r"nhms\.probe_timeout_s=1h .*60\.\.3600"),
    ],
    ids=[
        "station-count-drift",
        "crosswalk-count-drift",
        "hydro-run",
        "active-model",
        "model-set-union",
        "successor-without-active-model",
        "fk-set-drift",
        "target-in-manifest",
        "successor-not-in-manifest",
        "manifest-unset",
        "probe-timeout-below-range",
        "probe-timeout-above-range",
        "probe-timeout-not-an-integer",
    ],
)  # fmt: skip
def test_a_gate_violation_raises_and_deletes_nothing(
    throwaway_database_url: str, tmp_path: Path, mutation: str, settings: dict[str, str], message: str
) -> None:
    url = throwaway_database_url
    _seed(url)
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute(mutation)
    before = _digest(url, targets=True)

    with pytest.raises(ScriptFailedError, match=message) as raised:
        run_sql_file(DELETE_SQL, database_url=url, copy_dir=tmp_path, settings=settings, apply=True)

    assert raised.value.pgcode == "P0001", raised.value
    assert _digest(url, targets=True) == before


def test_an_undeclared_setting_is_refused_before_connecting(throwaway_database_url: str, tmp_path: Path) -> None:
    url = throwaway_database_url
    _seed(url)
    before = _digest(url, targets=True)

    with pytest.raises(ValueError, match=r"nhms\.expected_segments.*not declared"):
        run_sql_file(
            DELETE_SQL, database_url=url, copy_dir=tmp_path, settings={**SETTINGS, "nhms.expected_segments": "1"},
            apply=True,
        )  # fmt: skip
    assert _digest(url, targets=True) == before
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("relation", "column"),
    [
        ("hydro.river_timeseries", "river_segment_key"),
        ("met.forcing_station_timeseries", "station_key"),
        ("met.forcing_station_timeseries_legacy", "station_id"),
    ],
)
def test_a_target_reference_in_a_compressed_hypertable_chunk_raises_before_any_delete(
    throwaway_database_url: str, tmp_path: Path, relation: str, column: str
) -> None:
    """In replica mode the hypertable FK would not fire: the zero-reference probe is the only guard."""
    url = throwaway_database_url
    keys = _seed(url)
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT river_segment_key FROM core.river_segment WHERE river_network_version_id = %s "
            "ORDER BY river_segment_key DESC LIMIT 1",
            (_TARGET_RNV,),
        )
        segment_key = cursor.fetchone()["river_segment_key"]
        cursor.execute(
            "SELECT station_id, station_key FROM met.met_station WHERE basin_version_id = %s "
            "ORDER BY station_key DESC LIMIT 1",
            (_TARGET_BV,),
        )
        station = cursor.fetchone()
    _insert_target_reference(url, relation, keys, segment_key, station)
    before = _digest(url, targets=True)

    with pytest.raises(ScriptFailedError, match=rf"{relation}\.{column}: 1 row\(s\) reference the targets") as raised:
        run_sql_file(DELETE_SQL, database_url=url, copy_dir=tmp_path, settings=SETTINGS, apply=True)

    assert raised.value.pgcode == "P0001", raised.value
    assert _digest(url, targets=True) == before


def _insert_target_reference(
    database_url: str, relation: str, keys: dict[str, int], segment_key: int, station: dict[str, Any]
) -> None:
    """A row keyed on a TARGET segment/station but owned by the CONTROL run/forcing, in a compressed chunk.

    TimescaleDB 2.10 refuses an INSERT into a compressed chunk with a unique
    constraint, so the chunk is decompressed, written, and compressed again.
    """
    _decompress_all(database_url, relation)
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        if relation == "hydro.river_timeseries":
            cursor.execute(
                "INSERT INTO hydro.river_timeseries (run_key, basin_version_key, river_network_version_key, "
                "river_segment_key, valid_time, lead_time_hours, variable_e, value, unit_e, quality_flag_e) "
                "VALUES (%s, %s, %s, %s, '2026-09-01 12:00+00', 12, 'q_down', 2.5, 'm3/s', 'ok')",
                (keys["run_key"], keys["bv_key"], keys["rnv_key"], segment_key),
            )
        elif relation == "met.forcing_station_timeseries":
            cursor.execute(
                "INSERT INTO met.forcing_station_timeseries (forcing_version_key, station_key, valid_time, "
                "variable_e, value, unit_e, quality_flag_e) VALUES (%s, %s, '2026-09-01 12:00+00', 'PRCP', 2.0, "
                "'mm/day', 'ok')",
                (keys["forcing_key"], station["station_key"]),
            )
        else:
            cursor.execute(
                "INSERT INTO met.forcing_station_timeseries_legacy (forcing_version_id, basin_version_id, station_id, "
                "valid_time, source_id, variable, value, unit, quality_flag) VALUES ('it2621_control_forcing', %s, "
                "%s, '2026-09-01 12:00+00', 'gfs', 'PRCP', 2.0, 'mm/day', 'ok')",
                (_ids(CONTROL)[0], station["station_id"]),
            )
    # Recompress: the chunk now holds the target reference in compressed form only.
    _compress_all(database_url, relation)


def _decompress_all(database_url: str, relation: str) -> None:
    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            schema, table = relation.split(".")
            cursor.execute(
                "SELECT format('%%I.%%I', chunk_schema, chunk_name) FROM timescaledb_information.chunks "
                "WHERE hypertable_schema = %s AND hypertable_name = %s AND is_compressed",
                (schema, table),
            )
            for (chunk,) in cursor.fetchall():
                cursor.execute("SELECT decompress_chunk(%s::regclass)", (chunk,))
    finally:
        connection.close()


def test_a_role_that_cannot_set_replica_mode_is_refused_without_a_slow_fallback(
    throwaway_database_url: str, tmp_path: Path
) -> None:
    """Run as the built-in, non-superuser ``pg_read_all_data``: no role DDL on the shared cluster.

    The permission trial is the first gate after the settings, so the refusal
    lands before any table is read, locked, probed or backed up.
    """
    url = throwaway_database_url
    _seed(url)
    before = _digest(url, targets=True)

    with pytest.raises(ScriptFailedError, match=r"session_replication_role=replica is not permitted") as raised:
        run_sql_file(
            DELETE_SQL, database_url=make_dsn(url, options="-c role=pg_read_all_data"), copy_dir=tmp_path,
            settings=SETTINGS, apply=True,
        )  # fmt: skip

    assert raised.value.pgcode == "P0001", raised.value
    assert "for role pg_read_all_data" in str(raised.value)
    assert _digest(url, targets=True) == before
    assert list(tmp_path.iterdir()) == [], "refused before any backup was written"


def test_rollback_refuses_to_restore_over_rows_that_still_exist(throwaway_database_url: str, tmp_path: Path) -> None:
    url = throwaway_database_url
    _seed(url)
    run_sql_file(DELETE_SQL, database_url=url, copy_dir=tmp_path, settings=SETTINGS)  # dry-run: rows remain
    before = _digest(url, targets=True)

    with pytest.raises(ScriptFailedError, match=r"#2621 rollback: .* still exist") as raised:
        run_sql_file(ROLLBACK_SQL, database_url=url, copy_dir=tmp_path, apply=True)

    assert raised.value.pgcode == "P0001"
    assert _digest(url, targets=True) == before
