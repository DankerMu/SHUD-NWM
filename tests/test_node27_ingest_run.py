from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scripts.node27_ingest_run import upsert_hydro_run


class RecordingCursor:
    def __init__(self) -> None:
        self.statement = ""
        self.parameters: tuple[Any, ...] = ()

    def execute(self, statement: str, parameters: tuple[Any, ...]) -> None:
        self.statement = statement
        self.parameters = parameters

    def fetchone(self) -> dict[str, str]:
        return {
            "run_id": "fcst_gfs_2026062700_basins_qhh_shud",
            "status": "succeeded",
            "output_uri": "s3://nhms/runs/fcst_gfs_2026062700_basins_qhh_shud/output/",
        }


def _manifest() -> dict[str, Any]:
    return {
        "identity": {
            "run_id": "fcst_gfs_2026062700_basins_qhh_shud",
            "scenario_id": "forecast_gfs_deterministic",
            "model_id": "basins_qhh_shud",
            "basin_version_id": "basins_qhh_vbasins",
            "forcing_version_id": "forc_gfs_2026062700_basins_qhh_shud",
        },
        "forcing": {
            "forcing_version_id": "forc_gfs_2026062700_basins_qhh_shud",
        },
        "initial_state": {
            "state_id": "state_gfs_basins_qhh_shud_2026062700_gfs_2026062612_f012",
        },
        "cycle_time": "2026-06-27T00:00:00Z",
        "start_time": "2026-06-27T00:00:00Z",
        "end_time": "2026-07-04T00:00:00Z",
        "run_type": "forecast",
        "scenario_id": "forecast_gfs_deterministic",
    }


def test_upsert_hydro_run_revives_superseded_cold_start_placeholder() -> None:
    cursor = RecordingCursor()

    result = upsert_hydro_run(cursor, _manifest(), "gfs")

    assert result["status"] == "succeeded"
    assert "WHEN hydro.hydro_run.status = 'superseded' THEN EXCLUDED.status" in cursor.statement
    assert "ELSE hydro.hydro_run.status" in cursor.statement
    assert cursor.parameters[6] == "state_gfs_basins_qhh_shud_2026062700_gfs_2026062612_f012"


def test_register_upsert_never_writes_parsed_at() -> None:
    """#1789: the register must not touch ``hydro_run.parsed_at``.

    Same shape as the existing ``updated_at`` reasoning, inverted. This
    statement runs for EVERY run on EVERY tick and deliberately bumps
    ``updated_at``; that is exactly why ``updated_at`` cannot serve as a parse
    timestamp. ``parsed_at`` is read by the autopipe completeness criterion as
    "when did a parse last succeed", so a register bump would freeze recompute
    detection into always claiming the ingested data is current.
    """
    cursor = RecordingCursor()

    upsert_hydro_run(cursor, _manifest(), "gfs")

    assert "parsed_at" not in cursor.statement


