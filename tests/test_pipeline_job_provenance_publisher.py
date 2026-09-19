from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from packages.common.object_store import LocalObjectStore
from services.artifacts import published_log_uri
from services.orchestrator.chain_types import OrchestratorError
from services.orchestrator.file_orchestration_journal import (
    FILE_JOURNAL_READ_BLOCKED_STATUS,
    FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
)
from services.orchestrator.pipeline_job_provenance import (
    PipelineJobProvenanceError,
    publish_run_pipeline_job_provenance,
    publish_runs_pipeline_job_provenance,
)
from tests.test_file_orchestration_journal import _journal_record as _file_journal_record
from tests.test_file_orchestration_journal import _latest_view, _model_context, _write_json, _write_jsonl
from workers.data_adapters.base import cycle_id_for

# Independent expected values from `.workplans/2420/node22-bound-task-log-provenance.jsonl`.
IFS_PARENT_JOB_ID = "49999"
IFS_TASK_ID = 23
IFS_SELECTED_RUN_ID = "fcst_ifs_2026091612_dg_9ccb261a39d51c24f4de9173fb4461b6"
IFS_SELECTED_MODEL_ID = "dg_9ccb261a39d51c24f4de9173fb4461b6"
IFS_ENVELOPE_RUN_ID = "fcst_ifs_2026091612_dg_0e611766f8d1edb6e99a2ba1892e48e1"
IFS_ENVELOPE_MODEL_ID = "dg_0e611766f8d1edb6e99a2ba1892e48e1"
IFS_STDOUT_BYTES = 426
IFS_CYCLE_TIME = datetime(2026, 9, 16, 12, tzinfo=UTC)
IFS_CYCLE_STAMP = "2026091612"
IFS_JOB_ID = "job_fcst_ifs_2026091612_dg_9ccb261a39d51c24f4de9173fb4461b6_forecast_reconciled_49999_23"
IFS_CONVERT_JOB_ID = "job_cycle_ifs_2026091612_convert"
IFS_CONVERT_RUN_ID = "cycle_ifs_2026091612_convert"
IFS_STDOUT = "x" * IFS_STDOUT_BYTES
IFS_SIBLING_STDOUT = "y" * IFS_STDOUT_BYTES
IFS_SECOND_TASK_ID = 24
IFS_SECOND_MODEL_ID = "dg_1e611766f8d1edb6e99a2ba1892e48e1"
IFS_SECOND_RUN_ID = f"fcst_ifs_{IFS_CYCLE_STAMP}_{IFS_SECOND_MODEL_ID}"
IFS_SECOND_JOB_ID = f"job_{IFS_SECOND_RUN_ID}_forecast_reconciled_{IFS_PARENT_JOB_ID}_{IFS_SECOND_TASK_ID}"
IFS_SECOND_STDOUT = "z" * IFS_STDOUT_BYTES


def _forecast_job() -> dict[str, Any]:
    return {
        "job_id": IFS_JOB_ID,
        "run_id": IFS_SELECTED_RUN_ID,
        "cycle_id": cycle_id_for("IFS", IFS_CYCLE_TIME),
        "job_type": "run_shud_forecast_array",
        "slurm_job_id": IFS_PARENT_JOB_ID,
        "array_task_id": IFS_TASK_ID,
        "model_id": IFS_SELECTED_MODEL_ID,
        "status": "succeeded",
        "stage": "forecast",
        "log_uri": None,
        "created_at": "2026-09-16T12:05:00Z",
        "updated_at": "2026-09-16T12:40:00Z",
        "submitted_at": None,
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
        "retry_count": 0,
        "idempotency_key": f"{IFS_SELECTED_RUN_ID}:forecast",
    }


def _second_forecast_job() -> dict[str, Any]:
    return {
        **_forecast_job(),
        "job_id": IFS_SECOND_JOB_ID,
        "run_id": IFS_SECOND_RUN_ID,
        "array_task_id": IFS_SECOND_TASK_ID,
        "model_id": IFS_SECOND_MODEL_ID,
        "idempotency_key": f"{IFS_SECOND_RUN_ID}:forecast",
    }


