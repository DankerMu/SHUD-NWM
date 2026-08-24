"""Requirement pins for the one-shot manual-retry marker on DB-free forecast runs.

The requirement: a run whose failure the classifier calls permanent (``ARTIFACT_NOT_FOUND``
is the motivating case) never restarts on its own once the cause is repaired, so an operator
must be able to mark exactly ONE named run for one manual retry -- and nothing else.  "Nothing
else" is the whole risk: the forecast stage carries a cohort-master row covering every model in
the cycle, and a marker aimed at that row would restart the entire cohort.  The tool must
therefore resolve the per-run row, preview before it writes, report its refusals instead of
raising them, and stay per-run when several run ids are handed to one invocation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scripts.node22_manual_retry_failed_runs import main
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from tests.test_file_orchestration_journal import _dt, _journal_tree_bytes, _source_job

_CYCLE_TIME = _dt("2026-08-23T00:00:00Z")

# The two shapes of the motivating node-22 incident, verbatim in geometry: the per-run row the
# marker must target, and the cohort master covering the whole cycle that it must not.
_PER_RUN_MODEL_ID = "dg_abc"
_PER_RUN_ID = "fcst_gfs_2026082300_dg_abc"
_PER_RUN_JOB_ID = "job_fcst_gfs_2026082300_dg_abc_forecast_reconciled_34817_6"
_COHORT_RUN_ID = "cycle_gfs_2026082300"
_COHORT_JOB_ID = "job_cycle_gfs_2026082300_forecast_cohort_abc123_forecast"
_ACTIVE_RUN_ID = "fcst_gfs_2026082300_dg_zzz"
_ACTIVE_JOB_ID = "job_fcst_gfs_2026082300_dg_zzz_forecast_reconciled_34817_7"
_UNSEEDED_RUN_ID = "fcst_gfs_2026082300_dg_absent"


def _failed_per_run_job() -> dict[str, Any]:
    """The reconciled per-model row, permanently failed on a missing artifact."""

    job = _source_job(
        _CYCLE_TIME,
        source_id="gfs",
        job_id=_PER_RUN_JOB_ID,
        model_id=_PER_RUN_MODEL_ID,
    )
    job.update(
        {
            "status": "permanently_failed",
            "error_code": "ARTIFACT_NOT_FOUND",
            "slurm_job_id": "34817_6",
            "array_task_id": 6,
            "retry_count": 3,
        }
    )
    return job


def _failed_cohort_master_job() -> dict[str, Any]:
    """The cycle-wide forecast master, failed on the same cause.

    Failed as well, deliberately: with both rows in the same terminal shape a resolver that
    scoped by cycle instead of by run would have two rows to choose from, so naming the per-run
    one is evidence of run scoping rather than of the cohort row simply being absent.
    """

    job = _source_job(
        _CYCLE_TIME,
        source_id="gfs",
        job_id=_COHORT_JOB_ID,
        model_id=_PER_RUN_MODEL_ID,
    )
    job.update(
        {
            "run_id": _COHORT_RUN_ID,
            "model_id": None,
            "status": "permanently_failed",
            "error_code": "ARTIFACT_NOT_FOUND",
            "slurm_job_id": "34817",
            "idempotency_key": "cycle_gfs_2026082300:forecast",
            "retry_count": 3,
        }
    )
    return job


def _active_per_run_job() -> dict[str, Any]:
    """A second model in the same cycle, still queued -- not markable."""

    return _source_job(
        _CYCLE_TIME,
        source_id="gfs",
        job_id=_ACTIVE_JOB_ID,
        model_id="dg_zzz",
    )


def _journal(tmp_path: Path, jobs: list[dict[str, Any]]) -> Path:
    root = tmp_path / "journal"
    repository = FileOrchestrationJournalRepository(root)
    repository.ensure_forecast_cycle(source_id="gfs", cycle_time=_CYCLE_TIME)
    for job in jobs:
        repository.upsert_pipeline_job(job)
    for job in jobs:
        # A vacuous pin is worse than none: assert the seeding actually landed both shapes
        # before any assertion claims the tool chose between them.
        assert repository.get_pipeline_job(str(job["job_id"])) is not None
    return root


def _invoke(root: Path, tmp_path: Path, *args: str) -> tuple[int, dict[str, Any]]:
    receipt_path = tmp_path / "receipt.json"
    exit_code = main(
        [
            "--journal-root",
            str(root),
            "--reason",
            "forcing backfilled under the new model id",
            "--requested-by",
            "operator",
            "--output",
            str(receipt_path),
            *args,
        ]
    )
    return exit_code, json.loads(receipt_path.read_text(encoding="utf-8"))


def _manual_retry_marker_events(root: Path) -> list[dict[str, Any]]:
    """Every durable manual-retry marker in the journal, read off the record stream."""

    events: list[dict[str, Any]] = []
    for path in sorted((root / "journal").rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("record_type") != "pipeline_event":
                continue
            payload = record.get("payload") or {}
            if (payload.get("details") or {}).get("manual_retry_marker") is True:
                events.append(payload)
    return events


def test_preview_targets_the_per_run_row_not_the_cohort_master(tmp_path: Path) -> None:
    """The highest-value pin: a marker on the cohort master would restart every model."""

    root = _journal(tmp_path, [_failed_per_run_job(), _failed_cohort_master_job()])

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _PER_RUN_ID)

    assert exit_code == 0
    preview = receipt["runs"][0]["preview"]
    assert preview["decision"] == "would_mark"
    assert preview["job_id"] == _PER_RUN_JOB_ID
    assert preview["job_id"] != _COHORT_JOB_ID
    assert preview["stage"] == "forecast"
    assert preview["status"] == "permanently_failed"
    assert preview["error_code"] == "ARTIFACT_NOT_FOUND"


def test_default_invocation_previews_without_writing_anything(tmp_path: Path) -> None:
    """No ``--execute`` means no bytes: the operator sees the target before it is touched."""

    root = _journal(tmp_path, [_failed_per_run_job(), _failed_cohort_master_job()])
    before = _journal_tree_bytes(root)

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _PER_RUN_ID)

    assert exit_code == 0
    assert receipt["executed"] is False
    assert receipt["runs"][0]["outcome"] == "preview_only"
    assert receipt["outcome_counts"] == {"preview_only": 1}
    assert _journal_tree_bytes(root) == before
    assert _manual_retry_marker_events(root) == []


def test_active_run_is_reported_as_run_active_not_raised(tmp_path: Path) -> None:
    """A run still in flight is refused, and the refusal comes back in the receipt."""

    root = _journal(tmp_path, [_active_per_run_job()])

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _ACTIVE_RUN_ID, "--execute")

    # Under ``--execute`` the operator asked for a marker and did not get one, so this is a
    # refusal in the receipt AND in the exit code -- a preview-time refusal must not be
    # indistinguishable from a successful preview run.
    assert exit_code == 1
    entry = receipt["runs"][0]
    assert entry["outcome"] == "refused"
    assert entry["preview"]["decision"] == "refused"
    assert entry["preview"]["reason"] == "run_active"
    assert entry["preview"]["job_id"] == _ACTIVE_JOB_ID
    assert _manual_retry_marker_events(root) == []


def test_run_without_a_failed_job_is_reported_as_no_retryable_failed_job(tmp_path: Path) -> None:
    """Nothing eligible in the journal is a reported refusal, not a crash."""

    root = _journal(tmp_path, [_failed_per_run_job()])

    exit_code, receipt = _invoke(root, tmp_path, "--run-id", _UNSEEDED_RUN_ID, "--execute")

    assert exit_code == 1
    entry = receipt["runs"][0]
    assert entry["outcome"] == "refused"
    assert entry["preview"]["decision"] == "refused"
    assert entry["preview"]["reason"] == "no_retryable_failed_job"
    assert _manual_retry_marker_events(root) == []


def test_execute_marks_exactly_the_named_run_past_an_ineligible_sibling(tmp_path: Path) -> None:
    """Per-run, never a sweep: the ineligible run is listed FIRST and must not abort the rest.

    One marker lands, on the reconciled per-run row.  The cohort master and the active sibling
    stay unmarked -- marking the master here would be the cohort-wide restart this tool exists
    to avoid.
    """

    root = _journal(
        tmp_path,
        [_failed_per_run_job(), _failed_cohort_master_job(), _active_per_run_job()],
    )

    exit_code, receipt = _invoke(
        root,
        tmp_path,
        "--run-id",
        _ACTIVE_RUN_ID,
        "--run-id",
        _PER_RUN_ID,
        "--execute",
    )

    # Non-zero because one run was refused, and yet the eligible run WAS marked: the refusal
    # is per-run and must not abort the rest of the invocation.
    assert exit_code == 1
    assert receipt["executed"] is True
    assert receipt["outcome_counts"] == {"refused": 1, "marked": 1}

    refused, marked = receipt["runs"]
    assert refused["run_id"] == _ACTIVE_RUN_ID
    assert refused["preview"]["reason"] == "run_active"
    assert marked["run_id"] == _PER_RUN_ID
    assert marked["outcome"] == "marked"
    assert marked["marker"]["job_id"] == _PER_RUN_JOB_ID
    assert marked["marker"]["status"] == "manual_repair_requested"
    assert marked["marker"]["retry_count"] == 4

    events = _manual_retry_marker_events(root)
    assert len(events) == 1
    assert events[0]["entity_id"] == _PER_RUN_JOB_ID
    assert events[0]["status_to"] == "manual_repair_requested"
    assert events[0]["details"]["prior_failure_reason"] == "ARTIFACT_NOT_FOUND"
    assert events[0]["details"]["failure"]["permanent"] is False
    assert events[0]["details"]["requested_by"] == "operator"
