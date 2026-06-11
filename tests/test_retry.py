from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.main import app
from apps.api.routes import pipeline as pipeline_routes
from services.orchestrator.persistence import Base, PipelineEvent, PipelineJob, PipelineStore
from services.orchestrator.retry import (
    NON_TRANSIENT_ERROR_CODES,
    TRANSIENT_ERROR_CODES,
    RetryConfig,
    RetryConflictError,
    RetryError,
    RetryNotFoundError,
    RetryService,
    compute_backoff_seconds,
    is_transient_error,
)


def test_transient_error_classification() -> None:
    for error_code in TRANSIENT_ERROR_CODES:
        assert is_transient_error(error_code) is True
    for error_code in NON_TRANSIENT_ERROR_CODES:
        assert is_transient_error(error_code) is False
    assert is_transient_error("UNKNOWN_ERROR") is False
    assert is_transient_error(None) is False


def test_backoff_calculation() -> None:
    assert compute_backoff_seconds(0) == 60
    assert compute_backoff_seconds(1) == 300
    assert compute_backoff_seconds(2) == 900
    assert compute_backoff_seconds(3) == 900
    assert compute_backoff_seconds(0, [5, 10]) == 5
    assert compute_backoff_seconds(4, [5, 10]) == 10


def test_should_auto_retry_transient() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT")
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is True


def test_should_auto_retry_poll_timeout() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_JOB_TIMEOUT")
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is True


@pytest.mark.parametrize("error_code", ["INVALID_MANIFEST", "MALFORMED_INPUT", "POLICY_BLOCKED"])
def test_should_auto_retry_non_transient(error_code: str) -> None:
    with _store() as store:
        job = _create_job(store, error_code=error_code)
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is False
        assert job.status == "failed"
        assert _events(store) == []


def test_should_auto_retry_max_reached() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT", retry_count=3)
        service = RetryService(store, RetryConfig(max_retries=3))

        assert service.should_auto_retry(job) is False
        assert job.status == "failed"
        assert _events(store) == []


def test_out_of_memory_auto_retries_within_retry_limit() -> None:
    with _store() as store:
        job = _create_job(store, error_code="OUT_OF_MEMORY", retry_count=2)
        service = RetryService(store, RetryConfig(max_retries=3))

        policy = service.retry_policy_for_job(job)
        retry = service.handle_failed_job(job)

        assert policy["classifier"] == "transient_slurm_runtime"
        assert policy["retryable"] is True
        assert policy["permanent"] is False
        assert policy["auto_retry"] is True
        assert retry.status == "pending"
        assert retry.retry_count == 3
        event = _events(store)[0]
        assert event.event_type == "retry"
        assert event.details["previous_error"] == "OUT_OF_MEMORY"
        assert event.details["failure"]["classifier"] == "transient_slurm_runtime"
        assert event.details["failure"]["retryable"] is True


def test_handle_failed_job_transient() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.handle_failed_job(job)

        store.session.refresh(job)
        assert retry.job_id == "job_1_retry_1"
        assert retry.status == "pending"
        assert retry.retry_count == 1
        assert job.status == "failed"
        assert job.retry_count == 0


def test_handle_failed_job_non_transient() -> None:
    with _store() as store:
        job = _create_job(store, error_code="INVALID_MANIFEST")
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"


def test_handle_failed_job_exhausted() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT", retry_count=3)
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"


def test_out_of_memory_exhausted_retry_limit_becomes_permanent() -> None:
    with _store() as store:
        job = _create_job(store, error_code="OUT_OF_MEMORY", retry_count=3)
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.handle_failed_job(job)

        assert updated.status == "permanently_failed"
        event = _events(store)[0]
        assert event.event_type == "permanently_failed"
        assert event.details["failure"]["classifier"] == "transient_slurm_runtime"
        assert event.details["failure"]["retryable"] is False
        assert event.details["failure"]["permanent"] is True
        assert event.details["failure"]["limit_exhausted"] is True


def test_schedule_auto_retry() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.schedule_auto_retry(job)

        store.session.refresh(job)
        assert retry.job_id != job.job_id
        assert retry.job_id == "job_1_retry_1"
        assert retry.run_id == job.run_id
        assert retry.cycle_id == job.cycle_id
        assert retry.job_type == job.job_type
        assert retry.model_id == job.model_id
        assert retry.stage == job.stage
        assert retry.retry_count == 1
        assert retry.status == "pending"
        assert retry.slurm_job_id is None
        assert job.status == "failed"
        assert job.retry_count == 0
        assert job.slurm_job_id == "123"
        event = _events(store)[0]
        assert event.event_type == "retry"
        assert event.status_from == "failed"
        assert event.status_to == "pending"


def test_schedule_auto_retry_reuses_submission_failed_retry_without_slurm_binding() -> None:
    with _store() as store:
        job = _create_job(store, error_code="NODE_FAILURE", retry_count=1)
        stale_retry = _create_job(
            store,
            job_id="job_1_retry_2",
            run_id=job.run_id,
            status="submission_failed",
            error_code="SUBMIT_INTERRUPTED",
            retry_count=2,
        )
        stale_retry.slurm_job_id = None
        stale_retry.array_task_id = None
        stale_retry.started_at = datetime(2026, 5, 1, 1, tzinfo=UTC)
        stale_retry.finished_at = datetime(2026, 5, 1, 2, tzinfo=UTC)
        stale_retry.exit_code = 1
        stale_retry.error_message = "submission interrupted"
        stale_retry.log_uri = "s3://logs/stale"
        store.session.add(stale_retry)
        store.session.commit()
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.schedule_auto_retry(job)

        assert retry.job_id == "job_1_retry_2"
        assert retry.status == "pending"
        assert retry.retry_count == 2
        assert retry.slurm_job_id is None
        assert retry.array_task_id is None
        assert retry.started_at is None
        assert retry.finished_at is None
        assert retry.exit_code is None
        assert retry.error_code is None
        assert retry.error_message is None
        assert retry.log_uri is None
        assert [candidate.job_id for candidate in store.query_jobs_by_run(job.run_id)] == [
            "job_1",
            "job_1_retry_2",
        ]
        event = _events(store)[0]
        assert event.event_type == "retry"
        assert event.details["reused_existing_retry_job"] is True
        assert event.details["previous_job_id"] == "job_1"


