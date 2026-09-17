from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import httpx
import pytest

from services.orchestrator.chain import (
    CycleOrchestrationContext,
    HttpSlurmGatewayClient,
    M3_STAGES,
    OrchestratorError,
)
from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from services.orchestrator.reconcile import (
    CommentAccountingResult,
    SacctRecord,
    reconcile_inflight_jobs,
    reconcile_reserved_unbound_jobs,
)
from services.orchestrator.reservation import reserve_candidate, slurm_comment_for
from services.orchestrator.scheduler_candidate_execution_evidence import _pipeline_result_slurm_submit_called
from services.orchestrator.scheduler_evidence import UNKNOWN_AFTER_ATTEMPT
from tests.test_orchestration_chain import FakeCycleSlurmClient, _basins, _dt, _orchestrator


_CYCLE = "2026050100"
_CYCLE_TIME = _dt("2026-05-01T00:00:00Z")
_CYCLE_ID = f"gfs_{_CYCLE}"


def _forcing_basins(count: int, *, orchestration_run_id: str) -> list[dict[str, Any]]:
    basins = _basins(count)
    for index, basin in enumerate(basins):
        basin.update(
            {
                "run_id": f"fcst_gfs_{_CYCLE}_model_{index}",
                "candidate_id": (
                    f"gfs:2026-05-01T00:00:00Z:model_{index}:forecast_gfs_deterministic"
                ),
                "orchestration_run_id": orchestration_run_id,
                "restart_stage": "forcing",
                "state_evidence": {"restart_stage": "forcing"},
                "model_package_uri": f"s3://nhms/models/model_{index}.tar",
                "model_package_checksum": f"sha256:model-{index}",
            }
        )
    return basins


def _forcing_context(
    orchestrator: Any,
    basins: list[dict[str, Any]],
    *,
    run_id: str,
) -> CycleOrchestrationContext:
    normalized = orchestrator._normalize_cycle_basins(basins, "gfs", _CYCLE_TIME)
    return CycleOrchestrationContext(
        source_id="gfs",
        cycle_time=_CYCLE_TIME,
        cycle_id=_CYCLE_ID,
        run_id=run_id,
        all_basins=normalized,
        active_basins=list(normalized),
        restart_stage="forcing",
    )