def _cycle_scoped_convert_job() -> dict[str, Any]:
    return {
        "job_id": IFS_CONVERT_JOB_ID,
        "run_id": IFS_CONVERT_RUN_ID,
        "cycle_id": cycle_id_for("IFS", IFS_CYCLE_TIME),
        "job_type": "convert_source_cycle",
        "slurm_job_id": "49800",
        "model_id": None,
        "status": "succeeded",
        "stage": "convert",
        "created_at": "2026-09-16T12:01:00Z",
        "updated_at": "2026-09-16T12:10:00Z",
        "idempotency_key": f"{IFS_CONVERT_RUN_ID}:convert",
    }


def _journal_record(job: dict[str, Any], *, sequence: int) -> dict[str, Any]:
    return _file_journal_record(
        record_type="pipeline_job",
        source_id="IFS",
        cycle_time=IFS_CYCLE_TIME,
        payload=job,
        sequence=sequence,
        model_id=job.get("model_id"),
    )


def _seed_ifs_journal(journal_root: Path, *, jobs: list[dict[str, Any]] | None = None) -> None:
    seeded = jobs if jobs is not None else [_forecast_job(), _cycle_scoped_convert_job()]
    cycle_scoped = [job for job in seeded if not job.get("model_id")]
    jobs_by_model: dict[str, list[dict[str, Any]]] = {}
    for job in seeded:
        model_id = job.get("model_id")
        if isinstance(model_id, str) and model_id:
            jobs_by_model.setdefault(model_id, []).append(job)
    if not jobs_by_model:
        jobs_by_model[IFS_SELECTED_MODEL_ID] = []
    for model_id, model_jobs in jobs_by_model.items():
        latest = _latest_view(
            source_id="IFS",
            cycle_time=IFS_CYCLE_TIME,
            model_id=model_id,
            hydro_status="published",
            jobs=[*model_jobs, *cycle_scoped],
        )
        latest["schema_version"] = FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION
        latest["model_context"] = _model_context(model_id)
        latest["forcing_version"] = None
        hydro_run = latest.get("hydro_run")
        if isinstance(hydro_run, dict):
            forecast = next((job for job in model_jobs if str(job.get("run_id") or "").startswith("fcst_")), None)
            if forecast is not None:
                hydro_run["run_id"] = forecast["run_id"]
            hydro_run["source_id"] = "IFS"
        _write_json(
            journal_root / "latest" / "IFS" / IFS_CYCLE_STAMP / f"{model_id}.json",
            latest,
        )
    _write_jsonl(
        journal_root / "journal" / "IFS" / f"{IFS_CYCLE_STAMP}.jsonl",
        [_journal_record(job, sequence=index + 1) for index, job in enumerate(seeded)],
    )
    for index, job in enumerate(seeded):
        _write_json(
            journal_root / "pipeline-jobs" / f"{job['job_id']}.json",
            _journal_record(job, sequence=index + 1),
        )


def _seed_manifest(
    object_root: Path,
    *,
    run_id: str = IFS_SELECTED_RUN_ID,
    model_id: str = IFS_SELECTED_MODEL_ID,
) -> None:
    store = LocalObjectStore(root=object_root)
    store.write_bytes_atomic(
        f"runs/{run_id}/input/manifest.json",
        json.dumps(
            {
                "run_id": run_id,
                "source_id": "IFS",
                "cycle_time": "2026-09-16T12:00:00Z",
                "model_id": model_id,
                "identity": {
                    "source": "IFS",
                    "cycle_time": "2026-09-16T12:00:00Z",
                    "run_id": run_id,
                    "model_id": model_id,
                },
            },
            sort_keys=True,
        ).encode("utf-8"),
    )


