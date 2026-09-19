from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.backfill_pipeline_job_provenance as provenance_backfill
from packages.common.object_store import LocalObjectStore
from services.orchestrator.pipeline_job_provenance import (
    PROVENANCE_SCHEMA_VERSION,
    PipelineJobProvenanceError,
    import_run_pipeline_job_provenance,
)
from workers.data_adapters.base import cycle_id_for

IFS_RUN_ID = "fcst_ifs_2026091612_dg_9ccb261a39d51c24f4de9173fb4461b6"
IFS_MODEL_ID = "dg_9ccb261a39d51c24f4de9173fb4461b6"
IFS_CYCLE_TIME = datetime(2026, 9, 16, 12, tzinfo=UTC)
IFS_CYCLE_ID = cycle_id_for("IFS", IFS_CYCLE_TIME)
IFS_JOB_ID = "job_fcst_ifs_2026091612_dg_9ccb261a39d51c24f4de9173fb4461b6_forecast_reconciled_49999_23"
IFS_CONVERT_JOB_ID = "job_cycle_ifs_2026091612_convert"
IFS_CONVERT_RUN_ID = "cycle_ifs_2026091612_convert"
IFS_LOG_URI = (
    "published://logs/IFS/2026091612/"
    "fcst_ifs_2026091612_dg_9ccb261a39d51c24f4de9173fb4461b6/"
    "job_fcst_ifs_2026091612_dg_9ccb261a39d51c24f4de9173fb4461b6_forecast_reconciled_49999_23.out"
)
NEWER_UPDATED_AT = "2026-09-16T12:40:00Z"
OLDER_UPDATED_AT = "2026-09-16T12:10:00Z"
HYDRO_STATUS = "published"
HYDRO_UPDATED_AT = "2026-09-16T13:00:00Z"
NEWER_SOURCE_VERSION = int(datetime(2026, 9, 16, 12, 40, tzinfo=UTC).timestamp())
OLDER_SOURCE_VERSION = int(datetime(2026, 9, 16, 12, 10, tzinfo=UTC).timestamp())


class _PsycopgCompatibleCursor:
    """Translate psycopg2-style placeholders so the importer can use SQLite."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> "_PsycopgCompatibleCursor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, _tb: Any) -> None:
        self._cursor.close()

    def execute(self, sql: str, params: Any = None) -> sqlite3.Cursor:
        translated = (
            sql.replace("%s", "?")
            .replace("%(", ":")
            .replace(")s", "")
            .replace("FOR UPDATE", "")
        )
        if "hashtextextended" in translated:
            return self._cursor
        if params is None:
            return self._cursor.execute(translated)
        return self._cursor.execute(translated, params)


    def fetchone(self) -> Any:
        return self._cursor.fetchone()


class _KeepAliveConnection:
    """Wrap sqlite so importer close() does not drop the in-memory schemas."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def cursor(self) -> _PsycopgCompatibleCursor:
        return _PsycopgCompatibleCursor(self._connection.cursor())

    def close(self) -> None:
        return None

    def __enter__(self) -> "_KeepAliveConnection":
        self._connection.execute("BEGIN")
        return self

    def __exit__(self, exc_type: Any, exc: Any, _tb: Any) -> None:
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()