@pytest.mark.parametrize(
    ("failure", "expected_error_code"),
    [
        pytest.param("http_502", "SLURM_PARSE_ERROR", id="http-502"),
        pytest.param("transport", "SLURM_GATEWAY_UNAVAILABLE", id="transport"),
        pytest.param("invalid_success", "SLURM_GATEWAY_INVALID_RESPONSE", id="invalid-success"),
    ],
)
def test_real_http_forcing_ambiguity_persists_identity_and_blocks_renamed_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_error_code: str,
) -> None:
    requests: list[tuple[str, str]] = []

    class _HttpClient:
        def __enter__(self) -> _HttpClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def request(self, method: str, path: str, *, json: Any = None) -> httpx.Response:
            del json
            requests.append((method, path))
            if failure == "transport":
                raise httpx.ConnectError("gateway connection reset")
            if failure == "http_502":
                return httpx.Response(502, json={"detail": {"error": {"code": "SLURM_PARSE_ERROR"}}})
            return httpx.Response(200, json={"status": "submitted"})

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: _HttpClient())
    root = tmp_path / "journal"
    initial_run_id = f"cycle_gfs_{_CYCLE}_convert_cohort_a"
    repository = FileOrchestrationJournalRepository(root)
    initial = _orchestrator(
        tmp_path / "initial",
        repository,
        HttpSlurmGatewayClient("http://gateway.test"),
        terminal_stage="forecast",
    )
    context = _forcing_context(
        initial,
        _forcing_basins(2, orchestration_run_id=initial_run_id),
        run_id=initial_run_id,
    )

    result, aggregation = initial._submit_and_wait_cycle_stage(M3_STAGES[1], context)

    assert aggregation is None
    assert result.status == "submit_result_ambiguous"
    assert result.error_code == expected_error_code
    durable = repository.get_pipeline_job(result.pipeline_job_id)
    assert durable is not None
    assert durable["status"] == "reserved"
    assert durable["submit_outcome"] == "submit_result_ambiguous"
    assert durable["slurm_job_id"] is None
    assert durable["error_code"] == expected_error_code
    assert durable["error_message"]
    assert durable.get("accepted_submit_contract_version") is None
    assert durable.get("cohort_digest") is None
    assert durable["restart_stage"] == "forcing"
    assert [member["array_task_id"] for member in durable["cohort_members"]] == [0, 1]
    assert [member["model_id"] for member in durable["cohort_members"]] == ["model_0", "model_1"]
    assert all(member["restart_stage"] == "forcing" for member in durable["cohort_members"])
    assert repository.reclaim_pipeline_job_reservation(dict(durable)) is None

    renamed_run_id = f"cycle_gfs_{_CYCLE}_forcing_cohort_b"
    restarted = _orchestrator(
        tmp_path / "restarted",
        FileOrchestrationJournalRepository(root),
        HttpSlurmGatewayClient("http://gateway.test"),
        terminal_stage="forecast",
    )
    restarted_result = restarted.orchestrate_cycle(
        "gfs",
        _CYCLE,
        list(reversed(_forcing_basins(2, orchestration_run_id=renamed_run_id))),
    )

    assert restarted_result.status == "reconciling"
    assert [stage.stage for stage in restarted_result.stages] == ["forcing"]
    assert restarted_result.stages[0].pipeline_job_id == durable["job_id"]
    assert restarted_result.stages[0].slurm_job_id == ""
    assert _pipeline_result_slurm_submit_called(restarted_result) == UNKNOWN_AFTER_ATTEMPT
    assert requests == [("POST", "/api/v1/slurm/job-arrays")]