def _parent_array_response(*, include_sibling: bool = True) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    if include_sibling:
        tasks.append(
            {
                "task_id": 0,
                "identity_complete": True,
                "run_id": IFS_ENVELOPE_RUN_ID,
                "model_id": IFS_ENVELOPE_MODEL_ID,
                "stdout": IFS_SIBLING_STDOUT,
                "stderr": "",
                "truncated": False,
            }
        )
    tasks.append(
        {
            "task_id": IFS_TASK_ID,
            "identity_complete": True,
            "run_id": IFS_SELECTED_RUN_ID,
            "model_id": IFS_SELECTED_MODEL_ID,
            "stdout": IFS_STDOUT,
            "stderr": "",
            "truncated": False,
            "missing_stdout": False,
        }
    )
    return {
        "job_id": IFS_PARENT_JOB_ID,
        "run_id": IFS_ENVELOPE_RUN_ID,
        "complete": True,
        "metadata_complete": True,
        "truncated": False,
        "array_task_logs": tasks,
    }


def _expected_log_uri() -> str:
    return published_log_uri(
        source="IFS",
        cycle_time=IFS_CYCLE_TIME,
        run_id=IFS_SELECTED_RUN_ID,
        job_id=IFS_JOB_ID,
        stream="out",
    )


def _publish(
    tmp_path: Path,
    *,
    jobs: list[dict[str, Any]] | None = None,
    fetch_logs: Any | None = None,
) -> tuple[dict[str, Any], Path, Path, Path]:
    journal_root = tmp_path / "journal"
    object_root = tmp_path / "object-store"
    published_root = tmp_path / "published"
    journal_root.mkdir()
    object_root.mkdir()
    published_root.mkdir()
    _seed_ifs_journal(journal_root, jobs=jobs)
    _seed_manifest(object_root)
    summary = publish_run_pipeline_job_provenance(
        run_id=IFS_SELECTED_RUN_ID,
        journal_root=journal_root,
        object_store_root=object_root,
        published_artifact_root=published_root,
        fetch_logs=fetch_logs or (lambda _job_id: _parent_array_response()),
    )
    return summary, journal_root, object_root, published_root


def test_publisher_selects_ifs_task_entry_not_parent_envelope(tmp_path: Path) -> None:
    requested: list[str] = []

    def fetch_logs(job_id: str) -> dict[str, Any]:
        requested.append(job_id)
        return _parent_array_response()

    summary, _journal_root, object_root, published_root = _publish(tmp_path, fetch_logs=fetch_logs)
    expected_uri = _expected_log_uri()
    sidecar = json.loads(
        (object_root / "runs" / IFS_SELECTED_RUN_ID / "input" / "pipeline_jobs.json").read_text(encoding="utf-8")
    )
    forecast_row = next(job for job in sidecar["jobs"] if job["job_id"] == IFS_JOB_ID)
    convert_row = next(job for job in sidecar["jobs"] if job["job_id"] == IFS_CONVERT_JOB_ID)
    log_path = published_root / "logs" / "IFS" / IFS_CYCLE_STAMP / IFS_SELECTED_RUN_ID / f"{IFS_JOB_ID}.out"
    sibling_path = published_root / "logs" / "IFS" / IFS_CYCLE_STAMP / IFS_ENVELOPE_RUN_ID / f"{IFS_JOB_ID}.out"

    assert requested == [IFS_PARENT_JOB_ID]
    assert summary["status"] == "published"
    assert summary["advertised_logs"] == 1
    assert forecast_row["log_uri"] == expected_uri
    assert forecast_row["run_id"] == IFS_SELECTED_RUN_ID
    assert forecast_row["model_id"] == IFS_SELECTED_MODEL_ID
    assert convert_row["run_id"] == IFS_CONVERT_RUN_ID
    assert convert_row["model_id"] is None
    assert log_path.read_bytes() == IFS_STDOUT.encode("utf-8")
    assert len(log_path.read_bytes()) == IFS_STDOUT_BYTES
    assert not sibling_path.exists()
    assert IFS_ENVELOPE_RUN_ID not in json.dumps(sidecar)


