"""#1564 demote-reserved-job: reclaim doors, the full chain, and automatic-path regression.

A demoted row is a dead reservation the next pass takes over atomically
instead of reporting ``PIPELINE_ALREADY_ACTIVE``; both reclaim doors accept
exactly the two absence decisions and mint ``durable old attempt + 1`` from
the lock-owned state only.  The automatic absence path and the PostgreSQL /
chain-facade absence stay unchanged.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from services.orchestrator import file_orchestration_journal as journal_module
from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    OPERATOR_VERIFIED_ABSENCE_DECISION,
)
from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalRepository
from tests.orchestrator_demote_reserved_job_helpers import (
    JOB_ID,
    STARTED_AT,
    _absence_row,
    _demote_kwargs,
    _held_cohort_repository,
    _held_row,
)


def test_operator_demoted_row_reclaims_to_fresh_attempt_and_locked_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _held_cohort_repository(tmp_path)
    demoted = _absence_row(repository, decision=OPERATOR_VERIFIED_ABSENCE_DECISION)
    assert demoted["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
    request_anchor = STARTED_AT + timedelta(hours=3)
    locked_anchor = STARTED_AT + timedelta(hours=4)
    request = {
        **demoted,
        "expected_submission_attempt": demoted["submission_attempt"],
        "expected_submission_attempt_started_at": demoted["submission_attempt_started_at"],
        "status": "reserved",
        "submission_attempt": int(demoted["submission_attempt"]) + 1,
        "submission_attempt_started_at": request_anchor,
        "submit_outcome": None,
        "reconciliation_source": None,
        "reconciliation_decision": None,
        "matched_slurm_job_id": None,
    }

    monkeypatch.setattr(journal_module, "_utcnow", lambda: locked_anchor)
    reclaimed = repository.reclaim_pipeline_job_reservation(request)

    assert reclaimed is not None
    assert reclaimed["status"] == "reserved"
    assert reclaimed["submission_attempt"] == int(demoted["submission_attempt"]) + 1
    assert reclaimed["submission_attempt_started_at"] == locked_anchor.isoformat().replace("+00:00", "Z")
    assert reclaimed["submission_attempt_started_at"] != request_anchor.isoformat().replace("+00:00", "Z")
    assert reclaimed["reconciliation_decision"] is None
    assert reclaimed["reconciliation_reason_class"] is None
    # Immutable cohort identity survives reclaim.
    assert reclaimed["cohort_digest"] == demoted["cohort_digest"]
    assert reclaimed["cohort_members"] == demoted["cohort_members"]
    assert reclaimed["job_id"] == demoted["job_id"]
    # A stale attempt cannot reclaim the demoted row.
    stale = repository.reclaim_pipeline_job_reservation({**request, "expected_submission_attempt": 1})
    assert stale is None
    assert repository.get_accepted_submit_pipeline_job(JOB_ID)["status"] == "reserved"


def test_reclaim_derives_versioned_master_new_attempt_from_durable_state_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2.1/2.3: a versioned master's new attempt is lock-owned, never request-fed.

    Both reclaim doors (operator demoted and automatic absence) must mint
    ``durable old attempt + 1`` unconditionally -- exactly once, with a fresh
    locked anchor -- ignoring any ``submission_attempt`` the lock-external
    request carries, whether larger (999), equal, or lower/stale.
    """

    def _reclaim_request(row: dict[str, Any], proposed_attempt: int) -> dict[str, Any]:
        return {
            **row,
            "expected_submission_attempt": row["submission_attempt"],
            "expected_submission_attempt_started_at": row["submission_attempt_started_at"],
            "status": "reserved",
            "submission_attempt": proposed_attempt,
            "submission_attempt_started_at": STARTED_AT + timedelta(hours=3),
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "matched_slurm_job_id": None,
        }

    for index, decision in enumerate((OPERATOR_VERIFIED_ABSENCE_DECISION, "absence_retry_permitted")):
        # 999 = hostile larger, 2 = equal to durable+1, 1 = lower/stale; every
        # request still carries the exact durable expected attempt/anchor.
        for proposed in (999, 2, 1):
            repository = _held_cohort_repository(tmp_path / f"case-{index}-{proposed}")
            held = _held_row(repository)
            if decision == OPERATOR_VERIFIED_ABSENCE_DECISION:
                terminal = _absence_row(repository, decision=OPERATOR_VERIFIED_ABSENCE_DECISION)
            else:
                assert repository.permit_pipeline_job_retry(
                    JOB_ID,
                    accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
                    expected_submission_attempt=1,
                    expected_submission_attempt_started_at=held["submission_attempt_started_at"],
                ) == 1
                terminal = _held_row(repository)
            assert terminal["status"] == "reservation_lost"
            assert terminal["reconciliation_decision"] == decision
            assert terminal["submission_attempt"] == 1

            locked_anchor = STARTED_AT + timedelta(hours=4 + index * 10 + proposed)
            monkeypatch.setattr(journal_module, "_utcnow", lambda: locked_anchor)
            reclaimed = repository.reclaim_pipeline_job_reservation(
                _reclaim_request(terminal, proposed)
            )
            assert reclaimed is not None, (decision, proposed)
            # New attempt is durable old + 1 exactly once, whatever the request said.
            assert reclaimed["submission_attempt"] == 2, (decision, proposed)
            assert reclaimed["submission_attempt_started_at"] == locked_anchor.isoformat().replace(
                "+00:00", "Z"
            )
            assert reclaimed["reconciliation_decision"] is None
            assert reclaimed["reconciliation_reason_class"] is None
            # Immutable identity survives reclaim.
            assert reclaimed["cohort_digest"] == terminal["cohort_digest"]
            assert reclaimed["cohort_members"] == terminal["cohort_members"]
            assert reclaimed["job_id"] == terminal["job_id"]
            assert reclaimed["idempotency_key"] == terminal["idempotency_key"]