def test_schedule_auto_retry_does_not_reuse_retry_with_slurm_binding() -> None:
    with _store() as store:
        job = _create_job(store, error_code="NODE_FAILURE", retry_count=1)
        existing_retry = _create_job(
            store,
            job_id="job_1_retry_2",
            run_id=job.run_id,
            status="submission_failed",
            error_code="SUBMIT_INTERRUPTED",
            retry_count=2,
        )
        existing_retry.slurm_job_id = "slurm_existing_retry"
        store.session.add(existing_retry)
        store.session.commit()
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryError) as exc_info:
            service.schedule_auto_retry(job)

        assert exc_info.value.code == "AUTO_RETRY_JOB_CONFLICT"
        assert exc_info.value.details["retry_job_id"] == "job_1_retry_2"
        assert exc_info.value.details["existing_slurm_job_id"] == "slurm_existing_retry"


def test_mark_permanently_failed() -> None:
    with _store() as store:
        job = _create_job(store, error_code="SLURM_TIMEOUT", retry_count=3)
        service = RetryService(store, RetryConfig(max_retries=3))

        updated = service.mark_permanently_failed(job)

        assert updated.status == "permanently_failed"
        event = _events(store)[0]
        assert event.event_type == "permanently_failed"
        assert event.status_from == "failed"
        assert event.status_to == "permanently_failed"
        assert event.details["final_retry_count"] == 3
        assert event.details["last_error"] == "SLURM_TIMEOUT"
        assert event.details["automatic_retry_stopped"] is True
        assert event.details["failure"]["classifier"] == "transient_slurm_runtime"
        assert event.details["failure"]["limit_exhausted"] is True


def test_manual_retry_creates_new_job() -> None:
    with _store() as store:
        original = _create_job(store, run_id="run_1", error_code="NODE_FAILURE", retry_count=2)
        gateway = _RecordingGateway(job_id="slurm_retry_1")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("run_1", gateway=gateway, trusted_internal=True)

        store.session.refresh(original)
        assert retry.job_id != original.job_id
        assert retry.job_id.startswith("run_1_retry_")
        assert retry.run_id == original.run_id
        assert retry.cycle_id == original.cycle_id
        assert retry.job_type == original.job_type
        assert retry.model_id == original.model_id
        assert retry.stage == original.stage
        assert retry.status == "submitted"
        assert retry.retry_count == 3
        assert retry.slurm_job_id == "slurm_retry_1"
        assert retry.error_code is None
        assert original.status == "failed"
        assert original.retry_count == 2
        assert original.slurm_job_id == "123"


def test_manual_retry_without_gateway_raises_execution_unavailable() -> None:
    with _store() as store:
        _create_job(store, run_id="run_1", error_code="NODE_FAILURE", retry_count=2)
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryError) as exc_info:
            service.attempt_manual_retry("run_1", trusted_internal=True)

        assert exc_info.value.code == "RETRY_EXECUTION_UNAVAILABLE"
        assert store.query_jobs_by_run("run_1")[0].status == "failed"


def test_manual_retry_submits_to_slurm_when_gateway_available() -> None:
    with _store() as store:
        failed = _create_job(store, run_id="run_1", error_code="NODE_FAILURE", retry_count=2)
        gateway = _RecordingGateway(job_id="slurm_retry_1")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("run_1", gateway=gateway, trusted_internal=True)

        assert retry.status == "submitted"
        assert retry.slurm_job_id == "slurm_retry_1"
        assert retry.submitted_at is not None
        assert gateway.submissions[0].run_id == "run_1"
        assert gateway.submissions[0].model_id == failed.model_id
        assert gateway.submissions[0].job_type == failed.job_type
        events = _events(store)
        assert [event.event_type for event in events] == ["retry", "submission"]
        assert events[-1].status_to == "submitted"
        assert events[-1].details["slurm_job_id"] == "slurm_retry_1"


def test_manual_retry_download_source_cycle_submits_source_and_cycle_time() -> None:
    with _store() as store:
        job = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(
            store,
            job,
            {
                "workspace_dir": "/srv/nhms/workspace",
                "object_store_root": "/srv/nhms/object-store",
                "object_store_prefix": "s3://nhms-prod",
            },
        )
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        assert retry.status == "submitted"
        submission = gateway.submissions[0]
        assert submission.run_id == "cycle_ifs_2026053106"
        assert submission.job_type == "download_source_cycle"
        assert submission.manifest["cycle_id"] == "ifs_2026053106"
        assert submission.manifest["source_id"] == "ifs"
        assert submission.manifest["cycle_time"] == "2026053106"
        assert submission.manifest["pipeline_job_id"] == retry.job_id
        assert submission.manifest["object_store_root"] == "/srv/nhms/object-store"
        assert submission.manifest["object_store_prefix"] == "s3://nhms-prod"


def test_manual_retry_download_source_cycle_preserves_runtime_roots_from_original_event() -> None:
    with _store() as store:
        job = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(
            store,
            job,
            {
                "workspace_dir": "/srv/nhms/workspace",
                "object_store_root": "/srv/nhms/object-store",
                "object_store_prefix": "s3://nhms-prod",
                "published_artifact_root": "/srv/nhms/published",
                "published_artifact_uri_prefix": "published://prod",
            },
        )
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        submission = gateway.submissions[0]
        assert retry.status == "submitted"
        assert submission.run_id == "cycle_ifs_2026053106"
        assert submission.job_type == "download_source_cycle"
        assert submission.manifest == {
            "run_id": "cycle_ifs_2026053106",
            "model_id": "model_a",
            "cycle_id": "ifs_2026053106",
            "job_type": "download_source_cycle",
            "stage": "download",
            "pipeline_job_id": retry.job_id,
            "retry_count": 2,
            "manual_retry_marker": True,
            "workspace_dir": "/srv/nhms/workspace",
            "object_store_root": "/srv/nhms/object-store",
            "object_store_prefix": "s3://nhms-prod",
            "published_artifact_root": "/srv/nhms/published",
            "published_artifact_uri_prefix": "published://prod",
            "source_id": "ifs",
            "cycle_time": "2026053106",
        }
        event = _events(store)[-1]
        assert event.status_to == "submitted"
        evidence = event.details["runtime_root_resolution"]
        assert evidence["resolved"]["object_store_root"]["value"] == "/srv/nhms/object-store"
        assert evidence["resolved"]["object_store_root"]["same_as_workspace"] is False
        assert evidence["resolved"]["object_store_prefix"]["value"] == "s3://nhms-prod"
        assert event.details["runtime_root_contract"]["object_store_root"] == "/srv/nhms/object-store"
        assert event.details["runtime_root_contract"]["object_store_prefix"] == "s3://nhms-prod"
        assert evidence["published_fields_available"] == [
            "published_artifact_root",
            "published_artifact_uri_prefix",
        ]


