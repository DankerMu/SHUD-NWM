"""Pin: node-22's DB-free automatic forecast retry keeps the DB-free runtime contract (#3157 PR 2).

The scenario "automatic forecast retry runs without database url" (requirement "DB-free
scheduler retry manifests preserve runtime mode") used to be pinned only by two
``tests/test_retry.py`` cases that drove the retired ``RetryService`` submit helper. That method
had no production caller and was deleted with the dead manual-retry closure. This file
pins the scenario on the path the DB-free scheduler actually takes:

* ``scheduler_core.py`` ``_db_free_file_retry_service_from_env`` and the
  ``ForecastOrchestrator(...)`` construction in ``ProductionScheduler``: with
  ``db_free_required`` and a ``FileOrchestrationJournalRepository``, the orchestrator gets
  a ``FileJournalRetryService`` and no database store;
* ``chain_forecast_execution.py`` calls ``_schedule_cycle_stage_retry`` after a failed
  forecast stage;
* ``chain_forecast_orchestrator_cycle.py`` ``_schedule_cycle_stage_retry``: for a current
  accepted-submit master it mints ``<base>_retry_<N>`` and hands resubmission back to
  the stage loop;
* ``chain_stage_execution.py`` builds the retry's Slurm manifest with
  ``_build_cycle_stage_manifest``, which is ``chain_manifests.build_cycle_stage_manifest``.
  That is the same builder as the initial submission, and it reads the DB-free
  selectors from the scheduler environment.

The orchestrator, journal repository and retry service are the production classes. Only
the Slurm gateway is faked (``FakeCycleSlurmClient``, a system boundary).

Two of the scenario's clauses are NOT recorded by this path, so they are not asserted:

* the prior job id: the retry master row's ``previous_job_id`` stays ``None`` and no
  ``retry`` event is written. The predecessor can only be derived from the retry identity
  (``<prior job id>_retry_<N>``, also used as the idempotency key and Slurm comment
  suffix), which is pinned here;
* the DB-free runtime mode: it travels in the submitted manifest, but neither the journal
  rows nor the events (public or private) carry ``scheduler_db_free_required`` or any
  selector.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator.file_orchestration_journal import (
    FileJournalRetryService,
    FileOrchestrationJournalRepository,
)
from services.orchestrator.retry import RetryConfig
from services.orchestrator.retry_identity import split_retry_job_identity
from tests.test_orchestration_chain import FakeCycleSlurmClient, _basins, _orchestrator

_CYCLE = "2026050100"
_CYCLE_ID = f"gfs_{_CYCLE}"
_PRIOR_JOB_ID = f"job_cycle_gfs_{_CYCLE}_forecast_cohort_fixture_forecast"
_RETRY_JOB_ID = f"{_PRIOR_JOB_ID}_retry_1"
_DB_FREE_SELECTOR_KEYS = (
    "scheduler_db_free_required",
    "scheduler_allowed_roots",
    "scheduler_registry_backend",
    "scheduler_registry_manifest",
    "scheduler_canonical_readiness_backend",
    "scheduler_canonical_readiness_index",
    "scheduler_state_index_backend",
    "scheduler_state_index",
)


def _set_db_free_scheduler_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """The node-22 DB-free scheduler environment; returns the manifest values it must produce."""

    scheduler_root = tmp_path / "object-store" / "scheduler"
    expected = {
        "scheduler_db_free_required": "true",
        "scheduler_allowed_roots": str(tmp_path),
        "scheduler_registry_backend": "file",
        "scheduler_registry_manifest": str(scheduler_root / "registry" / "manifest-last.json"),
        "scheduler_canonical_readiness_backend": "file",
        "scheduler_canonical_readiness_index": str(scheduler_root / "canonical-readiness" / "index-last.json"),
        "scheduler_state_index_backend": "file",
        "scheduler_state_index": str(scheduler_root / "state" / "index-last.json"),
    }
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for key in (
        "NHMS_SLURM_SCHEDULER_REGISTRY_MANIFEST",
        "NHMS_SLURM_SCHEDULER_CANONICAL_READINESS_INDEX",
        "NHMS_SLURM_SCHEDULER_STATE_INDEX",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NHMS_SCHEDULER_DB_FREE_REQUIRED", "true")
    monkeypatch.setenv("NHMS_SCHEDULER_ALLOWED_ROOTS", expected["scheduler_allowed_roots"])
    monkeypatch.setenv("NHMS_SCHEDULER_REGISTRY_BACKEND", "file")
    monkeypatch.setenv("NHMS_SCHEDULER_REGISTRY_MANIFEST", expected["scheduler_registry_manifest"])
    monkeypatch.setenv("NHMS_SCHEDULER_CANONICAL_READINESS_BACKEND", "file")
    monkeypatch.setenv("NHMS_SCHEDULER_CANONICAL_READINESS_INDEX", expected["scheduler_canonical_readiness_index"])
    monkeypatch.setenv("NHMS_SCHEDULER_STATE_INDEX_BACKEND", "file")
    monkeypatch.setenv("NHMS_SCHEDULER_STATE_INDEX", expected["scheduler_state_index"])
    return expected


def _forecast_cohort_basins() -> list[dict[str, Any]]:
    basins = _basins(2)
    for index, basin in enumerate(basins):
        basin.update(
            {
                "run_id": f"fcst_gfs_{_CYCLE}_model_{index}",
                "candidate_id": f"gfs:2026-05-01T00:00:00Z:model_{index}:forecast_gfs_deterministic",
                "orchestration_run_id": f"cycle_gfs_{_CYCLE}_forecast_cohort_fixture",
                "restart_stage": "forecast",
                "state_evidence": {"restart_stage": "forecast"},
                "model_package_uri": f"s3://nhms/models/model_{index}.tar",
                "model_package_checksum": f"sha256:model-{index}",
            }
        )
    return basins


def _pipeline_events(journal_root: Path, entity_id: str) -> list[dict[str, Any]]:
    events = []
    for path in sorted(journal_root.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            payload = record.get("payload") or {}
            if record.get("record_type") == "pipeline_event" and payload.get("entity_id") == entity_id:
                events.append(payload)
    return events


def test_db_free_automatic_forecast_retry_resubmits_with_the_db_free_runtime_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_selectors = _set_db_free_scheduler_env(monkeypatch, tmp_path)
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    client = FakeCycleSlurmClient(
        array_results_by_stage={"forecast": [["failed", "failed"], ["succeeded", "succeeded"]]},
        error_code_by_stage={"forecast": "NODE_FAILURE"},
    )
    retry_service = FileJournalRetryService(repository, RetryConfig(max_retries=1, backoff_schedule=[0]))
    orchestrator = _orchestrator(tmp_path, repository, client, retry_service=retry_service)

    result = orchestrator.orchestrate_cycle("gfs", _CYCLE, _forecast_cohort_basins())

    # Runnable without DATABASE_URL: the whole cycle, retry included, completes with it unset.
    assert result.status == "complete"
    forecast_jobs = [job for job in client.jobs.values() if job["stage"] == "forecast"]
    assert len(forecast_jobs) == 2
    initial_job, retry_job = sorted(forecast_jobs, key=lambda job: int(job["job_id"]))
    initial_manifest = initial_job["payload"]["manifest"]
    retry_manifest = retry_job["payload"]["manifest"]

    # The retry is the automatic one: same cohort, the retry identity in the Slurm comment.
    assert retry_manifest["comment"] == f"nhms_idem:cycle_gfs_{_CYCLE}_forecast_cohort_fixture:forecast:retry_1"
    assert [task["model_id"] for task in retry_job["payload"]["tasks"]] == ["model_0", "model_1"]

    # Manifest clause: DB-free required plus the canonical file backend selectors, identical
    # to the initial submission's, and no database URL anywhere in the request.
    for job in (initial_job, retry_job):
        manifest = job["payload"]["manifest"]
        assert {key: manifest[key] for key in _DB_FREE_SELECTOR_KEYS} == expected_selectors
        assert "DATABASE_URL" not in json.dumps(job["payload"], sort_keys=True, default=str)
    assert {key: retry_manifest[key] for key in _DB_FREE_SELECTOR_KEYS} == {
        key: initial_manifest[key] for key in _DB_FREE_SELECTOR_KEYS
    }

    # Evidence clause, the parts this path records durably (reopened journal).
    reopened = FileOrchestrationJournalRepository(repository.root)
    prior = reopened.get_pipeline_job(_PRIOR_JOB_ID)
    retry = reopened.get_pipeline_job(_RETRY_JOB_ID)
    assert prior is not None and retry is not None
    assert prior["status"] == "failed"
    assert prior["slurm_job_id"] == initial_job["job_id"]
    assert retry["status"] == "succeeded"
    # Prior job: derivable from the retry identity only (see module docstring).
    assert split_retry_job_identity(retry["job_id"]) == (_PRIOR_JOB_ID, 1)
    assert retry["idempotency_key"] == f"cycle_gfs_{_CYCLE}_forecast_cohort_fixture:forecast:retry_1"
    # New Slurm job id.
    assert retry["slurm_job_id"] == retry_job["job_id"]
    assert retry["slurm_job_id"] != prior["slurm_job_id"]
    # Stage, source/cycle identity, model ids (cohort master: per-member model ids).
    assert retry["stage"] == "forecast"
    assert (retry["source_id"], retry["cycle_id"], retry["cycle_time"]) == (
        "gfs",
        _CYCLE_ID,
        "2026-05-01T00:00:00Z",
    )
    assert [member["model_id"] for member in retry["cohort_members"]] == ["model_0", "model_1"]
    # Retry attempt: the durable submission attempt advances 1 -> 2.
    assert (prior["submission_attempt"], retry["submission_attempt"]) == (1, 2)
    # The retry's own submission event names the new Slurm job and stage.
    submissions = [
        event for event in _pipeline_events(repository.root, _RETRY_JOB_ID) if event["event_type"] == "submission"
    ]
    assert len(submissions) == 1
    assert submissions[0]["details"]["slurm_job_id"] == retry_job["job_id"]
    assert submissions[0]["details"]["stage"] == "forecast"
