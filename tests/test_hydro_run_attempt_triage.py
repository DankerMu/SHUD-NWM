"""#2557 triage: can a same-run_id forecast rerun leave an ACTIVE hydro_run row at a stale attempt?

``create_hydro_run_from_basin`` keeps an existing non-retriable (not failed/cancelled)
``hydro_run`` row unchanged, so its ``submission_attempt`` can lag the reservation's.
The three accepted-submit release entrypoints (submit-attempt rejection, absence retry
permit, operator-verified-absence demotion) only release ACTIVE hydro rows whose
attempt equals the master's.  The harm the issue names therefore needs an ACTIVE
row at a stale attempt when a later attempt is staged.

These tests drive the real write path -- ``orchestrate_cycle`` over a real
``FileOrchestrationJournalRepository`` (only the Slurm runtime is a double), with the
release entrypoints called exactly as their production callers call them -- through
both ways a same-``run_id`` rerun stages a later attempt:

* the reclaim loop (an ambiguous submit, released by an absence permit or an
  operator demotion, then reclaimed): the release entrypoint of the FIRST attempt
  already moved the active row to ``failed`` at the matching attempt, so the
  restage rewrites it and the later attempt's rejection releases it;
* a terminal-success rerun (a forced resubmit minting ``_retry_1``): the lagging row
  is ``succeeded``, which no release entrypoint may touch at any attempt.

Scope of the verdict: on these two rerun paths the active-and-stale state is not
reachable, and the lag exists only on non-active terminal rows.  One path is NOT
exercised here: ``FileJournalRetryService.attempt_manual_retry`` (via
``_create_pending_manual_retry_job``) of a per-model ``fcst_`` run id resets a
failed hydro row to ``pending`` at the old attempt, and nothing finalizes that row
when the manual job ends.  That entry has no production caller: the API's manual
retry wires the database ``RetryService`` (``apps/api/routes/pipeline.py:159-163``,
called at ``:581``), ``scripts/node22_manual_retry_failed_runs.py`` only reads the
retry source for its preview (``:194``) and writes ``record_manual_repair``
(``:257``), and the scheduler wires the file service for inline auto-retry only
(``services/orchestrator/scheduler_core.py:96``).  If that state ever arises, the
last test pins what the release entrypoints do with it: an active row at another
attempt is never released.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# Every service / suite import is function-local (the ``tests/test_retry_mint_floor.py``
# convention): a module-scope import would join this file to other suites' pinned
# importer closures in ``scripts/select_ci_tests.py``.  Its CI routes are explicit rows.

_CYCLE = "2026050100"
_MODELS = ("model_0", "model_1")


def _repository(root: Path) -> Any:
    from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository

    return FileOrchestrationJournalRepository(root)


def _scripted_forecast_submit(script: list[str]) -> Any:
    """Slurm double whose forecast array submits follow ``script`` (``ok`` / ``ambiguous`` / ``rejected``)."""

    from services.orchestrator.chain_config import SubmitDisposition
    from services.orchestrator.chain_types import OrchestratorError
    from tests.test_orchestration_chain import FakeCycleSlurmClient

    class _ScriptedForecastSubmit(FakeCycleSlurmClient):
        def __init__(self) -> None:
            super().__init__()
            self.script = list(script)

        def submit_job_array(self, job_type: str, **kwargs: Any) -> dict[str, Any]:
            if kwargs["stage_name"] != "forecast":
                return super().submit_job_array(job_type, **kwargs)
            outcome = self.script.pop(0)
            if outcome == "ambiguous":
                raise RuntimeError("response lost after the Gateway call boundary")
            if outcome == "rejected":
                error = OrchestratorError("SBATCH_SUBMISSION_FAILED", "sbatch rejected the array")
                error.submit_disposition = SubmitDisposition.REJECTED
                raise error
            return super().submit_job_array(job_type, **kwargs)

    return _ScriptedForecastSubmit()


def _basins(**state_evidence: Any) -> list[dict[str, Any]]:
    from tests.test_cohort_membership_attribution import _candidate, _cohort_basins

    basins = _cohort_basins([_candidate(index) for index in range(len(_MODELS))])
    for basin in basins:
        basin["state_evidence"] = {**basin["state_evidence"], **state_evidence}
    return basins


def _pass(tmp_path: Path, root: Path, script: list[str], **state_evidence: Any) -> Any:
    from tests.test_orchestration_chain import _orchestrator

    workspace = tmp_path / f"pass-{len(list(tmp_path.glob('pass-*')))}"
    orchestrator = _orchestrator(
        workspace, _repository(root), _scripted_forecast_submit(script), terminal_stage="forecast"
    )
    return orchestrator.orchestrate_cycle("gfs", _CYCLE, _basins(**state_evidence))


def _hydro(root: Path) -> dict[str, tuple[str, int]]:
    repository = _repository(root)
    faces = {}
    for model_id in _MODELS:
        row = repository._hydro_run_for(f"fcst_gfs_{_CYCLE}_{model_id}")
        assert row is not None
        faces[model_id] = (str(row["status"]), int(row["submission_attempt"]))
    return faces


def _master(root: Path) -> dict[str, Any]:
    """The newest forecast cohort master (bare id, then ``_retry_<n>``)."""

    job_ids = sorted(
        str(row["job_id"])
        for row in _repository(root).query_pipeline_jobs_by_cycle(f"gfs_{_CYCLE}")
        if row.get("stage") == "forecast" and row.get("model_id") in (None, "")
    )
    return _repository(root).get_accepted_submit_pipeline_job(job_ids[-1])


def _release_first_attempt(root: Path, door: str) -> None:
    """Release attempt 1 through the entrypoint a production caller uses for this shape."""

    from services.orchestrator import reconcile as reconcile_module
    from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION

    repository = _repository(root)
    master = _master(root)
    assert (master["status"], master["submission_attempt"]) == ("reserved", 1)
    if door == "absence_retry_permitted":
        assert repository.permit_pipeline_job_retry(
            str(master["job_id"]),
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=1,
            expected_submission_attempt_started_at=master["submission_attempt_started_at"],
        )
        return

    class _NoCommentQuery:
        def __call__(self, _key: str, **kwargs: Any) -> Any:
            del kwargs
            raise reconcile_module.ReconcileQueryUnavailable(
                "accounting does not store job comments", reason_class="comment_accounting_unproven"
            )

    now = datetime.now(UTC) + timedelta(hours=1)
    outcomes = reconcile_module.reconcile_reserved_unbound_jobs(
        repository, comment_query=_NoCommentQuery(), grace=timedelta(0), now=lambda: now
    )
    assert [outcome.action for outcome in outcomes] == ["query_unavailable"]
    held = _master(root)
    assert repository.demote_operator_verified_reserved_job(
        str(held["job_id"]),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_submission_attempt_started_at=held["submission_attempt_started_at"],
        checked_by="operator-alice",
        checked_at=now,
        verification_note="sacct and squeue show no matching job in the attempt window",
    ) is not None


@pytest.mark.parametrize("door", ["absence_retry_permitted", "operator_verified_absence"])
def test_reclaimed_rerun_restages_the_released_row_and_releases_it_again(tmp_path: Path, door: str) -> None:
    from services.orchestrator.scheduler_state_types import ACTIVE_HYDRO_STATUSES

    root = tmp_path / "journal"

    first = _pass(tmp_path, root, ["ambiguous"])
    assert [stage.status for stage in first.stages][-1] == "submit_result_ambiguous"
    # The ambiguous attempt leaves each member's hydro row ACTIVE at attempt 1 ...
    assert {model_id: face[1] for model_id, face in _hydro(root).items()} == {"model_0": 1, "model_1": 1}
    assert all(face[0] in ACTIVE_HYDRO_STATUSES for face in _hydro(root).values())
    # ... and the only doors that make the master reclaimable release those rows first,
    # at the matching attempt.
    _release_first_attempt(root, door)
    assert _hydro(root) == {"model_0": ("failed", 1), "model_1": ("failed", 1)}

    second = _pass(tmp_path, root, ["rejected"])

    master = _master(root)
    assert master["submission_attempt"] == 2
    assert (master["status"], master["submit_outcome"]) == ("submission_failed", "rejected")
    assert second.stages[-1].status == "submission_failed"
    # The failed row was retriable, so the restage rewrote it at attempt 2, and the
    # attempt-2 rejection released it: no active row is left behind at any attempt.
    assert _hydro(root) == {"model_0": ("failed", 2), "model_1": ("failed", 2)}


def test_terminal_success_rerun_lags_only_on_a_row_no_release_may_touch(tmp_path: Path) -> None:
    root = tmp_path / "journal"
    assert _pass(tmp_path, root, ["ok"]).status == "succeeded"
    assert _hydro(root) == {"model_0": ("succeeded", 1), "model_1": ("succeeded", 1)}

    # A whitelisted forced resubmit of the terminal forecast (same run_ids).
    rerun = _pass(
        tmp_path,
        root,
        ["rejected"],
        decision="retry_terminal_run_manifest_missing",
        reason="terminal_run_manifest_missing",
    )

    assert rerun.stages[-1].status == "submission_failed"
    master = _master(root)
    assert str(master["job_id"]).endswith("_forecast_retry_1")
    assert (master["submission_attempt"], master["submit_outcome"]) == (2, "rejected")
    # The rerun's runtime manifest (and reservation) carry attempt 2 ...
    import json

    run_dir = tmp_path / "pass-1" / "workspace" / "runs" / f"fcst_gfs_{_CYCLE}_model_0"
    manifest_path = run_dir / "input" / "manifest.json"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["submission_attempt"] == 2
    # ... so the durable attempt lags (1 < 2), but the row is a non-active terminal success:
    # the attempt-2 rejection must leave it alone, and does.
    assert _hydro(root) == {"model_0": ("succeeded", 1), "model_1": ("succeeded", 1)}


def _journal_master_at_attempt_two(root: Path) -> tuple[Any, dict[str, Any]]:
    """Journal API only: a cohort master reclaimed to attempt 2 before any hydro row exists."""

    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )
    from tests.gateway_reconcile_helpers import _versioned_master_reservation_record

    repository = _repository(root)
    record = _versioned_master_reservation_record(member_count=2)
    assert repository.reserve_pipeline_job(dict(record)) is not None
    job_id = str(record["job_id"])
    repository.transition_pipeline_job_submit_evidence(
        job_id,
        AcceptedSubmitTransition.timeout(),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    )
    held = repository.get_accepted_submit_pipeline_job(job_id)
    assert repository.permit_pipeline_job_retry(
        job_id,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_submission_attempt_started_at=held["submission_attempt_started_at"],
    )
    released = repository.get_accepted_submit_pipeline_job(job_id)
    assert repository.reclaim_pipeline_job_reservation(
        {
            **record,
            "status": "reserved",
            "expected_submission_attempt": 1,
            "expected_submission_attempt_started_at": released["submission_attempt_started_at"],
            "submission_attempt": 2,
        }
    ) is not None
    master = repository.get_accepted_submit_pipeline_job(job_id)
    assert (master["status"], master["submission_attempt"]) == ("reserved", 2)
    return repository, master


def _drive_attempt_two_door(repository: Any, master: dict[str, Any], door: str) -> dict[str, Any]:
    """Drive one release entrypoint at attempt 2 exactly as its production caller does; return the master."""

    from services.orchestrator import reconcile as reconcile_module
    from services.orchestrator.accepted_submit_identity import (
        ACCEPTED_SUBMIT_CONTRACT_VERSION,
        AcceptedSubmitTransition,
    )

    job_id = str(master["job_id"])
    if door == "rejected":
        result = repository.reject_pipeline_job_submit_attempt(
            str(master["idempotency_key"]),
            pipeline_job_id=job_id,
            expected_submission_attempt=2,
            finished_at=datetime.now(UTC),
            error_code="SBATCH_SUBMISSION_FAILED",
            error_message="sbatch rejected the array",
            stage="forecast",
            job_type="run_shud_forecast_array",
        )
        assert result.committed
        return repository.get_accepted_submit_pipeline_job(job_id)
    repository.transition_pipeline_job_submit_evidence(
        job_id,
        AcceptedSubmitTransition.timeout(),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=2,
        expected_statuses=("reserved",),
        require_unbound=True,
    )
    if door == "absence_retry_permitted":
        held = repository.get_accepted_submit_pipeline_job(job_id)
        assert repository.permit_pipeline_job_retry(
            job_id,
            accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
            expected_submission_attempt=2,
            expected_submission_attempt_started_at=held["submission_attempt_started_at"],
        )
        return repository.get_accepted_submit_pipeline_job(job_id)

    class _NoCommentQuery:
        def __call__(self, _key: str, **kwargs: Any) -> Any:
            del kwargs
            raise reconcile_module.ReconcileQueryUnavailable(
                "accounting does not store job comments", reason_class="comment_accounting_unproven"
            )

    now = datetime.now(UTC) + timedelta(hours=1)
    outcomes = reconcile_module.reconcile_reserved_unbound_jobs(
        repository, comment_query=_NoCommentQuery(), grace=timedelta(0), now=lambda: now
    )
    assert [outcome.action for outcome in outcomes] == ["query_unavailable"]
    held = repository.get_accepted_submit_pipeline_job(job_id)
    assert repository.demote_operator_verified_reserved_job(
        job_id,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=2,
        expected_submission_attempt_started_at=held["submission_attempt_started_at"],
        checked_by="operator-alice",
        checked_at=now,
        verification_note="sacct and squeue show no matching job in the attempt window",
    ) is not None
    return repository.get_accepted_submit_pipeline_job(job_id)


@pytest.mark.parametrize(
    ("door", "master_face"),
    [
        ("rejected", ("submission_failed", "rejected")),
        ("absence_retry_permitted", ("reservation_lost", "absence_retry_permitted")),
        ("operator_verified_absence", ("reservation_lost", "operator_verified_absence")),
    ],
)
def test_another_attempts_active_row_is_never_released(
    tmp_path: Path, door: str, master_face: tuple[str, str]
) -> None:
    """Spec scenario: an ACTIVE hydro row at attempt 1 survives every release door at attempt 2 byte-unchanged."""

    from tests.gateway_reconcile_helpers import _append_cohort_placeholders

    repository, master = _journal_master_at_attempt_two(tmp_path / "journal")
    written = _append_cohort_placeholders(
        repository, 2, common_updates={"status": "submitted", "error_code": None, "error_message": None}
    )
    before = {run_id: repository._hydro_run_for(run_id) for run_id in written}
    assert {run_id: (row["status"], row["submission_attempt"]) for run_id, row in before.items()} == {
        "fcst_gfs_2026071200_model_0": ("submitted", 1),
        "fcst_gfs_2026071200_model_1": ("submitted", 1),
    }

    released = _drive_attempt_two_door(repository, master, door)

    # The door really fired on the attempt-2 master ...
    outcome = released["submit_outcome"] if door == "rejected" else released["reconciliation_decision"]
    assert (released["status"], outcome) == master_face
    assert released["submission_attempt"] == 2
    # ... and left the other attempt's active rows exactly as they were.
    reopened = _repository(repository.root)
    assert {run_id: reopened._hydro_run_for(run_id) for run_id in written} == before