def test_manual_retry_download_source_cycle_missing_object_store_root_fails_closed_without_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_root_env(monkeypatch)
    with _store() as store:
        job = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(store, job, {"workspace_dir": "/srv/nhms/workspace"})
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        assert gateway.submissions == []
        assert retry.status == "submission_failed"
        assert retry.slurm_job_id is None
        assert retry.error_code == "RETRY_RUNTIME_ROOTS_UNRESOLVED"
        assert "object-store runtime roots" in str(retry.error_message)
        event = _events(store)[-1]
        assert event.status_to == "submission_failed"
        evidence = event.details["runtime_root_resolution"]
        assert evidence["missing"] == ["object_store_root"]
        assert evidence["resolved"]["workspace_dir"]["value"] == "/srv/nhms/workspace"


def test_manual_retry_download_source_cycle_gateway_failure_persists_runtime_root_contract() -> None:
    with _store() as store:
        job = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(
            store,
            job,
            {
                "workspace_dir": "/srv/nhms/workspace",
                "object_store_root": "/srv/nhms/object-store",
                "object_store_prefix": "s3://nhms-prod",
            },
        )
        gateway = _RecordingGateway(error=RuntimeError("sbatch unavailable"))
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        assert retry.status == "submission_failed"
        assert len(gateway.submissions) == 1
        event = _events(store)[-1]
        assert event.status_to == "submission_failed"
        assert event.details["runtime_root_resolution"]["resolved"]["object_store_root"]["value"] == (
            "/srv/nhms/object-store"
        )
        assert event.details["runtime_root_contract"]["workspace_dir"] == "/srv/nhms/workspace"
        assert event.details["runtime_root_contract"]["object_store_root"] == "/srv/nhms/object-store"
        assert event.details["runtime_root_contract"]["object_store_prefix"] == "s3://nhms-prod"


def test_manual_retry_download_source_cycle_uses_explicit_runtime_config_after_legacy_workspace_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _store() as store:
        job = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(
            store,
            job,
            {
                "workspace_dir": "/srv/nhms/workspace",
                "object_store_root": "/srv/nhms/workspace",
                "object_store_prefix": "legacy-prefix",
            },
        )
        monkeypatch.setenv("WORKSPACE_ROOT", "/srv/nhms/workspace")
        monkeypatch.setenv("OBJECT_STORE_ROOT", "/srv/nhms/object-store")
        monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms-prod")
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        assert retry.status == "submitted"
        submission = gateway.submissions[0]
        assert submission.manifest["workspace_dir"] == "/srv/nhms/workspace"
        assert submission.manifest["object_store_root"] == "/srv/nhms/object-store"
        assert submission.manifest["object_store_prefix"] == "s3://nhms-prod"
        evidence = _events(store)[-1].details["runtime_root_resolution"]
        assert evidence["resolved"]["workspace_dir"]["source"] == "runtime_config:environment"
        assert evidence["resolved"]["object_store_root"]["source"] == "runtime_config:environment"
        assert evidence["resolved"]["object_store_root"]["same_as_workspace"] is False
        assert any(item["reason"] == "resolves_to_workspace_dir" for item in evidence["rejected"])


def test_manual_retry_download_source_cycle_rejects_object_store_root_path_alias_to_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clear_runtime_root_env(monkeypatch)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    alias_root = workspace_root.parent / "workspace-alias" / ".." / "workspace"
    with _store() as store:
        job = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(
            store,
            job,
            {
                "workspace_dir": str(workspace_root),
                "object_store_root": str(alias_root),
                "object_store_prefix": "s3://nhms-prod",
            },
        )
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        assert gateway.submissions == []
        assert retry.status == "submission_failed"
        assert retry.error_code == "RETRY_RUNTIME_ROOTS_UNSAFE"
        evidence = _events(store)[-1].details["runtime_root_resolution"]
        assert evidence["missing"] == ["object_store_root"]
        assert any(
            item["field"] == "object_store_root" and item["reason"] == "resolves_to_workspace_dir"
            for item in evidence["rejected"]
        )


def test_manual_retry_download_source_cycle_rejects_object_store_root_symlink_alias_to_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clear_runtime_root_env(monkeypatch)
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    alias_root = tmp_path / "workspace-link"
    alias_root.symlink_to(workspace_root, target_is_directory=True)
    with _store() as store:
        job = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(
            store,
            job,
            {
                "workspace_dir": str(workspace_root),
                "object_store_root": str(alias_root),
                "object_store_prefix": "s3://nhms-prod",
            },
        )
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        assert gateway.submissions == []
        assert retry.status == "submission_failed"
        assert retry.error_code == "RETRY_RUNTIME_ROOTS_UNSAFE"
        evidence = _events(store)[-1].details["runtime_root_resolution"]
        assert any(
            item["field"] == "object_store_root" and item["reason"] == "resolves_to_workspace_dir"
            for item in evidence["rejected"]
        )


def test_manual_retry_download_source_cycle_preserves_uri_style_object_store_root_without_path_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_root_env(monkeypatch)
    with _store() as store:
        job = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(
            store,
            job,
            {
                "workspace_dir": "/srv/nhms/workspace",
                "object_store_root": "s3://nhms-prod/raw-root",
                "object_store_prefix": "s3://nhms-prod/raw",
            },
        )
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        assert retry.status == "submitted"
        assert gateway.submissions[0].manifest["object_store_root"] == "s3://nhms-prod/raw-root"
        evidence = _events(store)[-1].details["runtime_root_resolution"]
        assert evidence["resolved"]["object_store_root"]["same_as_workspace"] is False
        assert evidence["rejected"] == []