def test_cycle_retry_shortcut_accepts_operator_absence_and_keeps_identity_release_false(
    tmp_path: Path,
) -> None:
    from services.orchestrator import chain as _chain  # import order: chain owns the cycle mixin
    from services.orchestrator.chain_forecast_orchestrator_cycle import (
        _verified_accepted_submit_forecast_retry,
    )

    del _chain
    repository = _held_cohort_repository(tmp_path)
    demoted = _absence_row(repository, decision=OPERATOR_VERIFIED_ABSENCE_DECISION)
    assert demoted["reconciliation_decision"] == OPERATOR_VERIFIED_ABSENCE_DECISION
    assert _verified_accepted_submit_forecast_retry(demoted) is True

    # Automatic absence stays accepted.
    automatic = _held_cohort_repository(tmp_path / "automatic")
    row = automatic.get_accepted_submit_pipeline_job(JOB_ID)
    assert automatic.permit_pipeline_job_retry(
        JOB_ID,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_submission_attempt_started_at=row["submission_attempt_started_at"],
    ) == 1
    permitted = automatic.get_accepted_submit_pipeline_job(JOB_ID)
    assert permitted["reconciliation_decision"] == "absence_retry_permitted"
    assert _verified_accepted_submit_forecast_retry(permitted) is True

    # Identity release and every other terminal shape stay false.  The release
    # CAS requires the exact current status, which is the permitted row's own.
    released = automatic.release_identity_blocked_reservation(
        JOB_ID,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_submission_attempt_started_at=permitted["submission_attempt_started_at"],
        expected_status=str(permitted["status"]),
        identity_blocked_streak=3,
    )
    assert released == 1
    assert _verified_accepted_submit_forecast_retry(automatic.get_accepted_submit_pipeline_job(JOB_ID)) is False
    for decision in ("identity_mismatch_released", "matched_bound", "accounting_unavailable"):
        row_copy = dict(demoted)
        row_copy["reconciliation_decision"] = decision
        assert _verified_accepted_submit_forecast_retry(row_copy) is False


def test_cycle_retry_shortcut_rejects_non_absence_reservation_lost_sub_shapes(
    tmp_path: Path,
) -> None:
    from services.orchestrator.chain_forecast_orchestrator_cycle import (
        _verified_accepted_submit_forecast_retry,
    )

    repository = _held_cohort_repository(tmp_path)
    demoted = _absence_row(repository, decision=OPERATOR_VERIFIED_ABSENCE_DECISION)
    # The shortcut's own predicate gates on outcome/source/decision/unbound and
    # cohort identity; the caller gates on status, and the REclaim door gates on
    # reason-null/attempt/anchor.  Verify the shortcut's actual checks here.
    for key, value in (
        ("matched_slurm_job_id", "17667"),
        ("reconciliation_source", "slurm_comment_only"),
        ("reconciliation_decision", "identity_mismatch_released"),
        ("reconciliation_decision", "accounting_unavailable"),
        ("reconciliation_decision", "matched_bound"),
        ("submit_outcome", "rejected"),
    ):
        row_copy = dict(demoted)
        row_copy[key] = value
        assert _verified_accepted_submit_forecast_retry(row_copy) is False
    # ``accepted`` outcome remains allowed by the existing shortcut predicate
    # (D4 keeps every other outcome/identity check unchanged).
    row_accepted = dict(demoted)
    row_accepted["submit_outcome"] = "accepted"
    assert _verified_accepted_submit_forecast_retry(row_accepted) is True


