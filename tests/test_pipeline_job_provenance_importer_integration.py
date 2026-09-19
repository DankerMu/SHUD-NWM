"""Real-PostgreSQL seam for published pipeline-job provenance projection.

Run only against the repository's disposable integration database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_pipeline_job_provenance_importer_integration.py
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from packages.common.object_store import LocalObjectStore
from services.orchestrator.pipeline_job_provenance import (
    PROVENANCE_SCHEMA_VERSION,
    PipelineJobProvenanceError,
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
CONVERT_JOB_ID = "it2420_cycle_convert"
UPDATED_AT = "2026-05-03T00:40:00.900000Z"
OLDER_UPDATED_AT = "2026-05-03T00:10:00Z"
NEWER_UPDATED_AT = "2026-05-03T00:50:00.900000Z"



def _sidecar(*, jobs: list[dict[str, Any]] | None = None, source_version: int | None = None) -> dict[str, Any]:
    payload_jobs = jobs if jobs is not None else [_forecast_job()]
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "source_version": source_version
        if source_version is not None
        else int(datetime(2026, 5, 3, 0, 40, tzinfo=UTC).timestamp()),
        "identity": {
            "source": "GFS",
            "cycle_time": CYCLE_TIME.isoformat().replace("+00:00", "Z"),
            "run_id": RUN_ID,
            "model_id": MODEL_ID,
        },
        "jobs": payload_jobs,
    }


def _forecast_job(
    *,
    updated_at: str = UPDATED_AT,
    status: str = "succeeded",
    log_uri: str | None = None,
) -> dict[str, Any]:

    return {
        "job_id": JOB_ID,
        "run_id": RUN_ID,
        "cycle_id": CYCLE_ID,
        "job_type": "run_shud_forecast_array",
        "slurm_job_id": "50025",
        "array_task_id": 0,
        "model_id": MODEL_ID,
        "status": status,
        "stage": "forecast",
        "submitted_at": None,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "retry_count": 0,
        "error_code": None,
        "error_message": None,
        "log_uri": log_uri,
        "log_truncated": None,
        "log_stdout_bytes": None,
        "log_stderr_bytes": None,
        "created_at": "2026-05-03T00:05:00Z",
        "updated_at": updated_at,
    }


def _convert_job() -> dict[str, Any]:
    return {
        "job_id": CONVERT_JOB_ID,
        "run_id": f"cycle_gfs_{CYCLE_TIME.strftime('%Y%m%d%H')}_convert",
        "cycle_id": CYCLE_ID,
        "job_type": "convert_source_cycle",
        "slurm_job_id": "49800",
        "array_task_id": None,
        "model_id": None,
        "status": "succeeded",
        "stage": "convert",
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
        "created_at": "2026-05-03T00:01:00Z",
        "updated_at": OLDER_UPDATED_AT,
    }




def _seed_object_store(root: Path, sidecar: dict[str, Any] | None = None) -> None:
    store = LocalObjectStore(root=root)
    payload = sidecar or _sidecar()
    identity = payload["identity"]
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
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
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



def _fetch_job(database_url: str, job_id: str) -> dict[str, Any] | None:
    with psycopg_connection(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT job_id, run_id, model_id, status::text AS status, log_uri, updated_at
                FROM ops.pipeline_job
                WHERE job_id = %s
                """,
                (job_id,),
            )
            row = cursor.fetchone()
    return dict(row) if row is not None else None