def test_manual_retry_download_source_cycle_recovers_original_contract_through_stale_retry_provenance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_root_env(monkeypatch)
    with _store() as store:
        original = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(
            store,
            original,
            {
                "workspace_dir": "/srv/nhms/workspace",
                "object_store_root": "/srv/nhms/object-store",
                "object_store_prefix": "s3://nhms-prod",
            },
        )
        stale_retry = _create_job(
            store,
            job_id="cycle_ifs_2026053106_retry_active",
            run_id="cycle_ifs_2026053106",
            status="submission_failed",
            error_code="SBATCH_SUBMISSION_FAILED",
            retry_count=2,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        stale_retry.manual_retry_marker = True
        stale_retry.slurm_job_id = None
        store.session.add(stale_retry)
        store.insert_event(
            entity_type="pipeline_job",
            entity_id=stale_retry.job_id,
            event_type="retry",
            status_from="failed",
            status_to="pending",
            details={"trigger": "manual", "previous_job_id": original.job_id},
        )
        store.session.commit()
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        assert retry.status == "submitted"
        assert retry.job_id == "cycle_ifs_2026053106_retry_2"
        submission = gateway.submissions[0]
        assert submission.manifest["workspace_dir"] == "/srv/nhms/workspace"
        assert submission.manifest["object_store_root"] == "/srv/nhms/object-store"
        assert submission.manifest["object_store_prefix"] == "s3://nhms-prod"
        evidence = _events(store)[-1].details["runtime_root_resolution"]
        assert evidence["resolved"]["object_store_root"]["source"].startswith("pipeline_event:submission:")


def test_manual_retry_download_source_cycle_ignores_stale_manual_retry_contract_for_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_root_env(monkeypatch)
    with _store() as store:
        original = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(
            store,
            original,
            {
                "workspace_dir": "/srv/original/workspace",
                "object_store_root": "/srv/original/object-store",
                "object_store_prefix": "s3://nhms-original",
            },
        )
        stale_retry = _create_job(
            store,
            job_id="cycle_ifs_2026053106_retry_active",
            run_id="cycle_ifs_2026053106",
            status="submission_failed",
            error_code="SBATCH_SUBMISSION_FAILED",
            retry_count=2,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        stale_retry.manual_retry_marker = True
        stale_retry.slurm_job_id = None
        store.session.add(stale_retry)
        store.insert_event(
            entity_type="pipeline_job",
            entity_id=stale_retry.job_id,
            event_type="retry",
            status_from="failed",
            status_to="pending",
            details={"trigger": "manual", "previous_job_id": original.job_id},
        )
        _insert_submission_event(
            store,
            stale_retry,
            {
                "workspace_dir": "/srv/stale/workspace",
                "object_store_root": "/srv/stale/object-store",
                "object_store_prefix": "s3://nhms-stale",
            },
        )
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        assert retry.status == "submitted"
        submission = gateway.submissions[0]
        assert submission.manifest["workspace_dir"] == "/srv/original/workspace"
        assert submission.manifest["object_store_root"] == "/srv/original/object-store"
        assert submission.manifest["object_store_prefix"] == "s3://nhms-original"
        assert "stale" not in json.dumps(submission.manifest, sort_keys=True)
        evidence = _events(store)[-1].details["runtime_root_resolution"]
        assert evidence["resolved"]["object_store_root"]["value"] == "/srv/original/object-store"
        assert evidence["candidate_counts"]["manual_retry_event_rows_ignored"] == 1


def test_manual_retry_download_source_cycle_recovers_original_same_run_submission_before_env_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _store() as store:
        original = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(
            store,
            original,
            {
                "workspace_dir": "/srv/original/workspace",
                "object_store_root": "/srv/original/object-store",
                "object_store_prefix": "s3://nhms-original",
            },
        )
        stale_retry = _create_job(
            store,
            job_id="cycle_ifs_2026053106_retry_active",
            run_id="cycle_ifs_2026053106",
            status="submission_failed",
            error_code="SBATCH_SUBMISSION_FAILED",
            retry_count=2,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        stale_retry.manual_retry_marker = True
        stale_retry.slurm_job_id = None
        store.session.add(stale_retry)
        store.session.commit()
        monkeypatch.setenv("WORKSPACE_ROOT", "/srv/current/workspace")
        monkeypatch.setenv("OBJECT_STORE_ROOT", "/srv/current/object-store")
        monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms-current")
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        assert retry.status == "submitted"
        submission = gateway.submissions[0]
        assert submission.manifest["workspace_dir"] == "/srv/original/workspace"
        assert submission.manifest["object_store_root"] == "/srv/original/object-store"
        assert submission.manifest["object_store_prefix"] == "s3://nhms-original"


def test_manual_retry_download_source_cycle_rejects_relative_env_roots_before_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _store() as store:
        job = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(store, job, {"workspace_dir": "/srv/nhms/workspace"})
        monkeypatch.setenv("WORKSPACE_ROOT", "/srv/nhms/workspace")
        monkeypatch.setenv("OBJECT_STORE_ROOT", "../object-store")
        monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms-prod")
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        assert gateway.submissions == []
        assert retry.status == "submission_failed"
        assert retry.error_code == "RETRY_RUNTIME_ROOTS_UNSAFE"
        evidence = _events(store)[-1].details["runtime_root_resolution"]
        assert any(
            item["field"] == "object_store_root" and item["reason"] == "parent_traversal_local_root"
            for item in evidence["rejected"]
        )


def test_manual_retry_download_source_cycle_rejects_mixed_legacy_event_and_incomplete_env_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _store() as store:
        job = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(
            store,
            job,
            {
                "workspace_dir": "/srv/legacy/workspace",
                "object_store_root": "/srv/legacy/workspace",
                "object_store_prefix": "legacy-prefix",
            },
        )
        monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
        monkeypatch.setenv("OBJECT_STORE_ROOT", "/srv/current/object-store")
        monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms-prod")
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        assert gateway.submissions == []
        assert retry.status == "submission_failed"
        assert retry.error_code == "RETRY_RUNTIME_ROOTS_UNSAFE"
        evidence = _events(store)[-1].details["runtime_root_resolution"]
        assert evidence["resolved"]["object_store_prefix"]["value"] == "legacy-prefix"
        assert any(item["reason"] == "resolves_to_workspace_dir" for item in evidence["rejected"])


def test_manual_retry_download_source_cycle_bounds_runtime_root_rejection_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_root_env(monkeypatch)
    with _store() as store:
        job = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        unsafe_contract = {
            "workspace_dir": "/srv/nhms/workspace",
            "object_store_root": "/srv/nhms/workspace",
            "object_store_prefix": "s3://nhms-prod",
        }
        candidate_paths = (
            ("runtime_root_contract",),
            ("submission_manifest",),
            ("submitted_manifest",),
            ("request_manifest",),
            ("slurm_submission_manifest",),
            ("manifest",),
            ("gateway_response", "manifest"),
            ("slurm", "manifest"),
        )
        for index in range(5):
            details: dict[str, Any] = {
                "stage": job.stage,
                "job_type": job.job_type,
                "event_index": index,
            }
            for path in candidate_paths:
                cursor = details
                for key in path[:-1]:
                    cursor = cursor.setdefault(key, {})
                cursor[path[-1]] = dict(unsafe_contract)
            store.insert_event(
                entity_type="pipeline_job",
                entity_id=job.job_id,
                event_type="submission",
                status_from=None,
                status_to=job.status,
                details=details,
            )
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("cycle_ifs_2026053106", gateway=gateway, trusted_internal=True)

        assert gateway.submissions == []
        assert retry.status == "submission_failed"
        assert retry.error_code == "RETRY_RUNTIME_ROOTS_UNSAFE"
        evidence = _events(store)[-1].details["runtime_root_resolution"]
        counts = evidence["candidate_counts"]
        assert counts["event_candidates_returned"] == counts["event_candidate_limit"]
        assert counts["event_candidates_total"] > counts["event_candidates_returned"]
        assert counts["event_candidates_omitted"] == (
            counts["event_candidates_total"] - counts["event_candidates_returned"]
        )
        assert len(evidence["rejected"]) == evidence["rejected_limit"]
        assert evidence["rejected_total_count"] > len(evidence["rejected"])
        assert evidence["rejected_omitted_count"] == (
            evidence["rejected_total_count"] - len(evidence["rejected"])
        )


def test_manual_retry_download_source_cycle_redacts_secret_runtime_root_evidence_and_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_root_env(monkeypatch)
    secret_root = "https://user:secret-pass@example.com/object-store?token=secret-token"
    secret_prefix = "s3://bucket/prod?X-Amz-Signature=secret-sig"
    with _store() as store:
        job = _create_job(
            store,
            job_id="job_cycle_ifs_2026053106_download",
            run_id="cycle_ifs_2026053106",
            error_code="NODE_FAILURE",
            retry_count=1,
            cycle_id="ifs_2026053106",
            job_type="download_source_cycle",
            stage="download",
        )
        _insert_submission_event(
            store,
            job,
            {
                "workspace_dir": "/srv/nhms/workspace",
                "object_store_root": secret_root,
                "object_store_prefix": secret_prefix,
            },
        )
        service = RetryService(store, RetryConfig(max_retries=3))
        gateway = _RecordingGateway(job_id="slurm_retry_ifs")
        app.dependency_overrides[pipeline_routes.get_retry_service] = lambda: service
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = lambda: gateway
        previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
        os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
        try:
            client = TestClient(app)

            response = client.post("/api/v1/runs/cycle_ifs_2026053106/retry", headers={"X-User-Role": "operator"})

            assert response.status_code == 503
            error = response.json()["error"]
            assert error["code"] == "RETRY_RUNTIME_ROOTS_SECRET_BEARING"
            assert error["details"]["status"] == "submission_failed"
            assert error["details"]["runtime_root_resolution"]["missing"] == ["object_store_root"]
            assert gateway.submissions == []
            event = _events(store)[-1]
            persisted = json.dumps({"message": event.message, "details": event.details}, sort_keys=True)
            response_body = json.dumps(response.json(), sort_keys=True)
            for raw_secret in (
                "user:secret-pass",
                "secret-pass",
                "secret-token",
                "secret-sig",
                "X-Amz-Signature=secret-sig",
            ):
                assert raw_secret not in persisted
                assert raw_secret not in response_body
            assert "https://example.com/object-store" in persisted
            assert "s3://bucket/prod" in persisted
            assert "[redacted]" not in persisted
        finally:
            if previous_allow_dev_role_header is None:
                os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
            else:
                os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
            app.dependency_overrides.pop(pipeline_routes.get_retry_service, None)
            app.dependency_overrides.pop(pipeline_routes.get_slurm_gateway, None)


def test_manual_retry_submission_failure_marks_submission_failed() -> None:
    with _store() as store:
        _create_job(store, run_id="run_1", error_code="NODE_FAILURE")
        gateway = _RecordingGateway(error=RuntimeError("sbatch unavailable"))
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("run_1", gateway=gateway, trusted_internal=True)

        assert retry.status == "submission_failed"
        assert retry.slurm_job_id is None
        assert retry.error_code == "SBATCH_SUBMISSION_FAILED"
        assert retry.error_message == "sbatch unavailable"
        assert _events(store)[-1].status_to == "submission_failed"


def test_manual_retry_submission_failure_redacts_persisted_event_and_api_error() -> None:
    secret_message = (
        "sbatch failed for https://alice:pass123@slurm.example/sbatch?"
        "X-Amz-Signature=sig123&token=tok123 token=tok123 password=pass123 "
        "Authorization: Bearer live-token-123 authorization=Basic basic-secret-123 "
        "{\"Authorization\": \"Bearer json-retry-token-123\"} "
        "Proxy-Authorization='Basic proxy-retry-secret-123' "
        "stderr=\"Bearer bare-retry-token-123\" Basic bare-basic-retry-secret-123; next field"
    )
    with _store() as store:
        _create_job(store, run_id="run_api_secret", error_code="SLURM_UNAVAILABLE")
        service = RetryService(store, RetryConfig(max_retries=3))
        gateway = _RecordingGateway(error=RuntimeError(secret_message))
        app.dependency_overrides[pipeline_routes.get_retry_service] = lambda: service
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = lambda: gateway
        previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
        os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
        try:
            client = TestClient(app)

            response = client.post("/api/v1/runs/run_api_secret/retry", headers={"X-User-Role": "operator"})

            assert response.status_code == 503
            error = response.json()["error"]
            assert error["code"] == "SBATCH_SUBMISSION_FAILED"
            assert error["details"]["run_id"] == "run_api_secret"
            assert error["details"]["status"] == "submission_failed"
            assert error["details"]["job_id"] == error["details"]["pipeline_job_id"]
            event = _events(store)[-1]
            assert event.status_to == "submission_failed"
            assert event.details["error_code"] == "SBATCH_SUBMISSION_FAILED"
            persisted = json.dumps({"message": event.message, "details": event.details}, sort_keys=True)
            response_body = json.dumps(response.json(), sort_keys=True)
            for raw_secret in (
                "alice:pass123",
                "pass123",
                "sig123",
                "tok123",
                "live-token-123",
                "basic-secret-123",
                "json-retry-token-123",
                "proxy-retry-secret-123",
                "bare-retry-token-123",
                "bare-basic-retry-secret-123",
            ):
                assert raw_secret not in persisted
                assert raw_secret not in response_body
            assert "[redacted]" in persisted
            assert "[redacted]" in response_body
        finally:
            if previous_allow_dev_role_header is None:
                os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
            else:
                os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
            app.dependency_overrides.pop(pipeline_routes.get_retry_service, None)
            app.dependency_overrides.pop(pipeline_routes.get_slurm_gateway, None)


def test_manual_retry_conflict_409() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_1", status="failed")
        _create_job(store, job_id="job_pending", run_id="run_1", status="pending")
        gateway = _RecordingGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryConflictError) as exc_info:
            service.attempt_manual_retry("run_1", gateway=gateway, trusted_internal=True)

        assert exc_info.value.message == "A retry is already in progress for this run."
        assert exc_info.value.details["active_job_id"] == "job_pending"
        assert len(store.query_jobs_by_run("run_1")) == 2


def test_manual_retry_queued_retry_marker_blocks_second_manual_retry_without_submission() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_queued_marker", status="failed")
        active_retry = _create_job(
            store,
            job_id="run_queued_marker_retry_active",
            run_id="run_queued_marker",
            status="queued",
            error_code=None,
            retry_count=1,
        )
        active_retry.manual_retry_marker = True
        store.session.add(active_retry)
        store.session.commit()
        gateway = _RecordingGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryConflictError) as exc_info:
            service.attempt_manual_retry("run_queued_marker", gateway=gateway, trusted_internal=True)

        assert exc_info.value.details["active_job_id"] == "run_queued_marker_retry_active"
        assert exc_info.value.details["active_status"] == "queued"
        assert gateway.submissions == []
        assert [job.job_id for job in store.query_jobs_by_run("run_queued_marker")] == [
            "job_failed",
            "run_queued_marker_retry_active",
        ]


def test_second_manual_retry_attempt_gets_conflict() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_1", status="failed")
        gateway = _RecordingGateway(job_id="slurm_retry_1")
        service = RetryService(store, RetryConfig(max_retries=3))

        first = service.attempt_manual_retry("run_1", gateway=gateway, trusted_internal=True)
        with pytest.raises(RetryConflictError) as exc_info:
            service.attempt_manual_retry("run_1", gateway=gateway, trusted_internal=True)

        assert exc_info.value.details["active_job_id"] == first.job_id
        assert exc_info.value.details["active_status"] == "submitted"


def test_manual_retry_duplicate_guard_failure_returns_conflict_without_submission() -> None:
    with _store() as store:
        failed = _create_job(store, job_id="job_failed", run_id="run_guard", status="failed")
        # Fast CI runs SQLite, where SELECT FOR UPDATE does not emulate a true PostgreSQL race.
        # This deterministic fixture covers the durable guard failure path that a duplicate
        # active retry insert would take after the first transaction wins the guard row.
        gateway = _RecordingGateway()
        service = RetryService(store, RetryConfig(max_retries=3))
        original_create_job = store.create_job

        def race_create_job(**kwargs):
            if kwargs["manual_retry_marker"] is True:
                original_create_job(**kwargs)
                raise IntegrityError("duplicate active retry guard", params=None, orig=RuntimeError("duplicate"))
            return original_create_job(**kwargs)

        store.create_job = race_create_job  # type: ignore[method-assign]

        with pytest.raises(RetryConflictError) as exc_info:
            service.attempt_manual_retry("run_guard", gateway=gateway, trusted_internal=True)

        assert exc_info.value.details["run_id"] == "run_guard"
        assert exc_info.value.details["active_job_id"] == "run_guard_retry_active"
        assert gateway.submissions == []
        assert [job.job_id for job in store.query_jobs_by_run("run_guard")] == ["job_failed"]
        assert failed.status == "failed"


def test_manual_retry_stale_terminal_guard_row_allows_next_retry_without_random_guard_bypass() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_stale_guard", status="failed", error_code="NODE_FAILURE")
        stale_guard = _create_job(
            store,
            job_id="run_stale_guard_retry_active",
            run_id="run_stale_guard",
            status="submission_failed",
            error_code="SBATCH_SUBMISSION_FAILED",
            retry_count=1,
        )
        stale_guard.manual_retry_marker = True
        store.session.add(stale_guard)
        store.session.commit()
        gateway = _RecordingGateway(job_id="slurm_retry_after_stale_guard")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("run_stale_guard", gateway=gateway, trusted_internal=True)

        assert retry.job_id == "run_stale_guard_retry_2"
        assert retry.manual_retry_marker is True
        assert retry.status == "submitted"
        assert gateway.submissions[0].manifest["pipeline_job_id"] == "run_stale_guard_retry_2"
        retry_jobs = [job for job in store.query_jobs_by_run("run_stale_guard") if job.manual_retry_marker]
        active_retry_jobs = [job for job in retry_jobs if job.status in {"pending", "queued", "submitted", "running"}]
        assert {job.job_id for job in retry_jobs} == {"run_stale_guard_retry_active", "run_stale_guard_retry_2"}
        assert [job.job_id for job in active_retry_jobs] == ["run_stale_guard_retry_2"]


def test_manual_retry_run_level_guard_blocks_duplicate_attempt_after_stale_guard_row() -> None:
    with _store() as store:
        _create_job(
            store,
            job_id="job_failed",
            run_id="run_stale_duplicate",
            status="failed",
            error_code="NODE_FAILURE",
        )
        stale_guard = _create_job(
            store,
            job_id="run_stale_duplicate_retry_active",
            run_id="run_stale_duplicate",
            status="failed",
            error_code="RETRY_STALE_PENDING",
            retry_count=1,
        )
        stale_guard.manual_retry_marker = True
        store.session.add(stale_guard)
        store.session.commit()
        gateway = _RecordingGateway(job_id="slurm_retry")
        service = RetryService(store, RetryConfig(max_retries=3))

        first = service.attempt_manual_retry("run_stale_duplicate", gateway=gateway, trusted_internal=True)
        with pytest.raises(RetryConflictError) as exc_info:
            service.attempt_manual_retry("run_stale_duplicate", gateway=gateway, trusted_internal=True)

        assert first.job_id == "run_stale_duplicate_retry_2"
        assert exc_info.value.details["active_job_id"] == first.job_id
        assert exc_info.value.details["active_status"] == "submitted"
        assert len(gateway.submissions) == 1
        active_retry_jobs = [
            job
            for job in store.query_jobs_by_run("run_stale_duplicate")
            if job.manual_retry_marker and job.status in {"pending", "queued", "submitted", "running"}
        ]
        assert [job.job_id for job in active_retry_jobs] == [first.job_id]


def test_manual_retry_conflicts_with_submitted_job() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_1", status="failed")
        _create_job(store, job_id="job_submitted", run_id="run_1", status="submitted")
        gateway = _RecordingGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryConflictError) as exc_info:
            service.attempt_manual_retry("run_1", gateway=gateway, trusted_internal=True)

        assert exc_info.value.details["active_job_id"] == "job_submitted"
        assert exc_info.value.details["active_status"] == "submitted"


