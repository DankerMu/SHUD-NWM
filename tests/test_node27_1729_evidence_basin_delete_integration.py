"""#1729 delete / backup / rollback scripts on a disposable database.

Runs the three checked-in files ``scripts/ops/node27_1729_delete_evidence_basin*.sql``
through the same runner the operator uses on node-27
(``scripts/ops/node27_oneshot_sql.py``), unchanged. The delete run itself
writes the backup (``@copy-out``, rows locked FOR UPDATE in the same
transaction as the DELETEs), so the rollback restores from the ``--apply``
delete run's copy directory; ``_backup.sql`` is only an optional read-only
precheck. The seed reproduces the
node-27 inventory (``.workplans/k3``): the evidence basin with 1 basin_version,
1 river_network_version, 1 mesh_version, 2 model_instance and 6 met_station
rows, the same ids, next to a control basin that must never be touched.

``flood.*`` exists on node-27 only through ledger migrations that left
``db/migrations`` (``db/roles/node27_write_roles.sql``), so the seed recreates
the two flood tables the script reads -- ``flood_frequency_curve`` with its FK
to ``core.model_instance`` -- and the migrated catalog plus that FK is exactly
the 20-FK set the script pins (the node-27 non-chunk set). The hypertable test
adds a real chunk, whose TimescaleDB copies of the FKs the pin must ignore.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.ops.node27_oneshot_sql import ScriptFailedError, run_sql_file
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection

pytestmark = pytest.mark.integration

_OPS = Path(__file__).resolve().parents[1] / "scripts" / "ops"
BACKUP_SQL = _OPS / "node27_1729_delete_evidence_basin_backup.sql"
DELETE_SQL = _OPS / "node27_1729_delete_evidence_basin.sql"
ROLLBACK_SQL = _OPS / "node27_1729_delete_evidence_basin_rollback.sql"

BASIN = "basin__evidence_cmfd_p02_synth"
BV = f"{BASIN}__v1"
RNV = "rnw__evidence_cmfd_p02_synth__v1"
MESH = "mesh__evidence_cmfd_p02_synth__v1"
MODELS = ("dg_10d27a62b35b39cb5a6f9d10f7fff6e9", "model__evidence_cmfd_p02_synth__v1")
STATIONS = (
    "synth-mip-m1-v2::cell:cell-0100.00-0030.00",
    "synth-mip-m1-v2::cell:cell-0100.00-0030.50",
    "synth-mip-m1-v2::cell:cell-0100.50-0030.00",
    "synth-station-001",
    "synth-station-002",
    "synth-station-003",
)
CONTROL = "basins_it1729_control"

# (relation, id column, projection) -- every column, geometry as EWKB hex + SRID.
_SNAPSHOT: tuple[tuple[str, str, str], ...] = (
    ("core.basin", "basin_id", "t.*"),
    (
        "core.basin_version",
        "basin_version_id",
        "t.*, encode(ST_AsEWKB(t.geom), 'hex') AS geom_ewkb, ST_SRID(t.geom) AS geom_srid",
    ),
    ("core.river_network_version", "river_network_version_id", "t.*"),
    ("core.mesh_version", "mesh_version_id", "t.*"),
    ("core.model_instance", "model_id", "t.*"),
    (
        "met.met_station",
        "station_id",
        "t.*, encode(ST_AsEWKB(t.geom), 'hex') AS geom_ewkb, ST_SRID(t.geom) AS geom_srid",
    ),
)


def _seed_basin(
    cursor: Any, basin_id: str, group: str, suffix: str, models: tuple[str, ...], stations: tuple[str, ...]
) -> None:
    bv, rnv, mesh = f"{basin_id}__v1", f"rnw{suffix}__v1", f"mesh{suffix}__v1"
    cursor.execute(
        "INSERT INTO core.basin (basin_id, basin_name, basin_group, description) VALUES (%s, %s, %s, %s)",
        (basin_id, f"{basin_id} name", group, "NOT a real basin — 合成夹具\ttab"),
    )
    cursor.execute(
        """
        INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom, active_flag, valid_from)
        VALUES (%s, %s, 'cmfd-p0.2-synth-v1',
                ST_GeomFromText('MULTIPOLYGON(((99.9 29.9, 100.6 29.9, 100.6 30.6000000000001, 99.9 30.6, 99.9 29.9)))',
                                4490),
                false, '2026-07-07 02:03:49.158577+00')
        """,
        (bv, basin_id),
    )
    cursor.execute(
        "INSERT INTO core.river_network_version (river_network_version_id, basin_version_id, version_label, "
        "segment_count) VALUES (%s, %s, 'v1', 0)",
        (rnv, bv),
    )
    cursor.execute(
        "INSERT INTO core.mesh_version (mesh_version_id, basin_version_id, version_label, mesh_uri, properties_json) "
        """VALUES (%s, %s, 'v1', 's3://nhms/synth/mesh', '{"note": "evidence", "nested": {"n": [1, 2.5, null]}}')""",
        (mesh, bv),
    )
    for model_id in models:
        cursor.execute(
            """
            INSERT INTO core.model_instance (
                model_id, basin_version_id, river_network_version_id, mesh_version_id,
                calibration_version_id, shud_code_version, model_package_uri, resource_profile
            )
            VALUES (%s, %s, %s, %s, 'calib', 'shud', 's3://nhms/synth/pkg/', '{"forcing_mapping_mode": "direct_grid"}')
            """,
            (model_id, bv, rnv, mesh),
        )
    for index, station_id in enumerate(stations):
        cursor.execute(
            """
            INSERT INTO met.met_station (
                station_id, basin_version_id, station_name, geom, elevation_m, active_flag, properties_json
            )
            VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4490), %s, false, %s)
            """,
            (
                station_id,
                bv,
                f"station {index}",
                100.0 + index / 3,
                30.0 + index / 7,
                1234.5678901234567 + index,
                '{"seed": "synthetic", "idx": %d}' % index,
            ),
        )


def _seed(database_url: str) -> None:
    apply_migrations_from_zero(database_url)
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("CREATE SCHEMA flood")
        cursor.execute(
            """
            CREATE TABLE flood.flood_frequency_curve (
                curve_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                model_id text NOT NULL REFERENCES core.model_instance (model_id),
                basin_version_id text,
                river_network_version_id text
            )
            """
        )
        cursor.execute(
            "CREATE TABLE flood.return_period_result (result_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
            "model_id text, basin_version_id text, river_network_version_id text)"
        )
        # A control basin first, so the evidence rows' identity keys are not 1.
        _seed_basin(cursor, CONTROL, "Basins", "_it1729_control", (f"{CONTROL}_model",), (f"{CONTROL}_station",))
        _seed_basin(cursor, BASIN, "evidence-only", "__evidence_cmfd_p02_synth", MODELS, STATIONS)
        cursor.execute(
            "INSERT INTO ops.audit_log (actor, actor_role, action, entity_type, entity_id) "
            "VALUES ('it', 'sys_admin', 'register', 'basin', %s)",
            (BASIN,),
        )


def _snapshot(database_url: str, basin_id: str) -> dict[str, list[dict[str, Any]]]:
    bv = f"{basin_id}__v1"
    scope = {
        "core.basin": ("basin_id", basin_id),
        "core.basin_version": ("basin_id", basin_id),
        "core.river_network_version": ("basin_version_id", bv),
        "core.mesh_version": ("basin_version_id", bv),
        "core.model_instance": ("basin_version_id", bv),
        "met.met_station": ("basin_version_id", bv),
    }
    snapshot: dict[str, list[dict[str, Any]]] = {}
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        for relation, id_column, projection in _SNAPSHOT:
            column, value = scope[relation]
            cursor.execute(
                f"SELECT {projection} FROM {relation} t WHERE t.{column} = %s ORDER BY t.{id_column}",
                (value,),
            )
            rows = []
            for row in cursor.fetchall():
                rendered = dict(row)
                rendered.pop("geom", None)  # compared as EWKB hex + SRID above
                rows.append(rendered)
            snapshot[relation] = rows
    return snapshot


# FKs onto met.met_station declared by TimescaleDB-internal relations: the
# compressed-hypertable tables from the migrations, plus one per chunk.
_INTERNAL_STATION_FKS = (
    "SELECT count(*) FROM pg_constraint c JOIN pg_class r ON r.oid = c.conrelid "
    "JOIN pg_namespace n ON n.oid = r.relnamespace "
    "WHERE c.contype = 'f' AND n.nspname = '_timescaledb_internal' AND c.confrelid = 'met.met_station'::regclass"
)


def _count(database_url: str, sql: str, *params: Any) -> int:
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(sql, params)
        return int(next(iter(cursor.fetchone().values())))


def _copy_files(directory: Path) -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(directory.iterdir())}


def test_the_apply_delete_backs_up_what_it_deletes_and_rollback_restores_it_byte_exact(
    throwaway_database_url: str, tmp_path: Path
) -> None:
    url = throwaway_database_url
    _seed(url)
    before = _snapshot(url, BASIN)
    control_before = _snapshot(url, CONTROL)
    assert [len(before[relation]) for relation, _, _ in _SNAPSHOT] == [1, 1, 1, 1, 2, 6]
    assert {row["geom_srid"] for row in before["met.met_station"] + before["core.basin_version"]} == {4490}
    assert min(row["station_key"] for row in before["met.met_station"]) > 1
    dry_dir, apply_dir = tmp_path / "dry-run", tmp_path / "apply"
    dry_dir.mkdir()
    apply_dir.mkdir()

    # The default dry-run writes its own backup files but commits nothing.
    dry_run = run_sql_file(DELETE_SQL, database_url=url, copy_dir=dry_dir)
    assert not dry_run.committed
    assert _snapshot(url, BASIN) == before, "the default dry-run must roll back"
    assert any("deleted core.basin=1" in notice for notice in dry_run.notices), dry_run.notices
    assert sorted(_copy_files(dry_dir)) == sorted(f"{relation}.copy" for relation, _, _ in _SNAPSHOT)

    applied = run_sql_file(DELETE_SQL, database_url=url, copy_dir=apply_dir, apply=True)
    assert applied.committed
    assert any("retained ops.audit_log rows (entity_id equality): 1" in notice for notice in applied.notices)
    assert all(rows == [] for rows in _snapshot(url, BASIN).values())
    assert _snapshot(url, CONTROL) == control_before
    assert _count(url, "SELECT count(*) FROM ops.audit_log WHERE entity_id = %s", BASIN) == 1
    written = _copy_files(apply_dir)
    assert [len(written[f"{relation}.copy"].splitlines()) for relation, _, _ in _SNAPSHOT] == [1, 1, 1, 1, 2, 6]
    assert written == _copy_files(dry_dir), "nothing changed between the runs, so neither may their backups"

    # The restore reads the --apply delete run's own directory.
    restored = run_sql_file(ROLLBACK_SQL, database_url=url, copy_dir=apply_dir, apply=True)
    assert restored.committed
    assert _snapshot(url, BASIN) == before
    assert _snapshot(url, CONTROL) == control_before


def test_the_delete_refuses_to_run_without_a_copy_dir(throwaway_database_url: str) -> None:
    url = throwaway_database_url
    _seed(url)
    before = _snapshot(url, BASIN)

    with pytest.raises(ValueError, match="copy directory is required"):
        run_sql_file(DELETE_SQL, database_url=url, apply=True)

    assert _snapshot(url, BASIN) == before


def test_a_dry_run_copy_dir_cannot_be_reused_for_the_apply(throwaway_database_url: str, tmp_path: Path) -> None:
    """The runner never overwrites a copy file, so --apply needs a fresh --copy-dir."""
    url = throwaway_database_url
    _seed(url)
    run_sql_file(DELETE_SQL, database_url=url, copy_dir=tmp_path)
    dry_files = _copy_files(tmp_path)
    before = _snapshot(url, BASIN)

    with pytest.raises(FileExistsError):
        run_sql_file(DELETE_SQL, database_url=url, copy_dir=tmp_path, apply=True)

    assert _snapshot(url, BASIN) == before
    assert _copy_files(tmp_path) == dry_files


def test_the_restore_carries_a_change_made_after_a_standalone_precheck(
    throwaway_database_url: str, tmp_path: Path
) -> None:
    """The authoritative backup is the one taken with the rows locked, not an earlier precheck.

    A lifecycle transition and a station property write land between the
    optional precheck and the delete; restoring from the delete run's directory
    brings back that later state, which the precheck files do not hold.
    """
    url = throwaway_database_url
    _seed(url)
    precheck_dir, apply_dir = tmp_path / "precheck", tmp_path / "apply"
    precheck_dir.mkdir()
    apply_dir.mkdir()
    precheck = run_sql_file(BACKUP_SQL, database_url=url, copy_dir=precheck_dir)
    assert not precheck.committed
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.model_instance SET lifecycle_state = 'deprecated', active_flag = false WHERE model_id = %s",
            (MODELS[0],),
        )
        cursor.execute(
            'UPDATE met.met_station SET properties_json = properties_json || \'{"late": "write"}\' '
            "WHERE station_id = %s",
            (STATIONS[4],),
        )
    mutated = _snapshot(url, BASIN)

    run_sql_file(DELETE_SQL, database_url=url, copy_dir=apply_dir, apply=True)
    assert all(rows == [] for rows in _snapshot(url, BASIN).values())
    precheck_files, delete_files = _copy_files(precheck_dir), _copy_files(apply_dir)
    assert sorted(precheck_files) == sorted(delete_files)
    changed = sorted(name for name in delete_files if delete_files[name] != precheck_files[name])
    assert changed == ["core.model_instance.copy", "met.met_station.copy"], changed

    run_sql_file(ROLLBACK_SQL, database_url=url, copy_dir=apply_dir, apply=True)
    restored = _snapshot(url, BASIN)
    assert restored == mutated
    model = next(row for row in restored["core.model_instance"] if row["model_id"] == MODELS[0])
    assert (model["lifecycle_state"], model["active_flag"]) == ("deprecated", False)


def test_a_dependency_count_mismatch_raises_and_deletes_nothing(throwaway_database_url: str, tmp_path: Path) -> None:
    url = throwaway_database_url
    _seed(url)
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO met.met_station (station_id, basin_version_id, geom) "
            "VALUES ('synth-station-004', %s, ST_SetSRID(ST_MakePoint(100, 30), 4490))",
            (BV,),
        )
    before = _snapshot(url, BASIN)

    with pytest.raises(ScriptFailedError, match="met_station set") as raised:
        run_sql_file(DELETE_SQL, database_url=url, copy_dir=tmp_path, apply=True)

    assert raised.value.pgcode == "P0001"
    assert _snapshot(url, BASIN) == before


def test_a_non_hypertable_dependent_row_raises_and_deletes_nothing(throwaway_database_url: str, tmp_path: Path) -> None:
    url = throwaway_database_url
    _seed(url)
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO hydro.hydro_run (run_id, run_type, scenario_id, model_id, basin_version_id,
                                         cycle_time, start_time, end_time, status, run_manifest_uri)
            VALUES ('it1729_run', 'forecast', 'forecast_gfs_deterministic', %s, %s,
                    '2026-05-14', '2026-05-14', '2026-05-15', 'parsed', 'integration://manifest.json')
            """,
            (MODELS[1], BV),
        )
    before = _snapshot(url, BASIN)

    with pytest.raises(ScriptFailedError, match=r"hydro\.hydro_run has 1 row\(s\) by basin_version_id"):
        run_sql_file(DELETE_SQL, database_url=url, copy_dir=tmp_path, apply=True)
    assert _snapshot(url, BASIN) == before