@pytest.mark.integration
def test_register_upsert_renews_updated_at_of_a_failed_run_without_touching_its_failure(
    throwaway_database_url: str,
) -> None:
    """#2529 renewal pin (real PostgreSQL). The residency lane's retry-liveness
    bound (``updated_at > now() - 6 h``) keeps a failed run in its watched set
    ONLY because this register upsert — the first step of every autopipe
    ``_process_run`` — bumps ``updated_at`` on an existing ``failed`` row while
    keeping its status. The parser does not renew it: ``mark_run_failed`` is a
    no-op on an already-failed run (``FAILABLE_RUN_STATUSES`` excludes
    ``failed``), so ``error_code`` stays the FIRST failure's code — which is why
    the observer reports it as ``first_error_code``.
    """
    import psycopg2
    from psycopg2.extras import RealDictCursor

    from scripts import node27_parse_failure_residency_alert as alert
    from scripts.node27_ingest_run import upsert_data_source, upsert_forcing_version
    from tests.integration_helpers import apply_migrations_from_zero
    from workers.output_parser.parser import PsycopgOutputParserRepository

    apply_migrations_from_zero(throwaway_database_url)
    run_id = "fcst_gfs_2026062700_basins_r2529_shud"
    manifest = {
        "identity": {
            "run_id": run_id,
            "scenario_id": "forecast_gfs_deterministic",
            "model_id": "m-r2529",
            "basin_version_id": "bv-r2529",
            "forcing_version_id": "forc-r2529",
            "source_id": "gfs",
        },
        "forcing": {"forcing_version_id": "forc-r2529", "forcing_package_uri": "s3://nhms/forcing/r2529/"},
        "cycle_time": "2026-06-27T00:00:00Z",
        "start_time": "2026-06-27T00:00:00Z",
        "end_time": "2026-07-04T00:00:00Z",
        "run_type": "forecast",
    }

    def register() -> dict[str, Any]:
        connection = psycopg2.connect(throwaway_database_url, cursor_factory=RealDictCursor)
        try:
            with connection, connection.cursor() as cursor:
                upsert_data_source(cursor, "gfs")
                upsert_forcing_version(cursor, manifest, "gfs")
                return upsert_hydro_run(cursor, manifest, "gfs")
        finally:
            connection.close()

    def row() -> tuple[str, str | None, bool]:
        connection = psycopg2.connect(throwaway_database_url)
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(
                    "SELECT status::text, error_code, updated_at > now() - interval '1 minute' "
                    "FROM hydro.hydro_run WHERE run_id = %s",
                    (run_id,),
                )
                return cursor.fetchone()
        finally:
            connection.close()

    connection = psycopg2.connect(throwaway_database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO core.basin (basin_id, basin_name) VALUES ('b-r2529', 'b')")
            cursor.execute(
                "INSERT INTO core.basin_version (basin_version_id, basin_id, version_label, geom, active_flag) "
                "VALUES ('bv-r2529', 'b-r2529', 'v1', ST_SetSRID(ST_GeomFromText("
                "'MULTIPOLYGON(((99 37, 99 39, 101 39, 101 37, 99 37)))'), 4490), true)"
            )
            cursor.execute(
                "INSERT INTO core.river_network_version (river_network_version_id, basin_version_id, "
                "version_label, segment_count) VALUES ('rnv-r2529', 'bv-r2529', 'v1', 1)"
            )
            cursor.execute(
                "INSERT INTO core.model_instance (model_id, basin_version_id, river_network_version_id, "
                "mesh_version_id, calibration_version_id, shud_code_version, model_package_uri, active_flag, "
                "lifecycle_state) "
                "VALUES ('m-r2529', 'bv-r2529', 'rnv-r2529', 'mesh', 'cal', '1.0', 's3://nhms/m', true, 'active')"
            )
    finally:
        connection.close()

    assert register()["status"] == "succeeded"
    parser_repository = PsycopgOutputParserRepository(throwaway_database_url)
    parser_repository.mark_run_failed(run_id, "MODEL_RIVER_FILE_MALFORMED", "first failure")

    def backdate() -> None:
        connection = psycopg2.connect(throwaway_database_url)
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE hydro.hydro_run SET updated_at = now() - interval '5 hours' WHERE run_id = %s",
                    (run_id,),
                )
        finally:
            connection.close()

    # A retry's parse failure does not renew the row: no-op on `failed`.
    backdate()
    parser_repository.mark_run_failed(run_id, "OUTPUT_PARSE_DB_ERROR", "a later failure")
    assert row() == ("failed", "MODEL_RIVER_FILE_MALFORMED", False)

    # The register upsert does: status and first code kept, updated_at now().
    assert register()["status"] == "failed"
    assert row() == ("failed", "MODEL_RIVER_FILE_MALFORMED", True)

    # ...which is what keeps the run inside the observer's liveness bound.
    config = alert.config_from_env({"DATABASE_URL": throwaway_database_url})
    floor = datetime.now(UTC) - timedelta(hours=alert.DEFAULT_RETRY_LIVENESS_HOURS)
    assert [(r.run_id, r.first_error_code) for r in alert.default_observe(config, floor)] == [
        (run_id, "MODEL_RIVER_FILE_MALFORMED")
    ]