def test_manual_retry_no_failed_job() -> None:
    with _store() as store:
        _create_job(store, run_id="run_1", status="succeeded", error_code=None)
        gateway = _RecordingGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryNotFoundError):
            service.attempt_manual_retry("run_1", gateway=gateway, trusted_internal=True)


def test_manual_retry_rejects_older_failure_when_latest_truth_succeeded() -> None:
    with _store() as store:
        failed = _create_job(store, job_id="job_failed", run_id="run_latest_success", status="failed")
        succeeded = _create_job(
            store,
            job_id="job_succeeded",
            run_id="run_latest_success",
            status="succeeded",
            error_code=None,
        )
        failed.updated_at = datetime(2026, 5, 1, 6, 20, tzinfo=UTC)
        succeeded.updated_at = datetime(2026, 5, 1, 6, 40, tzinfo=UTC)
        store.session.add_all([failed, succeeded])
        store.session.commit()
        gateway = _RecordingGateway()
        service = RetryService(store, RetryConfig(max_retries=3))

        with pytest.raises(RetryNotFoundError):
            service.attempt_manual_retry("run_latest_success", gateway=gateway, trusted_internal=True)

        assert gateway.submissions == []
        assert [job.job_id for job in store.query_jobs_by_run("run_latest_success")] == [
            "job_failed",
            "job_succeeded",
        ]
        assert _events(store) == []