def _new_db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("ATTACH DATABASE ':memory:' AS ops")
    connection.execute("ATTACH DATABASE ':memory:' AS hydro")
    connection.execute(
        """
        CREATE TABLE hydro.hydro_run (
            run_id TEXT PRIMARY KEY,
            source_id TEXT,
            cycle_time TEXT,
            model_id TEXT,
            status TEXT NOT NULL,
            updated_at TEXT
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE ops.pipeline_job (
            job_id TEXT PRIMARY KEY,
            run_id TEXT,
            cycle_id TEXT,
            job_type TEXT NOT NULL,
            slurm_job_id TEXT,
            array_task_id INTEGER,
            model_id TEXT,
            status TEXT NOT NULL,
            stage TEXT,
            submitted_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            exit_code INTEGER,
            retry_count INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            error_message TEXT,
            log_uri TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO hydro.hydro_run (run_id, source_id, cycle_time, model_id, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (IFS_RUN_ID, "IFS", "2026-09-16T12:00:00Z", IFS_MODEL_ID, HYDRO_STATUS, HYDRO_UPDATED_AT),
    )
    connection.commit()
    return connection


def _connect_factory(connection: sqlite3.Connection) -> Any:
    def connect(_database_url: str, *, fallback_application_name: str) -> _KeepAliveConnection:
        del fallback_application_name
        return _KeepAliveConnection(connection)

    return connect


def _seed_object_store(object_root: Path, sidecar: dict[str, Any]) -> None:
    store = LocalObjectStore(root=object_root)
    store.write_bytes_atomic(
        f"runs/{IFS_RUN_ID}/input/manifest.json",
        json.dumps(
            {
                "run_id": IFS_RUN_ID,
                "source_id": "IFS",
                "cycle_time": "2026-09-16T12:00:00Z",
                "model_id": IFS_MODEL_ID,
            },
            sort_keys=True,
        ).encode("utf-8"),
    )
    store.write_bytes_atomic(
        f"runs/{IFS_RUN_ID}/input/pipeline_jobs.json",
        json.dumps(sidecar, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )


def _forecast_job(*, updated_at: str = NEWER_UPDATED_AT, status: str = "succeeded") -> dict[str, Any]:
    return {
        "job_id": IFS_JOB_ID,
        "run_id": IFS_RUN_ID,
        "cycle_id": IFS_CYCLE_ID,
        "job_type": "run_shud_forecast_array",
        "slurm_job_id": "49999",
        "array_task_id": 23,
        "model_id": IFS_MODEL_ID,
        "status": status,
        "stage": "forecast",
        "submitted_at": None,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "retry_count": 0,
        "error_code": None,
        "error_message": None,
        "log_uri": IFS_LOG_URI,
        "created_at": "2026-09-16T12:05:00Z",
        "updated_at": updated_at,
    }


def _convert_job() -> dict[str, Any]:
    return {
        "job_id": IFS_CONVERT_JOB_ID,
        "run_id": IFS_CONVERT_RUN_ID,
        "cycle_id": IFS_CYCLE_ID,
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
        "created_at": "2026-09-16T12:01:00Z",
        "updated_at": OLDER_UPDATED_AT,
    }


def _sidecar(*, jobs: list[dict[str, Any]], source_version: int) -> dict[str, Any]:
    return {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "source_version": source_version,
        "identity": {
            "source": "IFS",
            "cycle_time": "2026-09-16T12:00:00Z",
            "run_id": IFS_RUN_ID,
            "model_id": IFS_MODEL_ID,
        },
        "jobs": jobs,
    }


def _hydro_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute(
        "SELECT run_id, source_id, cycle_time, model_id, status, updated_at FROM hydro.hydro_run WHERE run_id = ?",
        (IFS_RUN_ID,),
    ).fetchone()
    return dict(row)


def _jobs(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT job_id, run_id, model_id, status, log_uri, updated_at
        FROM ops.pipeline_job
        ORDER BY job_id
        """
    ).fetchall()
    return [dict(row) for row in rows]