def test_confirmed_forcing_attempt_resumes_in_original_order_and_continues_to_forecast(
    tmp_path: Path,
) -> None:
    class _AcceptedThenLostForcingClient(FakeCycleSlurmClient):
        def submit_job_array(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            accepted = super().submit_job_array(*args, **kwargs)
            if kwargs["stage_name"] == "forcing":
                raise OrchestratorError(
                    "SLURM_GATEWAY_UNAVAILABLE",
                    "forcing response was lost after Gateway acceptance",
                )
            return accepted

    root = tmp_path / "journal"
    initial_run_id = f"cycle_gfs_{_CYCLE}_convert_cohort_a"
    repository = FileOrchestrationJournalRepository(root)
    client = _AcceptedThenLostForcingClient()
    initial = _orchestrator(tmp_path / "initial", repository, client, terminal_stage="forecast")

    initial_result = initial.orchestrate_cycle(
        "gfs",
        _CYCLE,
        _forcing_basins(2, orchestration_run_id=initial_run_id),
    )

    assert initial_result.status == "reconciling"
    forcing = next(job for job in repository.query_pipeline_jobs_by_cycle(_CYCLE_ID) if job["stage"] == "forcing")
    assert forcing["submit_outcome"] == "submit_result_ambiguous"
    assert forcing["slurm_job_id"] is None

    accepted = SacctRecord(
        "2001",
        "RUNNING",
        "nhms_forcing",
        comment=forcing["slurm_comment"],
        run_id=forcing["run_id"],
        stage="forcing",
        pipeline_job_id=forcing["job_id"],
    )
    assert reconcile_reserved_unbound_jobs(repository, comment_query=lambda _key: accepted)[0].action == "bound"

    completed = SacctRecord(
        "2001",
        "COMPLETED",
        "nhms_forcing",
        comment=forcing["slurm_comment"],
        run_id=forcing["run_id"],
        stage="forcing",
        pipeline_job_id=forcing["job_id"],
    )
    assert reconcile_inflight_jobs(repository, sacct_query=lambda _job_id: completed)[0].action == "terminal"
    bound = repository.get_pipeline_job(forcing["job_id"])
    assert bound is not None
    assert bound["status"] == "succeeded"
    assert bound["submit_outcome"] == "accepted"
    assert bound["reconciliation_decision"] == "matched_bound"
    assert bound["matched_slurm_job_id"] == "2001"

    # The fake Gateway is the normal task-accounting/witness seam on resume. Its
    # accepted original job completed even though the initial response was lost.
    client.jobs["2001"]["status"] = "succeeded"
    resumed = _orchestrator(
        tmp_path / "resumed",
        FileOrchestrationJournalRepository(root),
        client,
        terminal_stage="forecast",
    )
    renamed_run_id = f"cycle_gfs_{_CYCLE}_forcing_cohort_b"
    resumed_result = resumed.orchestrate_cycle(
        "gfs",
        _CYCLE,
        list(reversed(_forcing_basins(2, orchestration_run_id=renamed_run_id))),
    )

    assert resumed_result.status == "succeeded"
    assert [stage.stage for stage in resumed_result.stages] == ["forcing", "forecast"]
    assert resumed_result.stages[0].pipeline_job_id == forcing["job_id"]
    assert resumed_result.stages[0].slurm_job_id == "2001"
    assert [submission["stage"] for submission in client.submissions] == ["forcing", "forecast"]


def _forcing_members(model_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        {
            "array_task_id": index,
            "candidate_id": f"gfs:2026-05-01T00:00:00Z:{model_id}:forecast_gfs_deterministic",
            "run_id": f"fcst_gfs_{_CYCLE}_{model_id}",
            "model_id": model_id,
            "basin_id": f"basin_{model_id}",
            "scenario_id": "forecast_gfs_deterministic",
            "restart_stage": "forcing",
        }
        for index, model_id in enumerate(model_ids)
    ]


def _reserve_forcing(
    repository: FileOrchestrationJournalRepository,
    *,
    suffix: str,
    model_ids: tuple[str, ...],
):
    run_id = f"cycle_gfs_{_CYCLE}_forcing_{suffix}"
    idempotency_key = f"{run_id}:forcing"
    return reserve_candidate(
        repository,
        idempotency_key=idempotency_key,
        job_id=f"job_{run_id}",
        run_id=run_id,
        cycle_id=_CYCLE_ID,
        job_type="produce_forcing_array",
        model_id=None,
        stage="forcing",
        candidate_id=run_id,
        reservation_evidence={
            "slurm_comment": slurm_comment_for(idempotency_key),
            "cohort_members": _forcing_members(model_ids),
            "restart_stage": "forcing",
            "submission_attempt": 1,
            "submission_attempt_started_at": _CYCLE_TIME,
            "slurm_ownership_required": False,
            "expected_slurm_user": None,
            "expected_slurm_account": None,
        },
    )


def test_forcing_member_reservation_is_atomic_across_keys_and_leaves_disjoint_work_eligible(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    barrier = Barrier(2)

    def reserve_overlapping(suffix: str):
        repository = FileOrchestrationJournalRepository(root)
        barrier.wait()
        return _reserve_forcing(
            repository,
            suffix=suffix,
            model_ids=("model_0", "model_1"),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve_overlapping, ("convert_cohort", "forcing_cohort")))

    admitted = [outcome for outcome in outcomes if outcome.created]
    blocked = [outcome for outcome in outcomes if not outcome.created]
    assert len(admitted) == 1
    assert len(blocked) == 1
    assert blocked[0].blocking_job_id == admitted[0].job_id
    assert _reserve_forcing(
        FileOrchestrationJournalRepository(root),
        suffix="disjoint",
        model_ids=("model_2",),
    ).created


def test_forcing_ambiguity_retries_only_after_coverage_proves_absence(tmp_path: Path) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    reservation = _reserve_forcing(
        repository,
        suffix="absence",
        model_ids=("model_0",),
    )
    transition = repository.transition_pipeline_job_submit_evidence(
        reservation.job_id,
        AcceptedSubmitTransition.timeout(),
        expected_submission_attempt=reservation.submission_attempt,
        expected_statuses=("reserved",),
        require_unbound=True,
        error_code="SLURM_PARSE_ERROR",
        error_message="Gateway response was empty after submission.",
    )
    assert transition.committed
    held = repository.query_reserved_unbound_jobs()[0]
    anchor = held.submission_attempt_started_at

    incomplete = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=lambda _key, **_kwargs: CommentAccountingResult(
            (),
            scope="global",
            coverage_start=anchor,
            coverage_end=anchor,
            coverage_complete=False,
        ),
    )[0]
    assert incomplete.action == "absence_unconfirmed"
    assert repository.get_pipeline_job(reservation.job_id)["status"] == "reserved"

    proven_absent = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=lambda _key, **_kwargs: CommentAccountingResult(
            (),
            scope="global",
            coverage_start=anchor,
            coverage_end=anchor,
            coverage_complete=True,
        ),
    )[0]
    assert proven_absent.action == "absence_retry_permitted"
    assert repository.get_pipeline_job(reservation.job_id)["status"] == "reservation_lost"

    retried = _reserve_forcing(
        repository,
        suffix="absence",
        model_ids=("model_0",),
    )
    assert retried.created
    assert retried.submission_attempt == 2
    reopened = repository.get_pipeline_job(reservation.job_id)
    assert reopened is not None
    assert reopened["submit_outcome"] is None
    assert reopened["reconciliation_decision"] is None