def test_expire_stale_retries_allows_new_retry() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_1", status="failed", error_code="NODE_FAILURE")
        pending = _create_job(
            store,
            job_id="job_pending",
            run_id="run_1",
            status="pending",
            error_code=None,
            retry_count=1,
        )
        pending.slurm_job_id = None
        pending.created_at = datetime(2026, 5, 1, tzinfo=UTC)
        store.session.add(pending)
        store.session.commit()
        gateway = _RecordingGateway(job_id="slurm_retry_1")
        service = RetryService(store, RetryConfig(max_retries=3))

        expired = service.expire_stale_retries(max_age_seconds=1)
        retry = service.attempt_manual_retry("run_1", gateway=gateway, trusted_internal=True)

        assert [job.job_id for job in expired] == ["job_pending"]
        assert expired[0].status == "failed"
        assert expired[0].error_code == "RETRY_STALE_PENDING"
        assert retry.status == "submitted"
        assert retry.job_id != "job_pending"
        assert _events(store)[0].event_type == "retry_expired"


def test_expire_stale_retries_ignores_non_retry_pending_jobs() -> None:
    with _store() as store:
        pending = _create_job(store, job_id="job_pending", run_id="run_1", status="pending", error_code=None)
        pending.slurm_job_id = None
        pending.created_at = datetime(2026, 5, 1, tzinfo=UTC)
        store.session.add(pending)
        store.session.commit()
        service = RetryService(store, RetryConfig(max_retries=3))

        expired = service.expire_stale_retries(max_age_seconds=1)

        assert expired == []
        store.session.refresh(pending)
        assert pending.status == "pending"