def test_publisher_leaves_log_unadvertised_when_task_entry_is_ambiguous(tmp_path: Path) -> None:
    def fetch_logs(_job_id: str) -> dict[str, Any]:
        return {
            "job_id": IFS_PARENT_JOB_ID,
            "run_id": IFS_ENVELOPE_RUN_ID,
            "complete": True,
            "metadata_complete": True,
            "array_task_logs": [
                {
                    "task_id": IFS_TASK_ID,
                    "identity_complete": True,
                    "run_id": IFS_SELECTED_RUN_ID,
                    "model_id": IFS_SELECTED_MODEL_ID,
                    "stdout": IFS_STDOUT,
                },
                {
                    "task_id": IFS_TASK_ID,
                    "identity_complete": True,
                    "run_id": IFS_ENVELOPE_RUN_ID,
                    "model_id": IFS_ENVELOPE_MODEL_ID,
                    "stdout": IFS_SIBLING_STDOUT,
                },
            ],
        }

    summary, _journal_root, object_root, published_root = _publish(
        tmp_path,
        jobs=[_forecast_job()],
        fetch_logs=fetch_logs,
    )
    sidecar = json.loads(
        (object_root / "runs" / IFS_SELECTED_RUN_ID / "input" / "pipeline_jobs.json").read_text(encoding="utf-8")
    )

    assert summary["status"] == "published"
    assert summary["advertised_logs"] == 0
    assert sidecar["jobs"][0]["log_uri"] is None
    assert not (published_root / "logs").exists()


def test_publisher_does_not_treat_redaction_sentinel_as_published_log(tmp_path: Path) -> None:
    job = _forecast_job()
    job["log_uri"] = "[object-uri]"
    summary, _journal_root, object_root, _published_root = _publish(
        tmp_path,
        jobs=[job],
        fetch_logs=lambda _job_id: _parent_array_response(include_sibling=False),
    )
    sidecar = json.loads(
        (object_root / "runs" / IFS_SELECTED_RUN_ID / "input" / "pipeline_jobs.json").read_text(encoding="utf-8")
    )

    assert summary["status"] == "published"
    assert sidecar["jobs"][0]["log_uri"] == _expected_log_uri()
    assert "[object-uri]" not in json.dumps(sidecar)




def test_publisher_keeps_only_a_contained_existing_published_log(tmp_path: Path) -> None:
    journal_root = tmp_path / "journal"
    object_root = tmp_path / "object-store"
    published_root = tmp_path / "published"
    journal_root.mkdir()
    object_root.mkdir()
    published_root.mkdir()
    job = _forecast_job()
    job["log_uri"] = _expected_log_uri()
    _seed_ifs_journal(journal_root, jobs=[job])
    _seed_manifest(object_root)
    artifact_path = published_root / "logs" / "IFS" / IFS_CYCLE_STAMP / IFS_SELECTED_RUN_ID / f"{IFS_JOB_ID}.out"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_bytes(IFS_STDOUT.encode("utf-8"))

    summary = publish_run_pipeline_job_provenance(
        run_id=IFS_SELECTED_RUN_ID,
        journal_root=journal_root,
        object_store_root=object_root,
        published_artifact_root=published_root,
        fetch_logs=lambda _job_id: pytest.fail("contained existing log must not refetch the gateway"),
    )
    sidecar = json.loads(
        (object_root / "runs" / IFS_SELECTED_RUN_ID / "input" / "pipeline_jobs.json").read_text(encoding="utf-8")
    )

    assert summary["advertised_logs"] == 0
    assert sidecar["jobs"][0]["log_uri"] == _expected_log_uri()
    assert artifact_path.read_bytes() == IFS_STDOUT.encode("utf-8")


def test_publisher_preserves_gateway_truncation_metadata(tmp_path: Path) -> None:
    response = _parent_array_response(include_sibling=False)
    task = response["array_task_logs"][0]
    task["truncated"] = True

    summary, _journal_root, object_root, _published_root = _publish(
        tmp_path,
        jobs=[_forecast_job()],
        fetch_logs=lambda _job_id: response,
    )
    sidecar = json.loads(
        (object_root / "runs" / IFS_SELECTED_RUN_ID / "input" / "pipeline_jobs.json").read_text(encoding="utf-8")
    )
    row = sidecar["jobs"][0]

    assert summary["advertised_logs"] == 1
    assert row["log_truncated"] is True
    assert row["log_stdout_bytes"] == IFS_STDOUT_BYTES
    assert row["log_stderr_bytes"] == 0