def test_sparse_historical_forcing_failure_does_not_block_a_new_successful_attempt(tmp_path: Path) -> None:
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    historical_job_id = f"job_cycle_gfs_{_CYCLE}_forcing_historical"
    repository.append_historical_pipeline_job(
        {
            "job_id": historical_job_id,
            "run_id": f"cycle_gfs_{_CYCLE}_forcing_historical",
            "cycle_id": _CYCLE_ID,
            "job_type": "produce_forcing_array",
            "model_id": None,
            "stage": "forcing",
            "status": "permanently_failed",
            "slurm_job_id": None,
            "error_code": "SLURM_PARSE_ERROR",
        }
    )

    replacement = _reserve_forcing(repository, suffix="replacement", model_ids=("model_0",))

    assert replacement.created
    assert repository.bind_pipeline_job_reservation(
        replacement.idempotency_key,
        slurm_job_id="3001",
    ) is not None
    _previous, completed = repository.update_pipeline_job_status(replacement.job_id, "succeeded")
    assert completed["status"] == "succeeded"
    historical = repository.get_pipeline_job(historical_job_id)
    assert historical is not None
    assert historical["status"] == "permanently_failed"


def test_real_http_forcing_rejection_keeps_rejected_semantics_without_ambiguity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HttpClient:
        def __enter__(self) -> _HttpClient:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def request(self, method: str, path: str, *, json: Any = None) -> httpx.Response:
            del method, path, json
            return httpx.Response(422, json={"error": {"code": "VALIDATION_ERROR"}})

    monkeypatch.setattr(httpx, "Client", lambda **_kwargs: _HttpClient())
    repository = FileOrchestrationJournalRepository(tmp_path / "journal")
    run_id = f"cycle_gfs_{_CYCLE}_forcing_rejection"
    orchestrator = _orchestrator(
        tmp_path,
        repository,
        HttpSlurmGatewayClient("http://gateway.test"),
        terminal_stage="forecast",
    )
    context = _forcing_context(
        orchestrator,
        _forcing_basins(1, orchestration_run_id=run_id),
        run_id=run_id,
    )

    result, aggregation = orchestrator._submit_and_wait_cycle_stage(M3_STAGES[1], context)

    assert aggregation is None
    assert result.status == "submission_failed"
    assert result.error_code == "VALIDATION_ERROR"
    durable = repository.get_pipeline_job(result.pipeline_job_id)
    assert durable is not None
    assert durable["status"] == "submission_failed"
    assert durable.get("submit_outcome") is None
    assert durable["slurm_job_id"] is None
    assert durable["cohort_members"]
    assert repository.query_reserved_unbound_jobs() == []