def test_audit_event_auto() -> None:
    with _store() as store:
        job = _create_job(store, error_code="STORAGE_WRITE_FAILED")
        service = RetryService(store, RetryConfig(max_retries=3))

        service.schedule_auto_retry(job)

        event = _events(store)[0]
        assert event.details["trigger"] == "auto"
        assert event.details["retry_count"] == 1
        assert event.details["previous_error"] == "STORAGE_WRITE_FAILED"
        assert event.details["backoff_seconds"] == 60
        assert event.details["previous_job_id"] == job.job_id
        assert event.details["slurm_job_id"] is None
        assert event.details["failure"]["classifier"] == "transient_slurm_runtime"


def test_audit_event_manual() -> None:
    with _store() as store:
        failed = _create_job(store, run_id="run_1", error_code="SBATCH_SUBMISSION_FAILED")
        gateway = _RecordingGateway(job_id="slurm_retry_1")
        service = RetryService(store, RetryConfig(max_retries=3))

        service.attempt_manual_retry("run_1", gateway=gateway, trusted_internal=True)

        event = _events(store)[0]
        assert event.details["trigger"] == "manual"
        assert event.details["retry_count"] == 1
        assert event.details["previous_error"] == "SBATCH_SUBMISSION_FAILED"
        assert event.details["previous_job_id"] == failed.job_id
        assert event.details["slurm_job_id"] is None
        assert event.details["manual_retry_marker"] is True
        assert event.details["prior_failure_reason"] == "SBATCH_SUBMISSION_FAILED"
        assert event.details["failure"]["manual_retry_marker"] is True


def test_manual_retry_audit_has_previous_job_id() -> None:
    with _store() as store:
        failed = _create_job(store, run_id="run_1", error_code="SBATCH_SUBMISSION_FAILED")
        gateway = _RecordingGateway(job_id="slurm_retry_1")
        service = RetryService(store, RetryConfig(max_retries=3))

        retry = service.attempt_manual_retry("run_1", gateway=gateway, trusted_internal=True)

        event = _events(store)[0]
        assert event.entity_id == retry.job_id
        assert event.details["previous_job_id"] == failed.job_id


def test_retry_api_endpoint() -> None:
    with _store() as store:
        failed = _create_job(store, run_id="run_api", error_code="SLURM_UNAVAILABLE")
        service = RetryService(store, RetryConfig(max_retries=3))
        app.dependency_overrides[pipeline_routes.get_retry_service] = lambda: service
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = lambda: _RecordingGateway(job_id="slurm_api_1")
        previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
        os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
        try:
            client = TestClient(app)
            headers = {"X-User-Role": "operator"}

            response = client.post("/api/v1/runs/run_api/retry", headers=headers)
            assert response.status_code == 200
            assert response.json()["status"] == "ok"
            data = response.json()["data"]
            assert data == {
                "job_id": data["job_id"],
                "pipeline_job_id": data["job_id"],
                "run_id": "run_api",
                "retry_count": 1,
                "status": "submitted",
                "slurm_job_id": "slurm_api_1",
                "execution_status": "submitted",
            }
            assert data["job_id"].startswith("run_api_retry_")
            store.session.refresh(failed)
            assert failed.status == "failed"

            conflict = client.post("/api/v1/runs/run_api/retry", headers=headers)
            assert conflict.status_code == 409
            assert conflict.json()["error"]["code"] == "RETRY_CONFLICT"
            assert conflict.json()["error"]["message"] == "A retry is already in progress for this run."
            assert conflict.json()["error"]["details"]["run_id"] == "run_api"
            assert "active_job_id" in conflict.json()["error"]["details"]

            missing = client.post("/api/v1/runs/missing/retry", headers=headers)
            assert missing.status_code == 404
            assert missing.json()["error"]["code"] == "RETRY_NOT_FOUND"
            assert missing.json()["error"]["message"] == "No retryable failure found for this run."
            assert missing.json()["error"]["details"]["run_id"] == "missing"

            invalid = client.post("/api/v1/runs/-bad/retry", headers=headers)
            assert invalid.status_code == 400
            assert invalid.json()["error"]["code"] == "INVALID_RUN_ID"
        finally:
            if previous_allow_dev_role_header is None:
                os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
            else:
                os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
            app.dependency_overrides.pop(pipeline_routes.get_retry_service, None)
            app.dependency_overrides.pop(pipeline_routes.get_slurm_gateway, None)


