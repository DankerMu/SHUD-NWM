"""#2621 basin-catalog / scheduler-manifest audit on a disposable database.

Drives the CLI end to end -- the production ``PsycopgModelRegistryStore``
``list_basins`` (both paths, paged), the active-model count and the forced
read-only session -- over three kinds of seeded basin:

* ``display``: a ready forecast run and an active model, listed in the manifest;
* ``retired``: an inactive model only, absent from the manifest;
* ``live_extra``: an active model but absent from the manifest.

An ``evidence-only`` fixture sits next to them and must stay invisible.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg2
import pytest

from scripts.basin_catalog_manifest_audit import EXIT_PASS, EXIT_VIOLATION, main, read_only_dsn
from tests.integration_helpers import apply_migrations_from_zero, psycopg_connection

pytestmark = pytest.mark.integration

DISPLAY = ("basins_it2621_display_a", "basins_it2621_display_b")
RETIRED = "basins_it2621_retired"
LIVE_EXTRA = "basins_it2621_live_extra"
EVIDENCE = "basin__it2621_evidence"


def _seed_basin(cursor: Any, basin_id: str, *, active: bool, ready_run: bool, group: str = "Basins") -> None:
    bv, rnv, mesh, model = f"{basin_id}_vbasins", f"{basin_id}_rivnet", f"{basin_id}_mesh", f"{basin_id}_shud"
    cursor.execute(
        "INSERT INTO core.basin (basin_id, basin_name, basin_group) VALUES (%s, %s, %s)", (basin_id, basin_id, group)
    )
    cursor.execute(
        "INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom, active_flag) "
        "VALUES (%s, %s, 'basins', ST_GeomFromText('MULTIPOLYGON(((100 30, 101 30, 101 31, 100 30)))', 4490), true)",
        (bv, basin_id),
    )
    cursor.execute(
        "INSERT INTO core.river_network_version (river_network_version_id, basin_version_id, version_label, "
        "segment_count) VALUES (%s, %s, 'basins', 0)",
        (rnv, bv),
    )
    cursor.execute(
        "INSERT INTO core.mesh_version (mesh_version_id, basin_version_id, version_label, mesh_uri) "
        "VALUES (%s, %s, 'basins', 's3://nhms/it2621/mesh')",
        (mesh, bv),
    )
    cursor.execute(
        """
        INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id, mesh_version_id,
                                         calibration_version_id, shud_code_version, model_package_uri, active_flag,
                                         lifecycle_state)
        VALUES (%s, %s, %s, %s, 'calib', 'shud', 's3://nhms/it2621/pkg/', %s, %s)
        """,
        (model, bv, rnv, mesh, active, "active" if active else "inactive"),
    )
    if ready_run:
        cursor.execute(
            """
            INSERT INTO hydro.hydro_run (run_id, run_type, scenario_id, model_id, basin_version_id,
                                         cycle_time, start_time, end_time, status, run_manifest_uri)
            VALUES (%s, 'forecast', 'forecast_gfs_deterministic', %s, %s,
                    '2026-09-28', '2026-09-28', '2026-10-05', 'published', 'integration://manifest.json')
            """,
            (f"{basin_id}_run", model, bv),
        )


def _seed(database_url: str) -> None:
    apply_migrations_from_zero(database_url)
    with psycopg_connection(database_url) as connection, connection.cursor() as cursor:
        for basin_id in DISPLAY:
            _seed_basin(cursor, basin_id, active=True, ready_run=True)
        _seed_basin(cursor, RETIRED, active=False, ready_run=False)
        _seed_basin(cursor, LIVE_EXTRA, active=True, ready_run=False)
        _seed_basin(cursor, EVIDENCE, active=True, ready_run=True, group="evidence-only")


def _manifest(tmp_path: Path, basin_ids: tuple[str, ...]) -> Path:
    path = tmp_path / "manifest-last.json"
    path.write_text(json.dumps({"models": [{"basin_id": basin_id} for basin_id in basin_ids]}), encoding="utf-8")
    return path


def _run(capsys: pytest.CaptureFixture[str], database_url: str, manifest: Path) -> tuple[int, dict[str, Any]]:
    code = main(["--manifest", str(manifest), "--database-url", database_url, "--page-size", "1"])
    return code, json.loads(capsys.readouterr().out)


def test_an_active_basin_outside_the_manifest_fails_then_passes_once_retired(
    throwaway_database_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = throwaway_database_url
    _seed(url)
    manifest = _manifest(tmp_path, DISPLAY)

    code, receipt = _run(capsys, url, manifest)
    assert code == EXIT_VIOLATION
    assert receipt["verdict"] == "violation"
    assert (receipt["default_count"], receipt["display_count"], receipt["manifest_count"]) == (4, 2, 2)
    assert receipt["catalog_extras"] == [
        {"basin_id": LIVE_EXTRA, "active_models": 1},
        {"basin_id": RETIRED, "active_models": 0},
    ]
    assert receipt["violations"] == [{"kind": "active_catalog_extra", "basin_id": LIVE_EXTRA, "active_models": 1}]

    with psycopg_connection(url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core.model_instance SET active_flag = false, lifecycle_state = 'inactive' WHERE model_id = %s",
            (f"{LIVE_EXTRA}_shud",),
        )

    code, receipt = _run(capsys, url, manifest)
    assert code == EXIT_PASS
    assert receipt["verdict"] == "pass"
    assert receipt["violations"] == []
    assert receipt["catalog_extras"] == [
        {"basin_id": LIVE_EXTRA, "active_models": 0},
        {"basin_id": RETIRED, "active_models": 0},
    ]


def test_display_and_manifest_drift_names_both_difference_sets(
    throwaway_database_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = throwaway_database_url
    _seed(url)

    code, receipt = _run(capsys, url, _manifest(tmp_path, (DISPLAY[0], LIVE_EXTRA)))

    assert code == EXIT_VIOLATION
    assert receipt["display_minus_manifest"] == [DISPLAY[1]]
    assert receipt["manifest_minus_display"] == [LIVE_EXTRA]
    assert {"kind": "display_not_in_manifest", "basin_id": DISPLAY[1]} in receipt["violations"]
    assert {"kind": "manifest_not_in_display", "basin_id": LIVE_EXTRA} in receipt["violations"]


def test_the_audit_session_is_read_only(throwaway_database_url: str) -> None:
    url = throwaway_database_url
    _seed(url)
    connection = psycopg2.connect(read_only_dsn(url))
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW transaction_read_only")
            assert cursor.fetchone() == ("on",)
            with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
                cursor.execute("DELETE FROM core.basin WHERE basin_id = %s", (RETIRED,))
    finally:
        connection.close()
