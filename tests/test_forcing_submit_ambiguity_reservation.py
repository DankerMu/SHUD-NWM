"""Forcing submit ambiguity: member reservation, coverage-gated retry and rejection semantics.

Partition of ``tests/test_forcing_submit_ambiguity.py`` (#3177). The cycle
constants and forcing fixtures are in ``tests/forcing_submit_ambiguity_helpers.py``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Barrier
from typing import Any

import httpx
import pytest

from services.orchestrator.accepted_submit_identity import AcceptedSubmitTransition
from services.orchestrator.chain import M3_STAGES, HttpSlurmGatewayClient
from services.orchestrator.file_orchestration_journal import (
    FileOrchestrationJournalRepository,
)
from services.orchestrator.reconcile import (
    RESERVATION_ABSENCE_GRACE,
    CommentAccountingResult,
    reconcile_reserved_unbound_jobs,
)
from tests.forcing_submit_ambiguity_helpers import (
    _CYCLE,
    _CYCLE_ID,
    _expected_forcing_attempt_comment,
    _forcing_basins,
    _forcing_context,
    _reserve_forcing,
)
from tests.test_orchestration_chain import _orchestrator


def test_forcing_member_reservation_is_atomic_across_keys_and_leaves_disjoint_work_eligible(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    barrier = Barrier(2)

    class _BarrierRepository(FileOrchestrationJournalRepository):
        def reserve_pipeline_job(self, record: dict[str, Any]) -> dict[str, Any] | None:
            barrier.wait()
            return super().reserve_pipeline_job(record)

    def reserve_overlapping(suffix: str):
        repository = _BarrierRepository(root)
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

    def unexpected_query(*_args: Any, **_kwargs: Any) -> CommentAccountingResult:
        pytest.fail("a changed attempt must not reach accounting")

    assert (
        reconcile_reserved_unbound_jobs(
            repository,
            comment_query=unexpected_query,
            target_job_id=reservation.job_id,
            target_submission_attempt=reservation.submission_attempt + 1,
            target_submission_attempt_started_at=anchor,
            target_slurm_comment=held.slurm_comment,
            bind_only=True,
        )
        == []
    )
    boundary_absence = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=lambda _key, **_kwargs: CommentAccountingResult(
            (),
            scope="global",
            coverage_start=anchor,
            coverage_end=anchor,
            coverage_complete=True,
        ),
        now=lambda: anchor + RESERVATION_ABSENCE_GRACE + timedelta(seconds=1),
        target_job_id=reservation.job_id,
        target_submission_attempt=reservation.submission_attempt,
        target_submission_attempt_started_at=anchor,
        target_slurm_comment=held.slurm_comment,
        bind_only=True,
    )[0]
    assert boundary_absence.action == "absence_unconfirmed"
    assert repository.get_pipeline_job(reservation.job_id)["status"] == "reserved"


    def token_without_scope(
        _key: str,
        *,
        forcing_exact_comment: str | None = None,
        forcing_current_attempt_comment: bool = False,
    ) -> CommentAccountingResult:
        del forcing_exact_comment, forcing_current_attempt_comment
        return CommentAccountingResult(
            (),
            scope="global",
            coverage_start=anchor,
            coverage_end=anchor,
            coverage_complete=True,
        )

    unsupported_adapter = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=token_without_scope,
        now=lambda: anchor + RESERVATION_ABSENCE_GRACE + timedelta(seconds=1),
    )[0]
    assert unsupported_adapter.action == "query_unavailable"
    assert repository.get_pipeline_job(reservation.job_id)["status"] == "reserved"


    incomplete = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=lambda _key, **_kwargs: CommentAccountingResult(
            (),
            scope="global",
            coverage_start=anchor,
            coverage_end=anchor,
            coverage_complete=False,
        ),
        now=lambda: anchor + timedelta(seconds=1),
    )[0]
    assert incomplete.action == "absence_unconfirmed"
    assert repository.get_pipeline_job(reservation.job_id)["status"] == "reserved"

    young_absence = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=lambda _key, **_kwargs: CommentAccountingResult(
            (),
            scope="global",
            coverage_start=anchor,
            coverage_end=anchor,
            coverage_complete=True,
        ),
        now=lambda: anchor + timedelta(seconds=1),
    )[0]
    assert young_absence.action == "absence_unconfirmed"
    held_after_young_absence = repository.get_pipeline_job(reservation.job_id)
    assert held_after_young_absence is not None
    assert held_after_young_absence["status"] == "reserved"
    assert repository.reclaim_pipeline_job_reservation(dict(held_after_young_absence)) is None

    proven_absent = reconcile_reserved_unbound_jobs(
        repository,
        comment_query=lambda _key, **_kwargs: CommentAccountingResult(
            (),
            scope="global",
            coverage_start=anchor,
            coverage_end=anchor,
            coverage_complete=True,
        ),
        now=lambda: anchor + RESERVATION_ABSENCE_GRACE + timedelta(seconds=1),
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
    assert reopened["slurm_comment"] == _expected_forcing_attempt_comment(
        retried.idempotency_key,
        retried.submission_attempt,
    )


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