def test_importer_inserts_jobs_without_changing_already_published_hydro(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    _seed_object_store(
        object_root,
        _sidecar(jobs=[_forecast_job(), _convert_job()], source_version=NEWER_SOURCE_VERSION),
    )
    connection = _new_db()

    result = import_run_pipeline_job_provenance(
        database_url="sqlite://",
        object_store_root=object_root,
        run_id=IFS_RUN_ID,
        connect=_connect_factory(connection),
    )
    jobs = {row["job_id"]: row for row in _jobs(connection)}
    hydro = _hydro_snapshot(connection)

    assert result["status"] == "imported"
    assert result["inserted"] == 2
    assert result["updated"] == 0
    assert result["unchanged"] == 0
    assert hydro["status"] == HYDRO_STATUS
    assert hydro["updated_at"] == HYDRO_UPDATED_AT
    assert jobs[IFS_JOB_ID]["run_id"] == IFS_RUN_ID
    assert jobs[IFS_JOB_ID]["model_id"] == IFS_MODEL_ID
    assert jobs[IFS_JOB_ID]["log_uri"] == IFS_LOG_URI
    assert jobs[IFS_CONVERT_JOB_ID]["run_id"] == IFS_CONVERT_RUN_ID
    assert jobs[IFS_CONVERT_JOB_ID]["model_id"] is None
    connection.close()


def test_importer_identical_replay_is_a_noop(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    sidecar = _sidecar(jobs=[_forecast_job(), _convert_job()], source_version=NEWER_SOURCE_VERSION)
    _seed_object_store(object_root, sidecar)
    connection = _new_db()
    connect = _connect_factory(connection)

    first = import_run_pipeline_job_provenance(
        database_url="sqlite://",
        object_store_root=object_root,
        run_id=IFS_RUN_ID,
        connect=connect,
    )
    after_insert = _jobs(connection)
    hydro_after_insert = _hydro_snapshot(connection)
    second = import_run_pipeline_job_provenance(
        database_url="sqlite://",
        object_store_root=object_root,
        run_id=IFS_RUN_ID,
        connect=connect,
    )

    assert first["inserted"] == 2
    assert second["status"] == "imported"
    assert second["inserted"] == 0
    assert second["updated"] == 0
    assert second["unchanged"] == 2
    assert _jobs(connection) == after_insert
    assert _hydro_snapshot(connection) == hydro_after_insert
    connection.close()


def test_importer_older_snapshot_cannot_roll_back_existing_job(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    connection = _new_db()
    connect = _connect_factory(connection)
    _seed_object_store(
        object_root,
        _sidecar(jobs=[_forecast_job()], source_version=NEWER_SOURCE_VERSION),
    )
    first = import_run_pipeline_job_provenance(
        database_url="sqlite://",
        object_store_root=object_root,
        run_id=IFS_RUN_ID,
        connect=connect,
    )
    older = _forecast_job(updated_at=OLDER_UPDATED_AT, status="running")
    older["log_uri"] = None
    _seed_object_store(object_root, _sidecar(jobs=[older], source_version=OLDER_SOURCE_VERSION))
    second = import_run_pipeline_job_provenance(
        database_url="sqlite://",
        object_store_root=object_root,
        run_id=IFS_RUN_ID,
        connect=connect,
    )
    jobs = _jobs(connection)

    assert OLDER_SOURCE_VERSION < NEWER_SOURCE_VERSION
    assert first["inserted"] == 1
    assert second["status"] == "imported"
    assert second["ignored_stale"] == 1
    assert second["updated"] == 0
    assert jobs == [
        {
            "job_id": IFS_JOB_ID,
            "run_id": IFS_RUN_ID,
            "model_id": IFS_MODEL_ID,
            "status": "succeeded",
            "log_uri": IFS_LOG_URI,
            "updated_at": NEWER_UPDATED_AT,
        }
    ]
    assert _hydro_snapshot(connection)["status"] == HYDRO_STATUS
    connection.close()




def test_importer_uses_each_job_source_timestamp_not_sidecar_batch_max(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    connection = _new_db()
    connect = _connect_factory(connection)
    _seed_object_store(
        object_root,
        _sidecar(jobs=[_forecast_job(), _convert_job()], source_version=NEWER_SOURCE_VERSION),
    )
    import_run_pipeline_job_provenance(
        database_url="sqlite://",
        object_store_root=object_root,
        run_id=IFS_RUN_ID,
        connect=connect,
    )
    fresher_forecast = _forecast_job(updated_at="2026-09-16T13:00:00Z", status="running")
    stale_convert = _convert_job()
    stale_convert["updated_at"] = "2026-09-16T12:05:00Z"
    stale_convert["status"] = "running"
    _seed_object_store(
        object_root,
        _sidecar(
            jobs=[fresher_forecast, stale_convert],
            source_version=int(datetime(2026, 9, 16, 13, tzinfo=UTC).timestamp()),
        ),
    )

    result = import_run_pipeline_job_provenance(
        database_url="sqlite://",
        object_store_root=object_root,
        run_id=IFS_RUN_ID,
        connect=connect,
    )
    rows = {row["job_id"]: row for row in _jobs(connection)}

    assert result["updated"] == 1
    assert result["ignored_stale"] == 1
    assert rows[IFS_JOB_ID]["status"] == "running"
    assert rows[IFS_CONVERT_JOB_ID]["status"] == "succeeded"
    assert rows[IFS_CONVERT_JOB_ID]["updated_at"] == OLDER_UPDATED_AT
    connection.close()


def test_importer_preserves_fractional_timestamp_ordering(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    connection = _new_db()
    connect = _connect_factory(connection)
    newer = _forecast_job(updated_at="2026-09-16T12:40:00.900000Z")
    _seed_object_store(object_root, _sidecar(jobs=[newer], source_version=NEWER_SOURCE_VERSION))
    import_run_pipeline_job_provenance(
        database_url="sqlite://",
        object_store_root=object_root,
        run_id=IFS_RUN_ID,
        connect=connect,
    )
    older = _forecast_job(updated_at="2026-09-16T12:40:00.100000Z", status="running")
    older["log_uri"] = None
    _seed_object_store(object_root, _sidecar(jobs=[older], source_version=NEWER_SOURCE_VERSION))

    result = import_run_pipeline_job_provenance(
        database_url="sqlite://",
        object_store_root=object_root,
        run_id=IFS_RUN_ID,
        connect=connect,
    )
    row = _jobs(connection)[0]

    assert result["ignored_stale"] == 1
    assert row["status"] == "succeeded"
    assert row["updated_at"] == "2026-09-16T12:40:00.900000Z"
    connection.close()


def test_importer_enriches_null_log_at_equal_source_version(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    connection = _new_db()
    connect = _connect_factory(connection)
    without_log = _forecast_job()
    without_log["log_uri"] = None
    _seed_object_store(object_root, _sidecar(jobs=[without_log], source_version=NEWER_SOURCE_VERSION))
    import_run_pipeline_job_provenance(
        database_url="sqlite://",
        object_store_root=object_root,
        run_id=IFS_RUN_ID,
        connect=connect,
    )
    enriched = _forecast_job()
    _seed_object_store(object_root, _sidecar(jobs=[enriched], source_version=NEWER_SOURCE_VERSION))

    result = import_run_pipeline_job_provenance(
        database_url="sqlite://",
        object_store_root=object_root,
        run_id=IFS_RUN_ID,
        connect=connect,
    )

    assert result["updated"] == 1
    assert _jobs(connection)[0]["log_uri"] == IFS_LOG_URI
    connection.close()

def test_importer_identity_conflict_rolls_back_the_run_transaction(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    connection = _new_db()
    connect = _connect_factory(connection)
    _seed_object_store(
        object_root,
        _sidecar(jobs=[_convert_job()], source_version=OLDER_SOURCE_VERSION),
    )
    first = import_run_pipeline_job_provenance(
        database_url="sqlite://",
        object_store_root=object_root,
        run_id=IFS_RUN_ID,
        connect=connect,
    )
    conflicting = {
        **_forecast_job(),
        "job_id": IFS_CONVERT_JOB_ID,
        "run_id": IFS_RUN_ID,
        "model_id": IFS_MODEL_ID,
        "log_uri": None,
    }
    _seed_object_store(
        object_root,
        _sidecar(jobs=[conflicting], source_version=NEWER_SOURCE_VERSION),
    )

    with pytest.raises(PipelineJobProvenanceError) as caught:
        import_run_pipeline_job_provenance(
            database_url="sqlite://",
            object_store_root=object_root,
            run_id=IFS_RUN_ID,
            connect=connect,
        )

    assert first["inserted"] == 1
    assert caught.value.code == "JOB_IDENTITY_CONFLICT"
    assert _jobs(connection) == [
        {
            "job_id": IFS_CONVERT_JOB_ID,
            "run_id": IFS_CONVERT_RUN_ID,
            "model_id": None,
            "status": "succeeded",
            "log_uri": None,
            "updated_at": OLDER_UPDATED_AT,
        }
    ]
    assert _hydro_snapshot(connection)["status"] == HYDRO_STATUS
    assert _hydro_snapshot(connection)["updated_at"] == HYDRO_UPDATED_AT
    connection.close()


def test_importer_rejects_symlink_sidecar(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    outside = tmp_path / "outside"
    object_root.mkdir()
    outside.mkdir()
    _seed_object_store(object_root, _sidecar(jobs=[_forecast_job()], source_version=NEWER_SOURCE_VERSION))
    sidecar_path = object_root / "runs" / IFS_RUN_ID / "input" / "pipeline_jobs.json"
    planted = outside / "pipeline_jobs.json"
    planted.write_bytes(sidecar_path.read_bytes())
    sidecar_path.unlink()
    sidecar_path.symlink_to(planted)

    from packages.common.object_store import ObjectStoreError

    with pytest.raises((PipelineJobProvenanceError, ObjectStoreError)):
        import_run_pipeline_job_provenance(
            database_url="sqlite://",
            object_store_root=object_root,
            run_id=IFS_RUN_ID,
            connect=_connect_factory(_new_db()),
        )


def test_importer_rejects_redacted_log_uri(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    job = _forecast_job()
    job["log_uri"] = "[object-uri]"
    _seed_object_store(object_root, _sidecar(jobs=[job], source_version=NEWER_SOURCE_VERSION))

    with pytest.raises(PipelineJobProvenanceError) as caught:
        import_run_pipeline_job_provenance(
            database_url="sqlite://",
            object_store_root=object_root,
            run_id=IFS_RUN_ID,
            connect=_connect_factory(_new_db()),
        )
    assert caught.value.code == "LOG_URI_INVALID"


def test_importer_rejects_traversal_run_id(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()

    with pytest.raises(PipelineJobProvenanceError) as caught:
        import_run_pipeline_job_provenance(
            database_url="sqlite://",
            object_store_root=object_root,
            run_id="../secret",
            connect=_connect_factory(_new_db()),
        )
    assert caught.value.code == "IDENTITY_INVALID"


def test_backfill_cli_publishes_on_node22_without_database_import(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: dict[str, Any] = {}

    def publish(**kwargs: Any) -> dict[str, Any]:
        calls.update(kwargs)
        return {"status": "published", "runs": [{"run_id": IFS_RUN_ID, "status": "published"}]}

    def import_forbidden(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("node-22 publication must not invoke the importer")

    monkeypatch.setattr(provenance_backfill, "publish_runs_pipeline_job_provenance", publish)
    monkeypatch.setattr(provenance_backfill, "import_runs_pipeline_job_provenance", import_forbidden)

    exit_code = provenance_backfill.main(
        [
            "--publish-only",
            "--run-id",
            IFS_RUN_ID,
            "--journal-root",
            "/journal",
            "--object-store-root",
            "/object-store",
        ]
    )

    assert exit_code == 0
    assert calls["run_ids"] == [IFS_RUN_ID]
    assert calls["journal_root"] == "/journal"
    assert calls["object_store_root"] == "/object-store"
    assert json.loads(capsys.readouterr().out)["publication"]["status"] == "published"


def test_backfill_cli_imports_on_node27_without_source_publication(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: dict[str, Any] = {}

    def publish_forbidden(**_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("node-27 import must not invoke the publisher")

    def import_sidecars(**kwargs: Any) -> dict[str, Any]:
        calls.update(kwargs)
        return {
            "status": "imported",
            "runs": [{"run_id": IFS_RUN_ID, "status": "imported"}],
            "imported": 1,
            "unavailable": 0,
            "failed": 0,
        }

    monkeypatch.setattr(provenance_backfill, "publish_runs_pipeline_job_provenance", publish_forbidden)
    monkeypatch.setattr(provenance_backfill, "import_runs_pipeline_job_provenance", import_sidecars)

    exit_code = provenance_backfill.main(
        [
            "--import-only",
            "--run-id",
            IFS_RUN_ID,
            "--database-url",
            "postgresql://node27-ingest-only",
            "--object-store-root",
            "/object-store",
        ]
    )

    assert exit_code == 0
    assert calls["database_url"] == "postgresql://node27-ingest-only"
    assert calls["run_ids"] == [IFS_RUN_ID]
    assert calls["object_store_root"] == "/object-store"
    assert json.loads(capsys.readouterr().out)["projection"]["status"] == "imported"


def test_backfill_cli_rejects_mixed_publish_and_import_modes() -> None:
    with pytest.raises(SystemExit) as caught:
        provenance_backfill.main(
            [
                "--publish-only",
                "--import-only",
                "--run-id",
                IFS_RUN_ID,
                "--object-store-root",
                "/object-store",
            ]
        )

    assert caught.value.code == 2


def test_backfill_cli_reports_invalid_symlink_journal_root_as_bounded_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_root = tmp_path / "real-journal"
    alias = tmp_path / "alias-journal"
    object_root = tmp_path / "object-store"
    real_root.mkdir()
    object_root.mkdir()
    alias.symlink_to(real_root, target_is_directory=True)

    exit_code = provenance_backfill.main(
        [
            "--publish-only",
            "--run-id",
            IFS_RUN_ID,
            "--journal-root",
            str(alias),
            "--object-store-root",
            str(object_root),
        ]
    )
    captured = capsys.readouterr()
    summary = json.loads(captured.out)

    assert exit_code == 1
    assert summary["publication"]["status"] == "failed"
    assert summary["publication"]["reason"] == "FILE_JOURNAL_INVALID_ROOT"
    assert "Traceback" not in captured.err
    assert str(alias) not in captured.out
    assert str(alias) not in captured.err
    assert "FILE_JOURNAL_INVALID_ROOT:" in captured.err