def test_publisher_rejects_master_log_relabelled_as_array_task(tmp_path: Path) -> None:
    response = _parent_array_response(include_sibling=False)
    response["array_task_logs"][0]["job_id"] = IFS_PARENT_JOB_ID

    summary, _journal_root, object_root, published_root = _publish(
        tmp_path,
        jobs=[_forecast_job()],
        fetch_logs=lambda _job_id: response,
    )
    sidecar = json.loads(
        (object_root / "runs" / IFS_SELECTED_RUN_ID / "input" / "pipeline_jobs.json").read_text(encoding="utf-8")
    )

    assert summary["advertised_logs"] == 0
    assert sidecar["jobs"][0]["log_uri"] is None
    assert not (published_root / "logs").exists()




def test_publisher_fetches_a_parent_array_once_per_publication_batch(tmp_path: Path) -> None:
    journal_root = tmp_path / "journal"
    object_root = tmp_path / "object-store"
    published_root = tmp_path / "published"
    journal_root.mkdir()
    object_root.mkdir()
    published_root.mkdir()
    _seed_ifs_journal(journal_root, jobs=[_forecast_job(), _second_forecast_job()])
    _seed_manifest(object_root)
    _seed_manifest(object_root, run_id=IFS_SECOND_RUN_ID, model_id=IFS_SECOND_MODEL_ID)
    requested: list[str] = []
    response = _parent_array_response()
    response["array_task_logs"].append(
        {
            "task_id": IFS_SECOND_TASK_ID,
            "identity_complete": True,
            "run_id": IFS_SECOND_RUN_ID,
            "model_id": IFS_SECOND_MODEL_ID,
            "stdout": IFS_SECOND_STDOUT,
            "stderr": "",
            "truncated": False,
        }
    )

    summary = publish_runs_pipeline_job_provenance(
        run_ids=[IFS_SELECTED_RUN_ID, IFS_SECOND_RUN_ID],
        journal_root=journal_root,
        object_store_root=object_root,
        published_artifact_root=published_root,
        fetch_logs=lambda job_id: requested.append(job_id) or response,
    )

    assert summary["status"] == "published"
    assert summary["published"] == 2
    assert requested == [IFS_PARENT_JOB_ID]
    assert (
        published_root / "logs" / "IFS" / IFS_CYCLE_STAMP / IFS_SECOND_RUN_ID / f"{IFS_SECOND_JOB_ID}.out"
    ).read_bytes() == IFS_SECOND_STDOUT.encode("utf-8")


def test_publisher_does_not_overwrite_a_newer_published_job_snapshot(tmp_path: Path) -> None:
    summary, journal_root, object_root, published_root = _publish(tmp_path, jobs=[_forecast_job()])
    assert summary["status"] == "published"
    stale = _forecast_job()
    stale["status"] = "running"
    stale["updated_at"] = "2026-09-16T12:10:00Z"
    _seed_ifs_journal(journal_root, jobs=[stale])

    replay = publish_run_pipeline_job_provenance(
        run_id=IFS_SELECTED_RUN_ID,
        journal_root=journal_root,
        object_store_root=object_root,
        published_artifact_root=published_root,
        fetch_logs=lambda _job_id: _parent_array_response(include_sibling=False),
    )
    sidecar = json.loads(
        (object_root / "runs" / IFS_SELECTED_RUN_ID / "input" / "pipeline_jobs.json").read_text(encoding="utf-8")
    )

    assert replay["status"] == "published"
    assert sidecar["jobs"][0]["status"] == "succeeded"
    assert sidecar["jobs"][0]["updated_at"] == "2026-09-16T12:40:00Z"