def test_a_hypertable_reference_fails_the_delete_through_its_fk(throwaway_database_url: str, tmp_path: Path) -> None:
    """No hypertable is scanned: a referencing chunk row fails the DELETE itself.

    The chunk this INSERT creates carries TimescaleDB's per-chunk copies of the
    hypertable FKs in ``_timescaledb_internal`` (next to the compressed-table
    copies the migrations already made); the FK-set pin must ignore them, or it
    would RAISE before the delete is ever attempted.
    """
    url = throwaway_database_url
    _seed(url)
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO met.data_source (source_id, source_name, source_type, status, native_format, adapter_name) "
            "VALUES ('gfs', 'GFS Integration', 'forecast', 'mock', 'netcdf', 'gfs')"
        )
        cursor.execute(
            """
            INSERT INTO met.forcing_version (forcing_version_id, model_id, source_id, start_time, end_time,
                                             station_count, forcing_package_uri)
            VALUES ('it1729_control_forcing', %s, 'gfs', '2026-05-14', '2026-05-15', 1, 'integration://forcing')
            RETURNING forcing_version_key
            """,
            (f"{CONTROL}_model",),
        )
        forcing_key = cursor.fetchone()["forcing_version_key"]
        cursor.execute("SELECT station_key FROM met.met_station WHERE station_id = %s", (STATIONS[3],))
        station_key = cursor.fetchone()["station_key"]
    internal_fks_before = _count(url, _INTERNAL_STATION_FKS)
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO met.forcing_station_timeseries (forcing_version_key, station_key, valid_time, variable_e, "
            "value, unit_e, quality_flag_e) VALUES (%s, %s, '2026-05-14', 'PRCP', 1.0, 'mm/day', 'ok')",
            (forcing_key, station_key),
        )
    assert _count(url, _INTERNAL_STATION_FKS) > internal_fks_before, (
        "the new chunk must carry its own FK copy, or this test proves nothing about the exclusion"
    )
    before = _snapshot(url, BASIN)

    with pytest.raises(ScriptFailedError) as raised:
        run_sql_file(DELETE_SQL, database_url=url, copy_dir=tmp_path, apply=True)

    assert raised.value.pgcode == "23503", raised.value
    assert _snapshot(url, BASIN) == before


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            "CREATE TABLE ops.it1729_unexpected (basin_id text REFERENCES core.basin (basin_id))",
            r"unexpected=\{\"ops\.it1729_unexpected\|core\.basin\|",
        ),
        (
            "ALTER TABLE flood.flood_frequency_curve DROP CONSTRAINT flood_frequency_curve_model_id_fkey",
            r"missing=\{\"flood\.flood_frequency_curve\|core\.model_instance\|",
        ),
    ],
    ids=["unexpected-dependent", "missing-dependent"],
)
def test_a_live_fk_set_drift_raises_and_deletes_nothing(
    throwaway_database_url: str, tmp_path: Path, mutation: str, message: str
) -> None:
    url = throwaway_database_url
    _seed(url)
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute(mutation)
    before = _snapshot(url, BASIN)

    with pytest.raises(ScriptFailedError, match=message):
        run_sql_file(DELETE_SQL, database_url=url, copy_dir=tmp_path, apply=True)
    assert _snapshot(url, BASIN) == before


def test_a_basin_that_is_not_evidence_only_is_refused(throwaway_database_url: str, tmp_path: Path) -> None:
    url = throwaway_database_url
    _seed(url)
    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE core.basin SET basin_group = 'Basins' WHERE basin_id = %s", (BASIN,))
    before = _snapshot(url, BASIN)

    with pytest.raises(ScriptFailedError, match="basin_group=Basins"):
        run_sql_file(DELETE_SQL, database_url=url, copy_dir=tmp_path, apply=True)
    assert _snapshot(url, BASIN) == before


def test_rollback_refuses_to_restore_over_rows_that_still_exist(throwaway_database_url: str, tmp_path: Path) -> None:
    url = throwaway_database_url
    _seed(url)
    run_sql_file(BACKUP_SQL, database_url=url, copy_dir=tmp_path)
    before = _snapshot(url, BASIN)

    with pytest.raises(ScriptFailedError) as raised:
        run_sql_file(ROLLBACK_SQL, database_url=url, copy_dir=tmp_path, apply=True)

    assert raised.value.pgcode == "23505"
    assert _snapshot(url, BASIN) == before