def test_full_chain_held_demote_reclaim_resubmits_without_pipeline_already_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real held row -> operator demotion -> reclaim -> fresh reserved attempt.

    The reclaim door is the file-journal CAS that the production reserve gate
    drives (``reservation.py`` ``reserve_candidate``): a demoted row is a dead
    reservation the next pass takes over atomically instead of reporting
    ``PIPELINE_ALREADY_ACTIVE``.
    """

    from services.orchestrator.reservation import reserve_candidate

    repository = _held_cohort_repository(tmp_path, member_count=1)
    held = _held_row(repository)
    assert repository.demote_operator_verified_reserved_job(
        JOB_ID,
        **_demote_kwargs(held),
    ) is not None
    demoted = _held_row(repository)
    assert demoted["status"] == "reservation_lost"
    # Before the reclaim the row is a terminal, so it is no longer "active" and
    # the cycle is not wedged on PIPELINE_ALREADY_ACTIVE — the exact #1116
    # wedge the operator demotion opens.
    from services.orchestrator import chain_runtime_utils

    assert (
        chain_runtime_utils._active_orchestration_conflicts(
            repository,
            source_id="GFS",
            cycle_time=STARTED_AT,
            cycle_id="gfs_2026071200",
            run_id=str(demoted["run_id"]),
            basins=[{"model_id": "model_0", "basin_id": "basin_0", "orchestration_run_id": demoted["run_id"]}],
        )
        is False
    )

    locked_anchor = STARTED_AT + timedelta(hours=5)
    monkeypatch.setattr(journal_module, "_utcnow", lambda: locked_anchor)
    result = reserve_candidate(
        repository,
        job_id=JOB_ID,
        run_id=str(demoted["run_id"]),
        cycle_id=str(demoted["cycle_id"]),
        job_type=str(demoted["job_type"]),
        model_id=demoted["model_id"],
        stage=str(demoted["stage"]),
        idempotency_key=str(demoted["idempotency_key"]),
        candidate_id=demoted["candidate_id"],
        reservation_evidence={
            "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
            "slurm_comment": demoted["slurm_comment"],
            "cohort_members": demoted["cohort_members"],
            "cohort_digest": demoted["cohort_digest"],
            "restart_stage": demoted["restart_stage"],
            "submission_attempt": int(demoted["submission_attempt"]) + 1,
            "submission_attempt_started_at": locked_anchor,
            "slurm_ownership_required": bool(demoted.get("slurm_ownership_required")),
            "expected_slurm_user": demoted.get("expected_slurm_user"),
            "expected_slurm_account": demoted.get("expected_slurm_account"),
            # The held fixture row records no ``native_shud_resubmitted``, so the
            # durable value is None; omitting it lets the reclaim request row
            # normalize to None and match the immutable identity.
        },
    )
    assert result.created is True
    assert result.submission_attempt == int(demoted["submission_attempt"]) + 1
    assert result.status == "reserved"
    reopened = FileOrchestrationJournalRepository(repository.root)
    current = reopened.get_accepted_submit_pipeline_job(JOB_ID)
    assert current["status"] == "reserved"
    assert current["submission_attempt"] == int(demoted["submission_attempt"]) + 1
    assert current["submission_attempt_started_at"] == locked_anchor.isoformat().replace("+00:00", "Z")
    assert current["reconciliation_decision"] is None
    # The row is live again under a fresh attempt: it is reserved-unbound (so it
    # appears in the reserved-unbound query), but the next pass's reconcile can
    # bind it via the exact-comment query instead of wedging on the terminal.
    reserved_unbound = reopened.query_reserved_unbound_jobs()
    assert [job.job_id for job in reserved_unbound] == [JOB_ID]
    assert reserved_unbound[0].submission_attempt == int(demoted["submission_attempt"]) + 1
    assert str(reserved_unbound[0].status) == "reserved"


# ---------------------------------------------------------------------------
# 4.3 regression: automatic path unchanged, PostgreSQL unchanged
# ---------------------------------------------------------------------------
def test_automatic_absence_retry_permitted_still_reclaims(tmp_path: Path) -> None:
    repository = _held_cohort_repository(tmp_path)
    row = _held_row(repository)
    assert repository.permit_pipeline_job_retry(
        JOB_ID,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_submission_attempt_started_at=row["submission_attempt_started_at"],
    ) == 1
    permitted = _held_row(repository)
    assert permitted["status"] == "reservation_lost"
    assert permitted["reconciliation_decision"] == "absence_retry_permitted"
    assert permitted["reconciliation_reason_class"] is None
    reclaimed = repository.reclaim_pipeline_job_reservation(
        {
            **permitted,
            "expected_submission_attempt": permitted["submission_attempt"],
            "expected_submission_attempt_started_at": permitted["submission_attempt_started_at"],
            "status": "reserved",
            "submission_attempt": int(permitted["submission_attempt"]) + 1,
            "submit_outcome": None,
            "reconciliation_source": None,
            "reconciliation_decision": None,
            "matched_slurm_job_id": None,
        }
    )
    assert reclaimed is not None
    assert reclaimed["status"] == "reserved"
    assert reclaimed["submission_attempt"] == 2


def test_identity_mismatch_released_stays_non_reclaimable(tmp_path: Path) -> None:
    repository = _held_cohort_repository(tmp_path)
    row = _held_row(repository)
    assert repository.release_identity_blocked_reservation(
        JOB_ID,
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_submission_attempt_started_at=row["submission_attempt_started_at"],
        identity_blocked_streak=3,
    ) == 1
    released = _held_row(repository)
    assert released["reconciliation_decision"] == "identity_mismatch_released"
    assert (
        repository.reclaim_pipeline_job_reservation(
            {
                **released,
                "expected_submission_attempt": released["submission_attempt"],
                "expected_submission_attempt_started_at": released["submission_attempt_started_at"],
                "status": "reserved",
                "submission_attempt": 2,
                "submit_outcome": None,
                "reconciliation_source": None,
                "reconciliation_decision": None,
                "matched_slurm_job_id": None,
                "identity_blocked_streak": 0,
            }
        )
        is None
    )
    assert _held_row(repository) == released


def test_comment_less_reconcile_keeps_the_held_shape_and_never_infers_absence(
    tmp_path: Path,
) -> None:
    """#1116 preservation: without an operator command the row stays held."""
    repository = _held_cohort_repository(tmp_path)
    held = _held_row(repository)
    assert held["status"] == "reserved"
    assert held["reconciliation_decision"] == "accounting_unavailable"
    assert held["reconciliation_reason_class"] == "comment_accounting_unproven"
    assert held["slurm_job_id"] is None
    # No demotion token ever enters the row through reconcile.
    assert held["reconciliation_decision"] != OPERATOR_VERIFIED_ABSENCE_DECISION


def test_pg_repository_and_chain_facade_have_no_demote_capability() -> None:
    """4.3: the #1564 demotion is file-journal-only; PostgreSQL surfaces gain nothing.

    The operator demotion is deliberately absent from the PostgreSQL repository
    implementation and from the shared ``OrchestratorRepository`` protocol /
    chain facade, so no caller reaching a non-file-journal repository can ever
    issue an ``operator_verified_absence`` demotion.
    """

    from services.orchestrator.chain import OrchestratorRepository
    from services.orchestrator.chain_repository import PsycopgOrchestratorRepository

    assert not hasattr(OrchestratorRepository, "demote_operator_verified_reserved_job")
    assert not hasattr(PsycopgOrchestratorRepository, "demote_operator_verified_reserved_job")
    # The shared chain facade re-exports the same protocol / implementation.
    from services.orchestrator import chain as chain_facade

    assert chain_facade.OrchestratorRepository is OrchestratorRepository
    assert chain_facade.PsycopgOrchestratorRepository is PsycopgOrchestratorRepository
    assert not hasattr(chain_facade, "demote_operator_verified_reserved_job")
