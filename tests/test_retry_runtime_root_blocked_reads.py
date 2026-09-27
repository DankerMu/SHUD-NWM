"""The manual-retry runtime-root walk after a read-blocked journal row (#2566, #2567).

Governing requirement (``openspec/changes/retry-blocked-reads-failed-stage-restart``,
job-retry-mechanism): every degrade of the walk is COUNTED, the resolution evidence
carries ``candidate_counts.blocked_reads`` whenever that count is non-zero (and only
then), and once any provenance read of an attempt was blocked no root or selector
value is taken from the current environment -- roots resolve from recorded
provenance or the attempt ends with the governed ``RETRY_RUNTIME_ROOTS_UNRESOLVED``.

Seam: the public ``FileJournalRetryService.attempt_manual_retry`` against a real
``FileOrchestrationJournalRepository``; the blocked row is produced by the journal's
own query entrypoints over the #2385/#2387 refusal seams.  Only the Slurm gateway is
a double.  Every service import is function-local for the reason given in
``tests/test_file_journal_read_blocked_consumers.py``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from tests.test_file_journal_read_blocked_consumers import (
    _BLOCK_FIELD,
    _BLOCK_REASON,
    _JOB_ID,
    _JOURNAL_LOGGER,
    _RUN_ID,
    _arm_predecessor_refusal_after_mint,
    _blocked_fault,
    _journal_records,
    _pipeline_job_record,
    _refuse_scoped_job_records,
    _seeded_journal,
    _SuccessGateway,
    _UnreachableGateway,
)

_COMPANION_JOB_ID = "job_fcst_gfs_2026072000_model_a_download"
_ENV_KEYS = (
    "WORKSPACE_ROOT",
    "OBJECT_STORE_ROOT",
    "OBJECT_STORE_PREFIX",
    "NHMS_PUBLISHED_ARTIFACT_ROOT",
    "NHMS_PUBLISHED_ARTIFACT_URI_PREFIX",
    "NHMS_SCHEDULER_DB_FREE_REQUIRED",
    "NHMS_SCHEDULER_ALLOWED_ROOTS",
    "NHMS_SCHEDULER_REGISTRY_BACKEND",
    "NHMS_SCHEDULER_REGISTRY_MANIFEST",
    "NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST",
    "NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND",
    "NHMS_SCHEDULER_CANONICAL_READINESS_INDEX",
    "NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX",
    "NHMS_SCHEDULER_STATE_INDEX_BACKEND",
    "NHMS_SCHEDULER_STATE_INDEX",
    "NHMS_SLURM_SCHEDULER_STATE_INDEX",
)


@pytest.fixture(autouse=True)
def _clean_runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _roots(tmp_path: Path, name: str) -> tuple[Path, Path]:
    workspace = (tmp_path / name / "workspace").resolve()
    object_store = (tmp_path / name / "object-store").resolve()
    workspace.mkdir(parents=True)
    object_store.mkdir(parents=True)
    return workspace, object_store


def _set_env_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """A COMPLETE current-environment candidate: the one a blocked read must not use."""

    workspace, object_store = _roots(tmp_path, "env")
    monkeypatch.setenv("WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_store))
    return workspace, object_store


def _record_submission_roots(repository: Any, job_id: str, workspace: Path, object_store: Path) -> None:
    """The job's ORIGINAL (non-manual) submission event, recording its runtime roots."""

    repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=job_id,
        event_type="submission",
        status_from="pending",
        status_to="submitted",
        details={
            "trigger": "scheduler",
            "runtime_root_contract": {
                "workspace_dir": str(workspace),
                "object_store_root": str(object_store),
                "object_store_prefix": "",
            },
        },
    )