def test_importer_real_postgres_update_stale_conflict_enrichment_and_rollback(
    throwaway_database_url: str,
    tmp_path: Path,
) -> None:
    apply_migrations_from_zero(throwaway_database_url)
    _provision_pipeline_job_roles(throwaway_database_url)
    seed_issue_126_data(throwaway_database_url)
    before_hydro = _seed_published_hydro_run(throwaway_database_url)
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    connect = _connect_as_role("nhms_ingest_rw")
    log_uri = (
        f"published://logs/GFS/{CYCLE_TIME.strftime('%Y%m%d%H')}/{RUN_ID}/{JOB_ID}.out"
    )

    _seed_object_store(object_root, _sidecar(jobs=[_forecast_job(), _convert_job()]))
    first = import_run_pipeline_job_provenance(
        database_url=throwaway_database_url,
        object_store_root=object_root,
        run_id=RUN_ID,
        connect=connect,
    )
    assert first["inserted"] == 2

    _seed_object_store(
        object_root,
        _sidecar(
            jobs=[_forecast_job(updated_at=OLDER_UPDATED_AT, status="running"), _convert_job()],
            source_version=int(datetime(2026, 5, 3, 0, 10, tzinfo=UTC).timestamp()),
        ),
    )
    stale = import_run_pipeline_job_provenance(
        database_url=throwaway_database_url,
        object_store_root=object_root,
        run_id=RUN_ID,
        connect=connect,
    )
    assert stale["ignored_stale"] == 1
    assert _fetch_job(throwaway_database_url, JOB_ID)["status"] == "succeeded"

    _seed_object_store(
        object_root,
        _sidecar(jobs=[_forecast_job(log_uri=log_uri), _convert_job()]),
    )
    enriched = import_run_pipeline_job_provenance(
        database_url=throwaway_database_url,
        object_store_root=object_root,
        run_id=RUN_ID,
        connect=connect,
    )
    assert enriched["updated"] == 1
    assert _fetch_job(throwaway_database_url, JOB_ID)["log_uri"] == log_uri

    _seed_object_store(
        object_root,
        _sidecar(
            jobs=[_forecast_job(updated_at=NEWER_UPDATED_AT, status="failed", log_uri=log_uri), _convert_job()],
            source_version=int(datetime(2026, 5, 3, 0, 50, tzinfo=UTC).timestamp()),
        ),
    )
    updated = import_run_pipeline_job_provenance(
        database_url=throwaway_database_url,
        object_store_root=object_root,
        run_id=RUN_ID,
        connect=connect,
    )
    assert updated["updated"] == 1
    assert _fetch_job(throwaway_database_url, JOB_ID)["status"] == "failed"

    conflicting = {
        **_forecast_job(updated_at=NEWER_UPDATED_AT, log_uri=None),
        "job_id": CONVERT_JOB_ID,
        "run_id": RUN_ID,
        "model_id": MODEL_ID,
    }
    _seed_object_store(
        object_root,
        _sidecar(
            jobs=[_forecast_job(updated_at=NEWER_UPDATED_AT, status="running", log_uri=log_uri), conflicting],
            source_version=int(datetime(2026, 5, 3, 0, 55, tzinfo=UTC).timestamp()),
        ),
    )
    with pytest.raises(PipelineJobProvenanceError) as caught:
        import_run_pipeline_job_provenance(
            database_url=throwaway_database_url,
            object_store_root=object_root,
            run_id=RUN_ID,
            connect=connect,
        )
    assert caught.value.code == "JOB_IDENTITY_CONFLICT"
    assert _fetch_job(throwaway_database_url, JOB_ID)["status"] == "failed"
    assert _fetch_job(throwaway_database_url, CONVERT_JOB_ID)["run_id"].startswith("cycle_")

    with psycopg_connection(throwaway_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status::text AS status, updated_at FROM hydro.hydro_run WHERE run_id = %s",
                (RUN_ID,),
            )
            after_hydro = cursor.fetchone()
    assert dict(after_hydro) == before_hydro


def test_importer_real_postgres_overlapping_stale_import_keeps_newer_row(
    throwaway_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    apply_migrations_from_zero(throwaway_database_url)
    _provision_pipeline_job_roles(throwaway_database_url)
    seed_issue_126_data(throwaway_database_url)
    _seed_published_hydro_run(throwaway_database_url)
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    connect = _connect_as_role("nhms_ingest_rw")
    _seed_object_store(object_root, _sidecar(jobs=[_forecast_job()]))
    import_run_pipeline_job_provenance(
        database_url=throwaway_database_url,
        object_store_root=object_root,
        run_id=RUN_ID,
        connect=connect,
    )

    barrier = threading.Barrier(2)
    original_fetch = __import__(
        "services.orchestrator.pipeline_job_provenance",
        fromlist=["_fetch_existing_job"],
    )._fetch_existing_job

    def gated_fetch(cursor: Any, job_id: str) -> dict[str, Any] | None:
        row = original_fetch(cursor, job_id)
        barrier.wait(timeout=5)
        return row

    monkeypatch.setattr(
        "services.orchestrator.pipeline_job_provenance._fetch_existing_job",
        gated_fetch,
    )
    errors: list[str] = []

    def import_stale() -> None:
        stale_root = tmp_path / "stale"
        stale_root.mkdir()
        _seed_object_store(
            stale_root,
            _sidecar(
                jobs=[_forecast_job(updated_at=OLDER_UPDATED_AT, status="running")],
                source_version=int(datetime(2026, 5, 3, 0, 10, tzinfo=UTC).timestamp()),
            ),
        )
        try:
            import_run_pipeline_job_provenance(
                database_url=throwaway_database_url,
                object_store_root=stale_root,
                run_id=RUN_ID,
                connect=connect,
            )
        except Exception as error:  # noqa: BLE001 - capture concurrent failure
            errors.append(type(error).__name__)

    def import_newer() -> None:
        newer_root = tmp_path / "newer"
        newer_root.mkdir()
        _seed_object_store(
            newer_root,
            _sidecar(
                jobs=[_forecast_job(updated_at=NEWER_UPDATED_AT, status="failed")],
                source_version=int(datetime(2026, 5, 3, 0, 50, tzinfo=UTC).timestamp()),
            ),
        )
        try:
            import_run_pipeline_job_provenance(
                database_url=throwaway_database_url,
                object_store_root=newer_root,
                run_id=RUN_ID,
                connect=connect,
            )
        except Exception as error:  # noqa: BLE001 - capture concurrent failure
            errors.append(type(error).__name__)

    stale_thread = threading.Thread(target=import_stale)
    newer_thread = threading.Thread(target=import_newer)
    stale_thread.start()
    newer_thread.start()
    stale_thread.join(timeout=10)
    newer_thread.join(timeout=10)

    final = _fetch_job(throwaway_database_url, JOB_ID)
    assert final is not None
    assert final["status"] == "failed"
    assert final["updated_at"] == datetime(2026, 5, 3, 0, 50, 0, 900000, tzinfo=UTC)