def test_publisher_rejects_symlink_journal_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-journal"
    alias = tmp_path / "alias-journal"
    object_root = tmp_path / "object-store"
    real_root.mkdir()
    object_root.mkdir()
    alias.symlink_to(real_root, target_is_directory=True)
    _seed_ifs_journal(real_root)
    _seed_manifest(object_root)

    with pytest.raises(OrchestratorError) as caught:
        publish_run_pipeline_job_provenance(
            run_id=IFS_SELECTED_RUN_ID,
            journal_root=alias,
            object_store_root=object_root,
            fetch_logs=lambda _job_id: _parent_array_response(),
        )
    assert caught.value.error_code == "FILE_JOURNAL_INVALID_ROOT"
    assert not (object_root / "runs" / IFS_SELECTED_RUN_ID / "input" / "pipeline_jobs.json").exists()


def test_publisher_rejects_traversal_run_id(tmp_path: Path) -> None:
    journal_root = tmp_path / "journal"
    object_root = tmp_path / "object-store"
    journal_root.mkdir()
    object_root.mkdir()

    with pytest.raises(PipelineJobProvenanceError) as caught:
        publish_run_pipeline_job_provenance(
            run_id="../secret",
            journal_root=journal_root,
            object_store_root=object_root,
            fetch_logs=lambda _job_id: _parent_array_response(),
        )
    assert caught.value.code == "IDENTITY_INVALID"


def test_publisher_journal_read_does_not_mkdir_or_take_write_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal_root = tmp_path / "journal"
    object_root = tmp_path / "object-store"
    published_root = tmp_path / "published"
    journal_root.mkdir()
    object_root.mkdir()
    published_root.mkdir()
    _seed_ifs_journal(journal_root, jobs=[_forecast_job()])
    _seed_manifest(object_root)
    before = {path.relative_to(journal_root).as_posix() for path in journal_root.rglob("*")}
    mkdir_calls: list[str] = []
    real_mkdir = Path.mkdir

    def tracking_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        try:
            relative = self.relative_to(journal_root)
        except ValueError:
            return real_mkdir(self, *args, **kwargs)
        mkdir_calls.append(relative.as_posix())
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", tracking_mkdir)
    summary = publish_run_pipeline_job_provenance(
        run_id=IFS_SELECTED_RUN_ID,
        journal_root=journal_root,
        object_store_root=object_root,
        published_artifact_root=published_root,
        fetch_logs=lambda _job_id: _parent_array_response(include_sibling=False),
    )
    after = {path.relative_to(journal_root).as_posix() for path in journal_root.rglob("*")}

    assert summary["status"] == "published"
    assert mkdir_calls == []
    assert after == before
    assert not (journal_root / "reconcile-inventory").exists()


def test_publisher_leaves_log_unadvertised_when_metadata_is_incomplete(tmp_path: Path) -> None:
    response = _parent_array_response(include_sibling=False)
    response["metadata_complete"] = False

    summary, _journal_root, object_root, published_root = _publish(
        tmp_path,
        jobs=[_forecast_job()],
        fetch_logs=lambda _job_id: response,
    )
    sidecar = json.loads(
        (object_root / "runs" / IFS_SELECTED_RUN_ID / "input" / "pipeline_jobs.json").read_text(encoding="utf-8")
    )

    assert summary["advertised_logs"] == 0
    assert sidecar["jobs"][0]["log_uri"] is None
    assert not (published_root / "logs").exists()


def test_publisher_leaves_log_unadvertised_when_task_identity_is_incomplete(tmp_path: Path) -> None:
    response = _parent_array_response(include_sibling=False)
    response["array_task_logs"][0]["identity_complete"] = False

    summary, _journal_root, object_root, published_root = _publish(
        tmp_path,
        jobs=[_forecast_job()],
        fetch_logs=lambda _job_id: response,
    )
    sidecar = json.loads(
        (object_root / "runs" / IFS_SELECTED_RUN_ID / "input" / "pipeline_jobs.json").read_text(encoding="utf-8")
    )

    assert summary["advertised_logs"] == 0
    assert sidecar["jobs"][0]["log_uri"] is None
    assert not (published_root / "logs").exists()


