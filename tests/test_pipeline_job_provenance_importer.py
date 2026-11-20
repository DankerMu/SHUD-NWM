from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import scripts.backfill_pipeline_job_provenance as provenance_backfill
from packages.common.contracts.time_normalization import cycle_id_for
from packages.common.object_store import LocalObjectStore
from scripts import node27_autopipeline
from services.orchestrator import pipeline_job_provenance
from services.orchestrator.pipeline_job_provenance import (
    PROVENANCE_SCHEMA_VERSION,
    PipelineJobProvenanceError,
    import_discovered_pipeline_job_provenance,
    import_run_pipeline_job_provenance,
)

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

    def __init__(
        self,
        cursor: sqlite3.Cursor,
        *,
        executed: list[str] | None = None,
        fail: Callable[[str], bool] | None = None,
    ) -> None:
        self._cursor = cursor
        self._executed = executed
        self._fail = fail

    def __enter__(self) -> "_PsycopgCompatibleCursor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, _tb: Any) -> None:
        self._cursor.close()

    def execute(self, sql: str, params: Any = None) -> sqlite3.Cursor:
        if self._executed is not None:
            self._executed.append(sql)
        if self._fail is not None and self._fail(sql):
            raise sqlite3.OperationalError("injected statement failure")
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

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        executed: list[str] | None = None,
        fail: Callable[[str], bool] | None = None,
    ) -> None:
        self._connection = connection
        self._executed = executed
        self._fail = fail

    def cursor(self) -> _PsycopgCompatibleCursor:
        return _PsycopgCompatibleCursor(self._connection.cursor(), executed=self._executed, fail=self._fail)

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


def _connect_factory(
    connection: sqlite3.Connection,
    *,
    executed: list[str] | None = None,
    fail: Callable[[str], bool] | None = None,
) -> Any:
    def connect(_database_url: str, **kwargs: Any) -> _KeepAliveConnection:
        del kwargs
        return _KeepAliveConnection(connection, executed=executed, fail=fail)

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