def _spy_env_candidate(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count reads of the environment candidate the walk would append."""

    import services.orchestrator.file_orchestration_journal as journal

    calls: list[int] = []
    real = journal._runtime_root_env_candidate

    def _spy() -> dict[str, str]:
        calls.append(1)
        return real()

    monkeypatch.setattr(journal, "_runtime_root_env_candidate", _spy)
    return calls


def _arm_companion_by_run_refusal_after_mint(
    monkeypatch: pytest.MonkeyPatch, repository: Any, service: Any
) -> dict[str, int]:
    """Refuse ONLY the walk's own by-run read (#2566), through the #2385 seam.

    The selector's and the allocator's by-run reads run inside the pending-row mint
    and must succeed; the refusal is armed after it, for exactly one by-run call.
    """

    state = {"armed": 0, "refused": 0}
    real_query = repository.query_pipeline_jobs_by_run

    def _query(run_id: str) -> Any:
        if not state["armed"]:
            return real_query(run_id)
        state["armed"] = 0
        state["refused"] += 1
        with monkeypatch.context() as scoped:
            _refuse_scoped_job_records(scoped, repository)
            return real_query(run_id)

    monkeypatch.setattr(repository, "query_pipeline_jobs_by_run", _query)
    real_mint = service._create_pending_manual_retry_job

    def _mint(run_id: str) -> Any:
        minted = real_mint(run_id)
        state["armed"] = 1
        return minted

    monkeypatch.setattr(service, "_create_pending_manual_retry_job", _mint)
    return state


def _retry_submission_details(root: Path, retry_job_id: str) -> dict[str, Any]:
    (event,) = [
        record
        for record in _journal_records(root, "pipeline_event")
        if record.get("entity_id") == retry_job_id and record.get("event_type") == "submission"
    ]
    return dict(event["details"])


# --- #2566: the walk's own same-run companion read ----------------------------------------


@pytest.mark.parametrize("blocked", [True, False], ids=["companion_read_blocked", "companion_less_run_pin"])
def test_blocked_companion_read_is_counted_not_read_as_no_companion_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, blocked: bool
) -> None:
    """Task 1.2: same run, same recorded roots; only the companion read's fate differs.

    The run genuinely has no companion download job.  The blocked read must not be
    byte-identical to that (``blocked_reads == 1`` plus the journal's warning); the
    genuine companion-less walk is unchanged (no ``blocked_reads`` key, no warning).
    Either way the recorded roots resolve, so the retry submits.
    """

    root, repository, service = _seeded_journal(tmp_path)
    recorded_workspace, recorded_object_store = _roots(tmp_path, "recorded")
    _record_submission_roots(repository, _JOB_ID, recorded_workspace, recorded_object_store)
    _set_env_roots(monkeypatch, tmp_path)
    refusal = _arm_companion_by_run_refusal_after_mint(monkeypatch, repository, service) if blocked else None
    gateway = _SuccessGateway()

    with caplog.at_level(logging.WARNING, logger=_JOURNAL_LOGGER):
        retry = service.attempt_manual_retry(_RUN_ID, gateway, trusted_internal=True)

    assert retry.status == "submitted"
    assert len(gateway.requests) == 1
    manifest = gateway.requests[0].manifest
    assert (manifest["workspace_dir"], manifest["object_store_root"]) == (
        str(recorded_workspace),
        str(recorded_object_store),
    )
    counts = _retry_submission_details(root, retry.job_id)["runtime_root_resolution"]["candidate_counts"]
    messages = [record.getMessage() for record in caplog.records if record.name == _JOURNAL_LOGGER]
    if blocked:
        assert refusal == {"armed": 0, "refused": 1}
        assert counts["blocked_reads"] == 1
        assert any(
            "companion read blocked" in message and _BLOCK_REASON in message and _BLOCK_FIELD in message
            for message in messages
        )
    else:
        assert "blocked_reads" not in counts
        assert messages == []


# --- #2567: no environment root after a blocked provenance read ----------------------------


@pytest.mark.parametrize(
    ("job_type", "stage"),
    [("run_shud_forecast_array", "forecast"), ("download_source_cycle", "download")],
    ids=["non_download_non_db_free", "download"],
)
def test_blocked_provenance_read_never_submits_with_the_environment_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job_type: str, stage: str
) -> None:
    """Task 2.3(b): recorded candidates cleared by the block, environment complete.

    The forecast-array job is neither a download nor db-free, so before #2567 its
    unresolved roots were optional: the walk returned ``None`` and the job would
    have submitted with no root contract at all.
    """

    root, repository, service = _seeded_journal(tmp_path, jobs=[_pipeline_job_record(job_type=job_type, stage=stage)])
    _set_env_roots(monkeypatch, tmp_path)
    env_reads = _spy_env_candidate(monkeypatch)
    _arm_predecessor_refusal_after_mint(monkeypatch, repository, service)
    gateway = _UnreachableGateway()

    retry = service.attempt_manual_retry(_RUN_ID, gateway, trusted_internal=True)

    assert gateway.requests == []
    assert env_reads == []
    assert (retry.status, retry.error_code) == ("submission_failed", "RETRY_RUNTIME_ROOTS_UNRESOLVED")
    details = _retry_submission_details(root, retry.job_id)
    assert details["error_code"] == "RETRY_RUNTIME_ROOTS_UNRESOLVED"
    resolution = details["runtime_root_resolution"]
    assert resolution["job_type"] == job_type
    assert resolution["candidate_counts"]["blocked_reads"] >= 1
    assert resolution["resolved"] == {}
    assert resolution["missing"] == ["workspace_dir", "object_store_root"]
    assert "runtime_config:environment" not in json.dumps(details)


def test_blocked_read_and_absent_provenance_persist_different_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tasks 2.3(a) and (d): "blocked" vs "no submission event", same environment.

    (d) is the pin half: with no blocked read and no event the environment fallback
    is exactly as before -- the retry submits with the environment roots and the
    evidence carries no ``blocked_reads`` key.
    """

    blocked_root, blocked_repository, blocked_service = _seeded_journal(tmp_path / "blocked")
    absent_root, _absent_repository, absent_service = _seeded_journal(tmp_path / "absent")
    env_workspace, env_object_store = _set_env_roots(monkeypatch, tmp_path)
    _arm_predecessor_refusal_after_mint(monkeypatch, blocked_repository, blocked_service)
    absent_gateway = _SuccessGateway()

    blocked = blocked_service.attempt_manual_retry(_RUN_ID, _UnreachableGateway(), trusted_internal=True)
    absent = absent_service.attempt_manual_retry(_RUN_ID, absent_gateway, trusted_internal=True)

    blocked_details = _retry_submission_details(blocked_root, blocked.job_id)
    absent_details = _retry_submission_details(absent_root, absent.job_id)
    assert blocked_details != absent_details
    assert blocked_details["runtime_root_resolution"]["candidate_counts"]["blocked_reads"] >= 1

    assert absent.status == "submitted"
    absent_resolution = absent_details["runtime_root_resolution"]
    assert "blocked_reads" not in absent_resolution["candidate_counts"]
    assert absent_resolution["resolved"]["workspace_dir"]["source"] == "runtime_config:environment"
    assert absent_resolution["resolved"]["object_store_root"]["source"] == "runtime_config:environment"
    (request,) = absent_gateway.requests
    assert (request.manifest["workspace_dir"], request.manifest["object_store_root"]) == (
        str(env_workspace),
        str(env_object_store),
    )


def test_one_blocked_provenance_id_keeps_the_recorded_roots_and_no_env_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 2.3(c): a blocked read on one provenance id while a recorded candidate resolves.

    The failed job's own submission recorded its roots; the run's companion download
    job is unreadable by id.  The submission uses the recorded roots, counts the
    block, never reads the environment candidate, and -- for this array job type --
    does not take its task list from the environment's ``WORKSPACE_ROOT`` either.
    """

    companion = _pipeline_job_record(
        job_id=_COMPANION_JOB_ID,
        job_type="download_source_cycle",
        stage="download",
        status="succeeded",
        idempotency_key="gfs:gfs_2026072000:download",
        error_code=None,
    )
    root, repository, service = _seeded_journal(tmp_path, jobs=[_pipeline_job_record(), companion])
    recorded_workspace, recorded_object_store = _roots(tmp_path, "recorded")
    _record_submission_roots(repository, _JOB_ID, recorded_workspace, recorded_object_store)
    env_workspace, _env_object_store = _set_env_roots(monkeypatch, tmp_path)
    # The array manifest index exists ONLY under the environment's workspace.
    env_index = env_workspace / "runs" / _RUN_ID / "input" / "forecast_manifest_index.json"
    env_index.parent.mkdir(parents=True)
    env_index.write_text(json.dumps({"tasks": [{"model_id": "from_env_workspace"}]}), encoding="utf-8")
    env_reads = _spy_env_candidate(monkeypatch)

    armed = {"on": False}
    real_read = repository._pipeline_job_for_id_unlocked

    def _read(job_id: str, *args: Any, **kwargs: Any) -> Any:
        if armed["on"] and str(job_id) == _COMPANION_JOB_ID:
            raise _blocked_fault()
        return real_read(job_id, *args, **kwargs)

    monkeypatch.setattr(repository, "_pipeline_job_for_id_unlocked", _read)
    real_mint = service._create_pending_manual_retry_job

    def _mint(run_id: str) -> Any:
        minted = real_mint(run_id)
        armed["on"] = True
        return minted

    monkeypatch.setattr(service, "_create_pending_manual_retry_job", _mint)
    gateway = _SuccessGateway()

    retry = service.attempt_manual_retry(_RUN_ID, gateway, trusted_internal=True)

    assert retry.status == "submitted"
    (request,) = gateway.requests
    assert (request.manifest["workspace_dir"], request.manifest["object_store_root"]) == (
        str(recorded_workspace),
        str(recorded_object_store),
    )
    assert "tasks" not in request.manifest
    assert env_reads == []
    resolution = _retry_submission_details(root, retry.job_id)["runtime_root_resolution"]
    assert resolution["candidate_counts"]["blocked_reads"] == 1
    assert resolution["resolved"]["workspace_dir"]["source"].startswith(f"file_journal_event:{_JOB_ID}:")


def test_blocked_read_keeps_the_environment_db_free_policy_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task 2.3(c'): the db-free POLICY switch still binds; only root/selector values are banned.

    The recorded candidate carries complete roots but no db-free selector.  Without the
    switch it would resolve and submit; with it, the selectors are required and --
    the environment's own values being off limits -- nothing can supply them.
    """

    root, repository, service = _seeded_journal(tmp_path)
    recorded_workspace, recorded_object_store = _roots(tmp_path, "recorded")
    _record_submission_roots(repository, _JOB_ID, recorded_workspace, recorded_object_store)
    monkeypatch.setenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", "true")
    _arm_companion_by_run_refusal_after_mint(monkeypatch, repository, service)
    gateway = _UnreachableGateway()

    retry = service.attempt_manual_retry(_RUN_ID, gateway, trusted_internal=True)

    assert gateway.requests == []
    assert (retry.status, retry.error_code) == ("submission_failed", "RETRY_RUNTIME_ROOTS_UNRESOLVED")
    resolution = _retry_submission_details(root, retry.job_id)["runtime_root_resolution"]
    assert resolution["candidate_counts"]["blocked_reads"] == 1
    assert resolution["missing"] == []
    assert resolution["db_free_runtime"]["required"] is True
    assert "scheduler_allowed_roots" in resolution["db_free_runtime"]["missing"]