def test_publisher_fail_closes_on_blocked_journal_row(tmp_path: Path) -> None:
    blocked = _forecast_job()
    blocked["status"] = FILE_JOURNAL_READ_BLOCKED_STATUS

    with pytest.raises(PipelineJobProvenanceError) as caught:
        _publish(tmp_path, jobs=[blocked], fetch_logs=lambda _job_id: _parent_array_response())

    assert caught.value.code == "JOURNAL_READ_BLOCKED"


def test_publisher_does_not_treat_file_uri_as_published_log(tmp_path: Path) -> None:
    job = _forecast_job()
    job["log_uri"] = "file:///tmp/secret.out"

    summary, _journal_root, object_root, published_root = _publish(
        tmp_path,
        jobs=[job],
        fetch_logs=lambda _job_id: _parent_array_response(include_sibling=False),
    )
    sidecar = json.loads(
        (object_root / "runs" / IFS_SELECTED_RUN_ID / "input" / "pipeline_jobs.json").read_text(encoding="utf-8")
    )

    assert summary["status"] == "published"
    assert sidecar["jobs"][0]["log_uri"] == _expected_log_uri()
    assert "file://" not in json.dumps(sidecar)
    assert (published_root / "logs" / "IFS" / IFS_CYCLE_STAMP / IFS_SELECTED_RUN_ID / f"{IFS_JOB_ID}.out").exists()


def test_publisher_refuses_equal_version_conflicting_sidecar_without_overwriting_log(
    tmp_path: Path,
) -> None:
    original_bytes = IFS_STDOUT.encode("utf-8")
    drifted_bytes = ("y" * IFS_STDOUT_BYTES).encode("utf-8")
    summary, journal_root, object_root, published_root = _publish(
        tmp_path,
        jobs=[_forecast_job()],
        fetch_logs=lambda _job_id: _parent_array_response(include_sibling=False),
    )
    assert summary["status"] == "published"
    artifact_path = published_root / "logs" / "IFS" / IFS_CYCLE_STAMP / IFS_SELECTED_RUN_ID / f"{IFS_JOB_ID}.out"
    sidecar_path = object_root / "runs" / IFS_SELECTED_RUN_ID / "input" / "pipeline_jobs.json"
    original_sidecar = sidecar_path.read_bytes()

    drifted = _parent_array_response(include_sibling=False)
    drifted["array_task_logs"][0]["stdout"] = drifted_bytes.decode("utf-8")
    with pytest.raises(PipelineJobProvenanceError) as caught:
        publish_run_pipeline_job_provenance(
            run_id=IFS_SELECTED_RUN_ID,
            journal_root=journal_root,
            object_store_root=object_root,
            published_artifact_root=published_root,
            fetch_logs=lambda _job_id: drifted,
        )

    assert caught.value.code == "EQUAL_VERSION_CONFLICT"
    assert artifact_path.read_bytes() == original_bytes
    assert sidecar_path.read_bytes() == original_sidecar


def test_publisher_identical_log_refetch_is_a_noop(tmp_path: Path) -> None:
    summary, journal_root, object_root, published_root = _publish(
        tmp_path,
        jobs=[_forecast_job()],
        fetch_logs=lambda _job_id: _parent_array_response(include_sibling=False),
    )
    artifact_path = published_root / "logs" / "IFS" / IFS_CYCLE_STAMP / IFS_SELECTED_RUN_ID / f"{IFS_JOB_ID}.out"
    sidecar_path = object_root / "runs" / IFS_SELECTED_RUN_ID / "input" / "pipeline_jobs.json"
    original_sidecar = sidecar_path.read_bytes()

    replay = publish_run_pipeline_job_provenance(
        run_id=IFS_SELECTED_RUN_ID,
        journal_root=journal_root,
        object_store_root=object_root,
        published_artifact_root=published_root,
        fetch_logs=lambda _job_id: _parent_array_response(include_sibling=False),
    )

    assert summary["status"] == "published"
    assert replay["status"] == "published"
    assert artifact_path.read_bytes() == IFS_STDOUT.encode("utf-8")
    assert sidecar_path.read_bytes() == original_sidecar