def test_backfill_cli_object_store_root_reads_only_object_store_root(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # #3146: the NHMS_OBJECT_STORE_ROOT fallback is retired; OBJECT_STORE_ROOT is the one name.
    monkeypatch.delenv("OBJECT_STORE_ROOT", raising=False)
    monkeypatch.setenv("NHMS_OBJECT_STORE_ROOT", "/alias-object-store")

    argv = ["--import-only", "--run-id", IFS_RUN_ID]
    assert provenance_backfill.build_parser().parse_args(argv).object_store_root is None
    with pytest.raises(SystemExit) as caught:
        provenance_backfill.main(argv)
    assert caught.value.code == 2
    assert "OBJECT_STORE_ROOT or --object-store-root is required" in capsys.readouterr().err

    monkeypatch.setenv("OBJECT_STORE_ROOT", "/object-store")
    assert provenance_backfill.build_parser().parse_args(argv).object_store_root == "/object-store"


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



def test_importer_component_connector_keeps_mapping_rows_writer_dsn_and_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psycopg2
    import psycopg2.extensions
    from psycopg2.extras import RealDictCursor

    object_root = tmp_path / "object-store"
    object_root.mkdir()
    _seed_object_store(
        object_root,
        _sidecar(jobs=[_forecast_job(), _convert_job()], source_version=NEWER_SOURCE_VERSION),
    )
    connection = _new_db()
    dsn = "postgresql://writer:secret@127.0.0.1:55432/nhms?application_name=operator-override"
    captured: dict[str, Any] = {}

    def fake_connect(database_url: str, **kwargs: Any) -> _KeepAliveConnection:
        captured["database_url"] = database_url
        captured["kwargs"] = dict(kwargs)
        return _KeepAliveConnection(connection)

    monkeypatch.setattr(psycopg2, "connect", fake_connect)

    result = import_run_pipeline_job_provenance(
        database_url=dsn,
        object_store_root=object_root,
        run_id=IFS_RUN_ID,
        connect=node27_autopipeline._attributed_connect,
    )

    assert result["status"] == "imported"
    assert result["inserted"] == 2
    assert captured["database_url"] == dsn
    assert captured["kwargs"]["fallback_application_name"] == node27_autopipeline._APPLICATION_NAME
    assert captured["kwargs"]["cursor_factory"] is RealDictCursor
    assert captured["kwargs"]["connect_timeout"] == 10
    assert captured["kwargs"]["options"] == "-c statement_timeout=600000"
    jobs = {row["job_id"]: row for row in _jobs(connection)}
    assert jobs[IFS_JOB_ID]["status"] == "succeeded"
    assert jobs[IFS_CONVERT_JOB_ID]["model_id"] is None
    conninfo = psycopg2.extensions.make_dsn(
        captured["database_url"],
        fallback_application_name=captured["kwargs"]["fallback_application_name"],
    )
    parsed = psycopg2.extensions.parse_dsn(conninfo)
    assert parsed["application_name"] == "operator-override"
    assert parsed["fallback_application_name"] == "nhms-autopipe"
    connection.close()


# ---------------------------------------------------------------------------
# #3224: the discovered pass skips a run whose sidecar bytes and projected rows
# are unchanged since the import that wrote its marker. Expected values come
# from the fixture (`openspec/changes/cheapen-idle-autopipe-tick/design.md`):
# the state-file name and schema, the three-part skip predicate and the marker
# lifecycle -- never from the implementation's own constants.
# ---------------------------------------------------------------------------

STATE_FILE_NAME = "pipeline-job-provenance-import-state.json"
STATE_SCHEMA = "nhms.node27.pipeline_job_provenance_import_state.v1"
RUN_B_MODEL_ID = "dg_0123456789abcdef0123456789abcdef"
RUN_B_ID = f"fcst_ifs_2026091612_{RUN_B_MODEL_ID}"
RUN_B_JOB_ID = f"job_{RUN_B_ID}_forecast_reconciled_49999_7"
LATER_UPDATED_AT = "2026-09-16T13:00:00Z"
LATER_SOURCE_VERSION = int(datetime(2026, 9, 16, 13, tzinfo=UTC).timestamp())


def _two_job_sidecar() -> dict[str, Any]:
    return _sidecar(jobs=[_forecast_job(), _convert_job()], source_version=NEWER_SOURCE_VERSION)


def _sidecar_key(run_id: str) -> str:
    return f"runs/{run_id}/input/pipeline_jobs.json"


def _sidecar_path(object_root: Path, run_id: str) -> Path:
    return object_root / _sidecar_key(run_id)


def _sidecar_bytes(sidecar: dict[str, Any]) -> bytes:
    return json.dumps(sidecar, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _run_b_sidecar(*, jobs: list[dict[str, Any]], source_version: int = NEWER_SOURCE_VERSION) -> dict[str, Any]:
    sidecar = _sidecar(jobs=jobs, source_version=source_version)
    sidecar["identity"] = {**sidecar["identity"], "run_id": RUN_B_ID, "model_id": RUN_B_MODEL_ID}
    return sidecar


def _run_b_forecast_job(*, status: str = "succeeded", updated_at: str = NEWER_UPDATED_AT) -> dict[str, Any]:
    return {
        **_forecast_job(status=status, updated_at=updated_at),
        "job_id": RUN_B_JOB_ID,
        "run_id": RUN_B_ID,
        "model_id": RUN_B_MODEL_ID,
        "array_task_id": 7,
        "log_uri": None,
    }


def _newer_convert_job() -> dict[str, Any]:
    return {**_convert_job(), "status": "failed", "updated_at": NEWER_UPDATED_AT}


def _add_run_b(connection: sqlite3.Connection, object_root: Path, sidecar: dict[str, Any] | None) -> None:
    connection.execute(
        """
        INSERT INTO hydro.hydro_run (run_id, source_id, cycle_time, model_id, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (RUN_B_ID, "IFS", "2026-09-16T12:00:00Z", RUN_B_MODEL_ID, HYDRO_STATUS, HYDRO_UPDATED_AT),
    )
    connection.commit()
    store = LocalObjectStore(root=object_root)
    store.write_bytes_atomic(
        f"runs/{RUN_B_ID}/input/manifest.json",
        json.dumps(
            {
                "run_id": RUN_B_ID,
                "source_id": "IFS",
                "cycle_time": "2026-09-16T12:00:00Z",
                "model_id": RUN_B_MODEL_ID,
            },
            sort_keys=True,
        ).encode("utf-8"),
    )
    if sidecar is not None:
        store.write_bytes_atomic(_sidecar_key(RUN_B_ID), _sidecar_bytes(sidecar))


def _publish(object_root: Path, run_id: str, sidecar: dict[str, Any]) -> bytes:
    payload = _sidecar_bytes(sidecar)
    LocalObjectStore(root=object_root).write_bytes_atomic(_sidecar_key(run_id), payload)
    return payload


def _pass(
    connection: sqlite3.Connection,
    object_root: Path,
    run_ids: list[str],
    state_path: Path | None,
    *,
    executed: list[str] | None = None,
    fail: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {} if state_path is None else {"state_path": state_path}
    return import_discovered_pipeline_job_provenance(
        database_url="sqlite://",
        object_store_root=object_root,
        run_ids=run_ids,
        connect=_connect_factory(connection, executed=executed, fail=fail),
        **kwargs,
    )


def _state(state_path: Path) -> dict[str, Any]:
    return json.loads(state_path.read_text(encoding="utf-8"))


def _by_run(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["run_id"]: item for item in summary["runs"]}


def _presence_reads(executed: list[str]) -> list[str]:
    return [sql for sql in executed if "ops.pipeline_job" in sql and "job_id IN" in " ".join(sql.split())]


def _row(connection: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM ops.pipeline_job WHERE job_id = ?", (job_id,)).fetchone()
    return None if row is None else dict(row)


@pytest.fixture
def stateful(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, Path, Path]]:
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    work_root = tmp_path / "autopipe-work"
    work_root.mkdir()
    connection = _new_db()
    yield connection, object_root, work_root / STATE_FILE_NAME
    connection.close()


def test_discovered_pass_skips_a_run_whose_sidecar_and_rows_are_unchanged(
    stateful: tuple[sqlite3.Connection, Path, Path],
) -> None:
    connection, object_root, state_path = stateful
    _seed_object_store(object_root, _two_job_sidecar())

    first = _pass(connection, object_root, [IFS_RUN_ID], state_path)
    rows_after_first = _jobs(connection)
    executed: list[str] = []
    second = _pass(connection, object_root, [IFS_RUN_ID], state_path, executed=executed)

    assert first["imported"] == 1
    assert first["skipped_unchanged"] == 0
    assert first["skip_state"] == "empty"
    assert first["skip_state_saved"] is True
    assert _by_run(first)[IFS_RUN_ID]["inserted"] == 2
    marker = _state(state_path)["runs"][IFS_RUN_ID]
    assert marker["sidecar_sha256"] == hashlib.sha256(_sidecar_path(object_root, IFS_RUN_ID).read_bytes()).hexdigest()
    assert set(marker["jobs"]) == {IFS_JOB_ID, IFS_CONVERT_JOB_ID}

    assert second["status"] == "imported"
    assert second["runs"] == []
    assert second["skipped_unchanged"] == 1
    assert (second["imported"], second["unavailable"], second["failed"]) == (0, 0, 0)
    assert second["skip_state"] == "loaded"
    assert len(_presence_reads(executed)) == 1
    assert len(executed) == 1, executed
    for statement in executed:
        assert "hydro.hydro_run" not in statement
        assert "FOR UPDATE" not in statement
        assert "hashtextextended" not in statement
        assert "INSERT" not in statement.upper()
        assert "UPDATE ops" not in statement
    assert _jobs(connection) == rows_after_first


def test_discovered_pass_reimports_a_run_whose_sidecar_bytes_changed(
    stateful: tuple[sqlite3.Connection, Path, Path],
) -> None:
    connection, object_root, state_path = stateful
    _seed_object_store(object_root, _two_job_sidecar())
    _pass(connection, object_root, [IFS_RUN_ID], state_path)
    old_digest = _state(state_path)["runs"][IFS_RUN_ID]["sidecar_sha256"]

    newer = _publish(
        object_root,
        IFS_RUN_ID,
        _sidecar(
            jobs=[_forecast_job(status="failed", updated_at=LATER_UPDATED_AT), _convert_job()],
            source_version=LATER_SOURCE_VERSION,
        ),
    )
    second = _pass(connection, object_root, [IFS_RUN_ID], state_path)

    result = _by_run(second)[IFS_RUN_ID]
    assert result["status"] == "imported"
    assert result["updated"] == 1
    assert result["unchanged"] == 1
    assert second["skipped_unchanged"] == 0
    assert _row(connection, IFS_JOB_ID)["status"] == "failed"
    new_digest = _state(state_path)["runs"][IFS_RUN_ID]["sidecar_sha256"]
    assert new_digest == hashlib.sha256(newer).hexdigest()
    assert new_digest != old_digest


def test_discovered_pass_retries_a_failed_run_and_never_marks_it(
    stateful: tuple[sqlite3.Connection, Path, Path],
) -> None:
    connection, object_root, state_path = stateful
    good = _sidecar(jobs=[_convert_job()], source_version=OLDER_SOURCE_VERSION)
    _seed_object_store(object_root, good)
    first = _pass(connection, object_root, [IFS_RUN_ID], state_path)
    assert IFS_RUN_ID in _state(state_path)["runs"]

    conflicting = {
        **_forecast_job(),
        "job_id": IFS_CONVERT_JOB_ID,
        "run_id": IFS_RUN_ID,
        "model_id": IFS_MODEL_ID,
        "log_uri": None,
    }
    _publish(object_root, IFS_RUN_ID, _sidecar(jobs=[conflicting], source_version=NEWER_SOURCE_VERSION))
    second = _pass(connection, object_root, [IFS_RUN_ID], state_path)
    marker_after_failure = _state(state_path)["runs"].get(IFS_RUN_ID)

    # Fixed by republishing exactly the bytes the first marker described: with
    # the marker kept, the third pass would have skipped instead of importing.
    _publish(object_root, IFS_RUN_ID, good)
    third = _pass(connection, object_root, [IFS_RUN_ID], state_path)
    fourth = _pass(connection, object_root, [IFS_RUN_ID], state_path)

    assert first["imported"] == 1
    assert second["status"] == "failed"
    assert _by_run(second)[IFS_RUN_ID]["reason"] == "JOB_IDENTITY_CONFLICT"
    assert marker_after_failure is None
    assert _by_run(third)[IFS_RUN_ID]["status"] == "imported"
    assert _by_run(third)[IFS_RUN_ID]["unchanged"] == 1
    assert third["skipped_unchanged"] == 0
    assert fourth["skipped_unchanged"] == 1


def test_discovered_pass_reimports_when_a_marked_row_is_missing(
    stateful: tuple[sqlite3.Connection, Path, Path],
) -> None:
    connection, object_root, state_path = stateful
    _seed_object_store(object_root, _two_job_sidecar())
    _pass(connection, object_root, [IFS_RUN_ID], state_path)

    connection.execute("DELETE FROM ops.pipeline_job WHERE job_id = ?", (IFS_JOB_ID,))
    connection.commit()
    second = _pass(connection, object_root, [IFS_RUN_ID], state_path)
    third = _pass(connection, object_root, [IFS_RUN_ID], state_path)

    result = _by_run(second)[IFS_RUN_ID]
    assert result["status"] == "imported"
    assert result["inserted"] == 1
    assert result["unchanged"] == 1
    assert second["skipped_unchanged"] == 0
    assert _row(connection, IFS_JOB_ID) is not None
    assert third["skipped_unchanged"] == 1


@pytest.mark.parametrize(
    ("restored_columns", "expected_column", "expected_value"),
    [
        pytest.param(
            {"status": "running", "updated_at": OLDER_UPDATED_AT},
            "status",
            "succeeded",
            id="restore-to-an-earlier-version",
        ),
        pytest.param(
            {"log_uri": None},
            "log_uri",
            IFS_LOG_URI,
            id="restore-without-the-log-enrichment-at-the-same-updated_at",
        ),
    ],
)
def test_discovered_pass_reimports_when_a_marked_row_differs(
    stateful: tuple[sqlite3.Connection, Path, Path],
    restored_columns: dict[str, Any],
    expected_column: str,
    expected_value: Any,
) -> None:
    connection, object_root, state_path = stateful
    _seed_object_store(object_root, _two_job_sidecar())
    _pass(connection, object_root, [IFS_RUN_ID], state_path)

    assignments = ", ".join(f"{column} = ?" for column in restored_columns)
    connection.execute(
        f"UPDATE ops.pipeline_job SET {assignments} WHERE job_id = ?",  # noqa: S608 - fixed test column names
        (*restored_columns.values(), IFS_JOB_ID),
    )
    connection.commit()
    second = _pass(connection, object_root, [IFS_RUN_ID], state_path)

    result = _by_run(second)[IFS_RUN_ID]
    assert second["skipped_unchanged"] == 0
    assert result["status"] == "imported"
    assert result["updated"] == 1
    row = _row(connection, IFS_JOB_ID)
    assert row[expected_column] == expected_value
    assert row["updated_at"] == NEWER_UPDATED_AT


def test_discovered_pass_restores_a_shared_cycle_row_from_the_older_sidecar(
    stateful: tuple[sqlite3.Connection, Path, Path],
) -> None:
    connection, object_root, state_path = stateful
    _seed_object_store(object_root, _two_job_sidecar())
    _add_run_b(connection, object_root, _run_b_sidecar(jobs=[_run_b_forecast_job(), _newer_convert_job()]))

    # B first, so A meets B's newer copy of the shared cycle row and ignores its own.
    first = _pass(connection, object_root, [RUN_B_ID, IFS_RUN_ID], state_path)
    assert _by_run(first)[IFS_RUN_ID]["ignored_stale"] == 1
    assert set(_state(state_path)["runs"]) == {IFS_RUN_ID, RUN_B_ID}

    connection.execute("DELETE FROM ops.pipeline_job WHERE job_id = ?", (IFS_CONVERT_JOB_ID,))
    connection.commit()
    second = _pass(connection, object_root, [IFS_RUN_ID], state_path)

    result = _by_run(second)[IFS_RUN_ID]
    assert second["skipped_unchanged"] == 0
    assert result["status"] == "imported"
    assert result["inserted"] == 1
    restored = _row(connection, IFS_CONVERT_JOB_ID)
    assert restored is not None
    assert restored["updated_at"] == OLDER_UPDATED_AT
    assert set(_state(state_path)["runs"]) == {IFS_RUN_ID}


def test_discovered_pass_refreshes_sibling_markers_after_a_shared_row_update(
    stateful: tuple[sqlite3.Connection, Path, Path],
) -> None:
    connection, object_root, state_path = stateful
    _seed_object_store(object_root, _two_job_sidecar())
    _add_run_b(connection, object_root, _run_b_sidecar(jobs=[_run_b_forecast_job(), _convert_job()]))
    both = [IFS_RUN_ID, RUN_B_ID]

    first = _pass(connection, object_root, both, state_path)
    second = _pass(connection, object_root, both, state_path)
    _publish(object_root, RUN_B_ID, _run_b_sidecar(jobs=[_run_b_forecast_job(), _newer_convert_job()]))
    third = _pass(connection, object_root, both, state_path)
    fourth = _pass(connection, object_root, both, state_path)
    fifth = _pass(connection, object_root, both, state_path)

    assert first["imported"] == 2
    assert second["skipped_unchanged"] == 2
    assert second["runs"] == []
    # A's rows had not changed when the third pass started, so A is skipped; B is imported.
    assert third["skipped_unchanged"] == 1
    assert list(_by_run(third)) == [RUN_B_ID]
    assert _by_run(third)[RUN_B_ID]["updated"] == 1
    assert _row(connection, IFS_CONVERT_JOB_ID)["status"] == "failed"
    # A's marker still records the shared row before B's update: A goes through
    # the ordinary import once and meets B's newer copy.
    assert fourth["skipped_unchanged"] == 1
    assert list(_by_run(fourth)) == [IFS_RUN_ID]
    assert _by_run(fourth)[IFS_RUN_ID]["ignored_stale"] == 1
    assert _by_run(fourth)[IFS_RUN_ID]["unchanged"] == 1
    assert fifth["skipped_unchanged"] == 2
    assert fifth["runs"] == []
    assert _row(connection, IFS_CONVERT_JOB_ID)["status"] == "failed"


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(None, id="no-file"),
        pytest.param(b"{not json", id="not-json"),
        pytest.param(json.dumps({"schema": "nhms.other.v1", "runs": {}}).encode(), id="wrong-schema"),
        pytest.param(json.dumps({"schema": STATE_SCHEMA, "runs": []}).encode(), id="runs-not-an-object"),
        pytest.param(
            json.dumps({"schema": STATE_SCHEMA, "runs": {IFS_RUN_ID: "x"}}).encode(),
            id="marker-not-an-object",
        ),
    ],
)
def test_discovered_pass_treats_missing_corrupt_or_foreign_state_as_empty(
    stateful: tuple[sqlite3.Connection, Path, Path],
    damage: bytes | None,
) -> None:
    connection, object_root, state_path = stateful
    _seed_object_store(object_root, _two_job_sidecar())
    _add_run_b(connection, object_root, _run_b_sidecar(jobs=[_run_b_forecast_job()]))
    both = [IFS_RUN_ID, RUN_B_ID]
    _pass(connection, object_root, both, state_path)
    assert set(_state(state_path)["runs"]) == set(both)

    if damage is None:
        state_path.unlink()
    else:
        state_path.write_bytes(damage)
    second = _pass(connection, object_root, both, state_path)

    assert second["skip_state"] == "empty"
    assert second["skipped_unchanged"] == 0
    assert second["imported"] == 2
    assert [item["status"] for item in second["runs"]] == ["imported", "imported"]
    assert second["skip_state_saved"] is True
    state = _state(state_path)
    assert state["schema"] == STATE_SCHEMA
    assert set(state["runs"]) == set(both)
    assert set(state["runs"][IFS_RUN_ID]["jobs"]) == {IFS_JOB_ID, IFS_CONVERT_JOB_ID}
    assert set(state["runs"][RUN_B_ID]["jobs"]) == {RUN_B_JOB_ID}
    assert _pass(connection, object_root, both, state_path)["skipped_unchanged"] == 2


def test_discovered_pass_imports_a_sidecar_that_appears_later(
    stateful: tuple[sqlite3.Connection, Path, Path],
) -> None:
    connection, object_root, state_path = stateful
    _seed_object_store(object_root, _sidecar(jobs=[_forecast_job()], source_version=NEWER_SOURCE_VERSION))
    sidecar = _sidecar_path(object_root, IFS_RUN_ID)
    published = sidecar.read_bytes()
    sidecar.unlink()

    first = _pass(connection, object_root, [IFS_RUN_ID], state_path)
    marker_while_missing = _state(state_path)["runs"].get(IFS_RUN_ID)
    sidecar.write_bytes(published)
    second = _pass(connection, object_root, [IFS_RUN_ID], state_path)
    sidecar.unlink()
    third = _pass(connection, object_root, [IFS_RUN_ID], state_path)

    assert _by_run(first)[IFS_RUN_ID]["status"] == "unavailable"
    assert first["unavailable"] == 1
    assert marker_while_missing is None
    assert _by_run(second)[IFS_RUN_ID]["status"] == "imported"
    assert _by_run(second)[IFS_RUN_ID]["inserted"] == 1
    # A sidecar that disappears again is unavailable and loses its marker.
    assert _by_run(third)[IFS_RUN_ID]["status"] == "unavailable"
    assert IFS_RUN_ID not in _state(state_path)["runs"]


def test_discovered_pass_drops_markers_of_runs_no_longer_requested(
    stateful: tuple[sqlite3.Connection, Path, Path],
) -> None:
    connection, object_root, state_path = stateful
    _seed_object_store(object_root, _sidecar(jobs=[_forecast_job()], source_version=NEWER_SOURCE_VERSION))
    _add_run_b(connection, object_root, _run_b_sidecar(jobs=[_run_b_forecast_job()]))
    _pass(connection, object_root, [IFS_RUN_ID, RUN_B_ID], state_path)
    assert set(_state(state_path)["runs"]) == {IFS_RUN_ID, RUN_B_ID}

    second = _pass(connection, object_root, [IFS_RUN_ID], state_path)
    third = _pass(connection, object_root, [IFS_RUN_ID, RUN_B_ID], state_path)

    assert second["skipped_unchanged"] == 1
    assert set(_state(state_path)["runs"]) == {IFS_RUN_ID, RUN_B_ID}
    # B's marker was dropped by the second pass, so B is imported again in the third.
    assert third["skipped_unchanged"] == 1
    assert list(_by_run(third)) == [RUN_B_ID]


def test_discovered_pass_marker_is_the_digest_of_the_imported_bytes(
    stateful: tuple[sqlite3.Connection, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, object_root, state_path = stateful
    read_by_pass = _seed_and_bytes(object_root)
    republished = _sidecar_bytes(
        _sidecar(
            jobs=[_forecast_job(status="failed", updated_at=LATER_UPDATED_AT)],
            source_version=LATER_SOURCE_VERSION,
        )
    )
    original_read = LocalObjectStore.read_bytes_limited
    replaced: list[str] = []

    def read_then_republish(self: LocalObjectStore, key_or_uri: str, *, max_bytes: int) -> bytes:
        content = original_read(self, key_or_uri, max_bytes=max_bytes)
        if key_or_uri == _sidecar_key(IFS_RUN_ID) and not replaced:
            replaced.append(key_or_uri)
            original_write(LocalObjectStore(root=object_root), key_or_uri, republished)
        return content

    original_write = LocalObjectStore.write_bytes_atomic
    monkeypatch.setattr(LocalObjectStore, "read_bytes_limited", read_then_republish)
    first = _pass(connection, object_root, [IFS_RUN_ID], state_path)
    monkeypatch.setattr(LocalObjectStore, "read_bytes_limited", original_read)

    assert replaced == [_sidecar_key(IFS_RUN_ID)]
    assert _sidecar_path(object_root, IFS_RUN_ID).read_bytes() == republished
    assert _by_run(first)[IFS_RUN_ID]["inserted"] == 1
    # The committed import parsed the bytes the pass read, not the republished ones.
    assert _row(connection, IFS_JOB_ID)["status"] == "succeeded"
    assert _state(state_path)["runs"][IFS_RUN_ID]["sidecar_sha256"] == hashlib.sha256(read_by_pass).hexdigest()

    second = _pass(connection, object_root, [IFS_RUN_ID], state_path)

    assert second["skipped_unchanged"] == 0
    assert _by_run(second)[IFS_RUN_ID]["updated"] == 1
    assert _row(connection, IFS_JOB_ID)["status"] == "failed"
    assert _state(state_path)["runs"][IFS_RUN_ID]["sidecar_sha256"] == hashlib.sha256(republished).hexdigest()


def _seed_and_bytes(object_root: Path) -> bytes:
    _seed_object_store(object_root, _sidecar(jobs=[_forecast_job()], source_version=NEWER_SOURCE_VERSION))
    return _sidecar_path(object_root, IFS_RUN_ID).read_bytes()


def test_discovered_pass_presence_read_failure_skips_nothing(
    stateful: tuple[sqlite3.Connection, Path, Path],
) -> None:
    connection, object_root, state_path = stateful
    _seed_object_store(object_root, _two_job_sidecar())
    _pass(connection, object_root, [IFS_RUN_ID], state_path)
    failures: list[str] = []

    def fail_first_presence_read(sql: str) -> bool:
        if _presence_reads([sql]) and not failures:
            failures.append(sql)
            return True
        return False

    second = _pass(connection, object_root, [IFS_RUN_ID], state_path, fail=fail_first_presence_read)
    third = _pass(connection, object_root, [IFS_RUN_ID], state_path)

    assert len(failures) == 1
    assert second["skip_state"] == "presence_check_failed"
    assert second["skipped_unchanged"] == 0
    assert _by_run(second)[IFS_RUN_ID]["status"] == "imported"
    assert _by_run(second)[IFS_RUN_ID]["unchanged"] == 2
    assert second["skip_state_saved"] is True
    assert third["skip_state"] == "loaded"
    assert third["skipped_unchanged"] == 1


def test_discovered_pass_post_import_read_failure_leaves_the_imported_run_unmarked(
    stateful: tuple[sqlite3.Connection, Path, Path],
) -> None:
    connection, object_root, state_path = stateful
    _seed_object_store(object_root, _sidecar(jobs=[_forecast_job()], source_version=NEWER_SOURCE_VERSION))

    first = _pass(connection, object_root, [IFS_RUN_ID], state_path, fail=lambda sql: bool(_presence_reads([sql])))
    markers_after_first = _state(state_path)["runs"]
    second = _pass(connection, object_root, [IFS_RUN_ID], state_path)

    assert first["status"] == "imported"
    assert _by_run(first)[IFS_RUN_ID]["inserted"] == 1
    assert first["skip_state_saved"] is True
    assert IFS_RUN_ID not in markers_after_first
    assert second["skipped_unchanged"] == 0
    assert _by_run(second)[IFS_RUN_ID]["unchanged"] == 1


def test_discovered_pass_state_write_failure_only_reports_it(
    stateful: tuple[sqlite3.Connection, Path, Path],
) -> None:
    connection, object_root, state_path = stateful
    _seed_object_store(object_root, _sidecar(jobs=[_forecast_job()], source_version=NEWER_SOURCE_VERSION))
    blocked = state_path.parent / "not-a-directory"
    blocked.write_text("", encoding="utf-8")

    result = _pass(connection, object_root, [IFS_RUN_ID], blocked / STATE_FILE_NAME)

    assert result["status"] == "imported"
    assert result["imported"] == 1
    assert result["skip_state"] == "empty"
    assert result["skip_state_saved"] is False


def _make_symlinked_sidecar(object_root: Path, outside: Path) -> None:
    sidecar = _sidecar_path(object_root, IFS_RUN_ID)
    planted = outside / "pipeline_jobs.json"
    planted.write_bytes(sidecar.read_bytes())
    sidecar.unlink()
    sidecar.symlink_to(planted)


def _make_oversized_sidecar(object_root: Path, _outside: Path) -> None:
    _sidecar_path(object_root, IFS_RUN_ID).write_bytes(b"{" + b" " * pipeline_job_provenance.MAX_SIDECAR_BYTES + b"}")


# The reason codes today's per-run path reports for these inputs. The fixture
# names `SIDECAR_TOO_LARGE`, but that code is raised only by the publisher; the
# importer reports an over-limit read as `SIDECAR_UNREADABLE`, and a symlinked
# sidecar is refused by the existence probe as `ObjectStoreError`.
@pytest.mark.parametrize(
    ("spoil", "expected_reason"),
    [
        pytest.param(_make_symlinked_sidecar, "ObjectStoreError", id="symlinked-sidecar"),
        pytest.param(_make_oversized_sidecar, "SIDECAR_UNREADABLE", id="oversized-sidecar"),
    ],
)
def test_discovered_pass_unreadable_sidecar_fails_only_its_run(
    tmp_path: Path,
    spoil: Callable[[Path, Path], None],
    expected_reason: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    reasons: dict[str, str] = {}
    for label, state_path in (("without", None), ("with", tmp_path / "work" / STATE_FILE_NAME)):
        object_root = tmp_path / f"object-store-{label}"
        object_root.mkdir()
        connection = _new_db()
        _seed_object_store(object_root, _sidecar(jobs=[_forecast_job()], source_version=NEWER_SOURCE_VERSION))
        _add_run_b(connection, object_root, _run_b_sidecar(jobs=[_run_b_forecast_job()]))
        spoil(object_root, outside)
        if state_path is not None:
            state_path.parent.mkdir()
        first = _pass(connection, object_root, [IFS_RUN_ID, RUN_B_ID], state_path)
        assert first["status"] == "failed"
        assert first["failed"] == 1
        assert _by_run(first)[IFS_RUN_ID]["status"] == "failed"
        assert _by_run(first)[RUN_B_ID]["status"] == "imported"
        reasons[label] = _by_run(first)[IFS_RUN_ID]["reason"]
        if state_path is not None:
            assert set(_state(state_path)["runs"]) == {RUN_B_ID}
            second = _pass(connection, object_root, [IFS_RUN_ID, RUN_B_ID], state_path)
            assert list(_by_run(second)) == [IFS_RUN_ID]
            assert _by_run(second)[IFS_RUN_ID]["reason"] == reasons["without"]
            assert second["skipped_unchanged"] == 1
            assert set(_state(state_path)["runs"]) == {RUN_B_ID}
        connection.close()

    assert reasons == {"without": expected_reason, "with": expected_reason}


def test_discovered_pass_without_state_path_imports_every_run_and_writes_no_file(tmp_path: Path) -> None:
    object_root = tmp_path / "object-store"
    object_root.mkdir()
    connection = _new_db()
    _seed_object_store(object_root, _two_job_sidecar())
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    cwd_before = sorted(os.listdir(Path.cwd()))

    first = _pass(connection, object_root, [IFS_RUN_ID], None)
    second = _pass(connection, object_root, [IFS_RUN_ID], None)

    assert set(second) == {"status", "runs", "imported", "unavailable", "failed", "skipped_unchanged", "skip_state"}
    assert first["skip_state"] == second["skip_state"] == "disabled"
    assert first["skipped_unchanged"] == second["skipped_unchanged"] == 0
    assert second["runs"] == [
        {
            "run_id": IFS_RUN_ID,
            "status": "imported",
            "inserted": 0,
            "updated": 0,
            "unchanged": 2,
            "ignored_stale": 0,
        }
    ]
    assert _by_run(first)[IFS_RUN_ID]["inserted"] == 2
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before
    assert sorted(os.listdir(Path.cwd())) == cwd_before
    connection.close()


def test_discovered_pass_batches_the_row_read(
    stateful: tuple[sqlite3.Connection, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, object_root, state_path = stateful
    second_forecast = {
        **_forecast_job(),
        "job_id": f"{IFS_JOB_ID}_retry",
        "array_task_id": 24,
        "log_uri": None,
    }
    jobs = [_forecast_job(), second_forecast, _convert_job()]
    _seed_object_store(object_root, _sidecar(jobs=jobs, source_version=NEWER_SOURCE_VERSION))
    monkeypatch.setattr(pipeline_job_provenance, "MAX_JOB_IDS_PER_ROW_READ", 2)

    first = _pass(connection, object_root, [IFS_RUN_ID], state_path)
    executed: list[str] = []
    second = _pass(connection, object_root, [IFS_RUN_ID], state_path, executed=executed)

    assert _by_run(first)[IFS_RUN_ID]["inserted"] == 3
    assert len(_state(state_path)["runs"][IFS_RUN_ID]["jobs"]) == 3
    assert second["skipped_unchanged"] == 1
    reads = _presence_reads(executed)
    assert reads == executed
    assert len(reads) == 2  # ceil(3 / 2)
    assert sorted(statement.count("%s") for statement in reads) == [1, 2]


def test_discovered_pass_skips_a_zero_job_sidecar_without_a_connection(
    stateful: tuple[sqlite3.Connection, Path, Path],
) -> None:
    connection, object_root, state_path = stateful
    _seed_object_store(object_root, _sidecar(jobs=[], source_version=NEWER_SOURCE_VERSION))
    connects: list[str] = []
    inner = _connect_factory(connection)

    def counting_connect(database_url: str, **kwargs: Any) -> _KeepAliveConnection:
        connects.append(database_url)
        return inner(database_url, **kwargs)

    def discovered_pass() -> dict[str, Any]:
        return import_discovered_pipeline_job_provenance(
            database_url="sqlite://",
            object_store_root=object_root,
            run_ids=[IFS_RUN_ID],
            connect=counting_connect,
            state_path=state_path,
        )

    first = discovered_pass()
    connects_after_first = len(connects)
    second = discovered_pass()

    assert _by_run(first)[IFS_RUN_ID]["status"] == "imported"
    assert connects_after_first == 1  # the import itself; an empty read-back opens nothing
    assert _state(state_path)["runs"][IFS_RUN_ID]["jobs"] == {}
    assert second["skipped_unchanged"] == 1
    assert second["runs"] == []
    assert len(connects) == connects_after_first