def test_retry_api_without_gateway_returns_503() -> None:
    with _store() as store:
        _create_job(store, run_id="run_api", error_code="SLURM_UNAVAILABLE")
        service = RetryService(store, RetryConfig(max_retries=3))
        app.dependency_overrides[pipeline_routes.get_retry_service] = lambda: service
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = lambda: _NoSubmitGateway()
        previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
        os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
        try:
            client = TestClient(app)

            response = client.post("/api/v1/runs/run_api/retry", headers={"X-User-Role": "operator"})

            assert response.status_code == 503
            assert response.json()["error"]["code"] == "RETRY_EXECUTION_UNAVAILABLE"
            assert response.json()["error"]["message"] == "Retry execution path unavailable."
            assert len(store.query_jobs_by_run("run_api")) == 1
        finally:
            if previous_allow_dev_role_header is None:
                os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
            else:
                os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
            app.dependency_overrides.pop(pipeline_routes.get_retry_service, None)
            app.dependency_overrides.pop(pipeline_routes.get_slurm_gateway, None)


def test_retry_api_submitted_response_contract() -> None:
    with _store() as store:
        _create_job(store, run_id="run_api", error_code="SLURM_UNAVAILABLE")
        service = RetryService(store, RetryConfig(max_retries=3))
        gateway = _RecordingGateway(job_id="slurm_api_1")
        app.dependency_overrides[pipeline_routes.get_retry_service] = lambda: service
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = lambda: gateway
        previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
        os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
        try:
            client = TestClient(app)

            response = client.post("/api/v1/runs/run_api/retry", headers={"X-User-Role": "operator"})

            assert response.status_code == 200
            data = response.json()["data"]
            assert data["status"] == "submitted"
            assert data["execution_status"] == "submitted"
            assert data["slurm_job_id"] == "slurm_api_1"
        finally:
            if previous_allow_dev_role_header is None:
                os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
            else:
                os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
            app.dependency_overrides.pop(pipeline_routes.get_retry_service, None)
            app.dependency_overrides.pop(pipeline_routes.get_slurm_gateway, None)


def test_retry_api_submission_error_response_contract() -> None:
    with _store() as store:
        _create_job(store, run_id="run_api", error_code="SLURM_UNAVAILABLE")
        service = RetryService(store, RetryConfig(max_retries=3))
        gateway = _RecordingGateway(error=RuntimeError("no execution path"))
        app.dependency_overrides[pipeline_routes.get_retry_service] = lambda: service
        app.dependency_overrides[pipeline_routes.get_slurm_gateway] = lambda: gateway
        previous_allow_dev_role_header = os.environ.get("ALLOW_DEV_ROLE_HEADER")
        os.environ["ALLOW_DEV_ROLE_HEADER"] = "true"
        try:
            client = TestClient(app)

            response = client.post("/api/v1/runs/run_api/retry", headers={"X-User-Role": "operator"})

            assert response.status_code == 503
            error = response.json()["error"]
            assert error["code"] == "SBATCH_SUBMISSION_FAILED"
            assert error["message"] == "no execution path"
            assert error["details"]["status"] == "submission_failed"
        finally:
            if previous_allow_dev_role_header is None:
                os.environ.pop("ALLOW_DEV_ROLE_HEADER", None)
            else:
                os.environ["ALLOW_DEV_ROLE_HEADER"] = previous_allow_dev_role_header
            app.dependency_overrides.pop(pipeline_routes.get_retry_service, None)
            app.dependency_overrides.pop(pipeline_routes.get_slurm_gateway, None)


def test_permanently_failed_override() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_1", status="failed")

        updated = store.update_job_status("job_failed", "permanently_failed")

        assert updated.status == "permanently_failed"


def test_permanently_failed_is_sticky() -> None:
    with _store() as store:
        _create_job(store, job_id="job_failed", run_id="run_1", status="permanently_failed")

        partial = store.update_job_status("job_failed", "partially_failed")
        running = store.update_job_status("job_failed", "running")

        assert partial.status == "permanently_failed"
        assert running.status == "permanently_failed"
        assert store.get_job("job_failed").status == "permanently_failed"


def _store() -> "_ClosingStore":
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_ops_schema(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS ops")

    Base.metadata.create_all(engine)
    return _ClosingStore(Session(engine))


class _ClosingStore(PipelineStore):
    def __init__(self, session: Session) -> None:
        super().__init__(session)

    def __enter__(self) -> PipelineStore:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.session.close()


class _RecordingGateway:
    def __init__(self, *, job_id: str = "slurm_retry", error: Exception | None = None) -> None:
        self.job_id = job_id
        self.error = error
        self.submissions = []

    def submit_job(self, request):
        self.submissions.append(request)
        if self.error is not None:
            raise self.error
        return {
            "job_id": self.job_id,
            "run_id": request.run_id,
            "model_id": request.model_id,
            "status": "submitted",
            "submitted_at": "2026-05-15T00:00:00Z",
            "updated_at": "2026-05-15T00:00:00Z",
        }


class _NoSubmitGateway:
    pass


def _create_job(
    store: PipelineStore,
    *,
    job_id: str = "job_1",
    run_id: str = "run_1",
    status: str = "failed",
    error_code: str | None = "SLURM_TIMEOUT",
    retry_count: int = 0,
    cycle_id: str = "gfs_2026050100",
    job_type: str = "run_shud_analysis",
    stage: str = "run",
) -> PipelineJob:
    job = store.create_job(
        job_id=job_id,
        run_id=run_id,
        cycle_id=cycle_id,
        job_type=job_type,
        slurm_job_id="123",
        model_id="model_a",
        stage=stage,
        status=status,
    )
    job.error_code = error_code
    job.error_message = f"{error_code} failed" if error_code else None
    job.retry_count = retry_count
    store.session.add(job)
    store.session.commit()
    store.session.refresh(job)
    return job


def _insert_submission_event(store: PipelineStore, job: PipelineJob, runtime_root_contract: dict[str, Any]) -> None:
    store.insert_event(
        entity_type="pipeline_job",
        entity_id=job.job_id,
        event_type="submission",
        status_from=None,
        status_to=job.status,
        details={
            "stage": job.stage,
            "job_type": job.job_type,
            "runtime_root_contract": runtime_root_contract,
        },
    )


def _clear_runtime_root_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "WORKSPACE_ROOT",
        "OBJECT_STORE_ROOT",
        "OBJECT_STORE_PREFIX",
        "NHMS_PUBLISHED_ARTIFACT_ROOT",
        "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
    ):
        monkeypatch.delenv(key, raising=False)


def _events(store: PipelineStore) -> list[PipelineEvent]:
    statement = select(PipelineEvent).order_by(PipelineEvent.event_id.asc())
    return list(store.session.scalars(statement))
