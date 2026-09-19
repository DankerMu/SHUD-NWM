"""Real-PostgreSQL seam for published pipeline-job provenance projection.

Run only against the repository's disposable integration database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_pipeline_job_provenance_importer_integration.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from packages.common.object_store import LocalObjectStore
from services.orchestrator.pipeline_job_provenance import (
    PROVENANCE_SCHEMA_VERSION,
    import_run_pipeline_job_provenance,
)
from tests.integration_helpers import (
    BASIN_VERSION_ID,
    CYCLE_ID,
    CYCLE_TIME,
    FORCING_VERSION_ID,
    MODEL_ID,
    SOURCE_ID,
    apply_migrations_from_zero,
    psycopg_connection,
    seed_issue_126_data,
)

pytestmark = pytest.mark.integration

RUN_ID = f"fcst_gfs_{CYCLE_TIME.strftime('%Y%m%d%H')}_{MODEL_ID}"
JOB_ID = "it2420_forecast_50025_0"
UPDATED_AT = "2026-05-03T00:40:00.900000Z"


def _sidecar() -> dict[str, Any]:
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "source_version": int(datetime(2026, 5, 3, 0, 40, tzinfo=UTC).timestamp()),
        "identity": {
            "source": "GFS",
            "cycle_time": CYCLE_TIME.isoformat().replace("+00:00", "Z"),
            "run_id": RUN_ID,
            "model_id": MODEL_ID,
        },
        "jobs": [
            {
                "job_id": JOB_ID,
                "run_id": RUN_ID,
                "cycle_id": CYCLE_ID,
                "job_type": "run_shud_forecast_array",
                "slurm_job_id": "50025",
                "array_task_id": 0,
                "model_id": MODEL_ID,
                "status": "succeeded",
                "stage": "forecast",
                "submitted_at": None,
                "started_at": None,
                "finished_at": None,
                "exit_code": None,
                "retry_count": 0,
                "error_code": None,
                "error_message": None,
                "log_uri": None,
                "log_truncated": None,
                "log_stdout_bytes": None,
                "log_stderr_bytes": None,
                "created_at": "2026-05-03T00:05:00Z",
                "updated_at": UPDATED_AT,
            }
        ],
    }


def _seed_object_store(root: Path) -> None:
    store = LocalObjectStore(root=root)
    identity = _sidecar()["identity"]
    store.write_bytes_atomic(
        f"runs/{RUN_ID}/input/manifest.json",
        json.dumps(
            {
                "run_id": RUN_ID,
                "source_id": identity["source"],
                "cycle_time": identity["cycle_time"],
                "model_id": MODEL_ID,
            },
            sort_keys=True,
        ).encode("utf-8"),
    )
    store.write_bytes_atomic(
        f"runs/{RUN_ID}/input/pipeline_jobs.json",
        json.dumps(_sidecar(), sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def _seed_published_hydro_run(database_url: str) -> dict[str, Any]:
    with psycopg_connection(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO hydro.hydro_run (
                    run_id, run_type, scenario_id, model_id, basin_version_id, forcing_version_id,
                    source_id, cycle_time, start_time, end_time, status, run_manifest_uri, output_uri
                ) VALUES (%s, 'forecast', 'issue2420', %s, %s, %s, %s, %s, %s, %s, 'published', %s, %s)
                """,
                (
                    RUN_ID,
                    MODEL_ID,
                    BASIN_VERSION_ID,
                    FORCING_VERSION_ID,
                    SOURCE_ID,
                    CYCLE_TIME,
                    CYCLE_TIME,
                    CYCLE_TIME + timedelta(hours=1),
                    f"s3://nhms/runs/{RUN_ID}/input/manifest.json",
                    f"s3://nhms/runs/{RUN_ID}/output/",
                ),
            )
            cursor.execute(
                "SELECT status::text AS status, updated_at FROM hydro.hydro_run WHERE run_id = %s",
                (RUN_ID,),
            )
            row = cursor.fetchone()
    assert row is not None
    return dict(row)


def _provision_pipeline_job_roles(database_url: str) -> None:
    """Create disposable ingest/display roles and grant only this database.

    Roles are cluster-wide in PostgreSQL; this is additive and never DROPs, so a
    pre-existing cluster role is left intact. Grants stay on the throwaway
    database created around this test.
    """

    with psycopg_connection(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DO $roles$
                DECLARE
                  v_role text;
                BEGIN
                  FOREACH v_role IN ARRAY ARRAY['nhms_ingest_rw', 'nhms_display_ro'] LOOP
                    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_role) THEN
                      EXECUTE format(
                        'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
                        v_role
                      );
                    END IF;
                  END LOOP;
                END
                $roles$;
                """
            )
            cursor.execute("GRANT USAGE ON SCHEMA hydro, ops TO nhms_ingest_rw, nhms_display_ro")
            cursor.execute("GRANT SELECT ON hydro.hydro_run TO nhms_ingest_rw, nhms_display_ro")
            cursor.execute(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ops.pipeline_job TO nhms_ingest_rw"
            )
            cursor.execute("GRANT SELECT ON ops.pipeline_job TO nhms_display_ro")
            cursor.execute("REVOKE INSERT, UPDATE, DELETE ON ops.pipeline_job FROM nhms_display_ro")


def _connect_as_role(role: str) -> Any:
    def connect(database_url: str, *, fallback_application_name: str) -> Any:
        connection = psycopg2.connect(
            database_url,
            cursor_factory=RealDictCursor,
            fallback_application_name=fallback_application_name,
        )
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('role', %s, false)", (role,))
        connection.autocommit = False
        return connection

    return connect


def test_importer_projects_real_postgres_rows_without_changing_published_hydro(
    throwaway_database_url: str,
    tmp_path: Path,
) -> None:
    apply_migrations_from_zero(throwaway_database_url)
    _provision_pipeline_job_roles(throwaway_database_url)
    seed_issue_126_data(throwaway_database_url)
    before_hydro = _seed_published_hydro_run(throwaway_database_url)
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    _seed_object_store(object_root)
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        import_run_pipeline_job_provenance(
            database_url=throwaway_database_url,
            object_store_root=object_root,
            run_id=RUN_ID,
            connect=_connect_as_role("nhms_display_ro"),
        )

    first = import_run_pipeline_job_provenance(
        database_url=throwaway_database_url,
        object_store_root=object_root,
        run_id=RUN_ID,
        connect=_connect_as_role("nhms_ingest_rw"),
    )
    second = import_run_pipeline_job_provenance(
        database_url=throwaway_database_url,
        object_store_root=object_root,
        run_id=RUN_ID,
        connect=_connect_as_role("nhms_ingest_rw"),
    )
    with psycopg_connection(throwaway_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT run_id, model_id, status::text AS status, updated_at FROM ops.pipeline_job WHERE job_id = %s",
                (JOB_ID,),
            )
            projected = cursor.fetchone()
            cursor.execute(
                "SELECT status::text AS status, updated_at FROM hydro.hydro_run WHERE run_id = %s",
                (RUN_ID,),
            )
            after_hydro = cursor.fetchone()

    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["updated"] == 0
    assert second["unchanged"] == 1
    assert dict(projected) == {
        "run_id": RUN_ID,
        "model_id": MODEL_ID,
        "status": "succeeded",
        "updated_at": datetime(2026, 5, 3, 0, 40, 0, 900000, tzinfo=UTC),
    }
    assert dict(after_hydro) == before_hydro


