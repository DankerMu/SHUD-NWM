"""Shared helpers of the binding-provenance suite (#3177).

Split out of ``tests/test_gateway_reconcile_binding_provenance.py``; its
partitions (``tests/test_gateway_reconcile_binding_provenance*.py``) import the
additive field names and the projection, fallback and sibling-reservation
helpers from here. Not a test module: pytest does not collect it. The
same-named ``_fallback_row`` / ``_fallback_querier`` of the claimant and
comment-accounting suites have different bodies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.orchestrator.accepted_submit_identity import ACCEPTED_SUBMIT_CONTRACT_VERSION, AcceptedSubmitTransition
from tests.gateway_reconcile_helpers import _append_cohort_placeholders, _versioned_master_reservation_record

_ANCHOR = datetime(2026, 7, 12, tzinfo=UTC)

# #1850 Fix A additive field names. Imported lazily in each test so the module
# stays collectible against pre-change source (the red proof half of the
# regression matrix must fail on the FIXED assertions, not on collection).
def _SLURM_BINDING_SOURCE_FIELD() -> str:
    from services.orchestrator.accepted_submit_identity import SLURM_BINDING_SOURCE_FIELD

    return SLURM_BINDING_SOURCE_FIELD


def _SLURM_ACCOUNTING_SUBMITTED_AT_FIELD() -> str:
    from services.orchestrator.accepted_submit_identity import (
        SLURM_ACCOUNTING_SUBMITTED_AT_FIELD,
    )

    return SLURM_ACCOUNTING_SUBMITTED_AT_FIELD


def _single_projection(repository: Any, job_id: str) -> tuple[list[dict[str, Any]], bool]:
    """A single-member complete cohort projection, mirroring terminal reconcile."""
    row = repository.get_pipeline_job(job_id)
    members = row["cohort_members"]
    projections = [
        {
            "candidate_id": members[0].get("candidate_id"),
            "run_id": members[0].get("run_id"),
            "model_id": members[0].get("model_id"),
            "array_task_id": members[0]["array_task_id"],
            "array_task_outcome": "succeeded",
            "task_slurm_job_id": f"{row['slurm_job_id']}_{members[0]['array_task_id']}",
            "error_code": None,
            "restart_stage": "state_save_qc",
            "native_shud_resubmitted": False,
        }
    ]
    return projections, True


def _incomplete_projection(repository: Any, job_id: str) -> tuple[list[dict[str, Any]], bool]:
    """A single-member incomplete projection (outcome unverified)."""
    row = repository.get_pipeline_job(job_id)
    members = row["cohort_members"]
    projections = [
        {
            "candidate_id": members[0].get("candidate_id"),
            "run_id": members[0].get("run_id"),
            "model_id": members[0].get("model_id"),
            "array_task_id": members[0]["array_task_id"],
            "array_task_outcome": "unverified",
            "task_slurm_job_id": None,
            "error_code": None,
            "restart_stage": "forecast",
            "native_shud_resubmitted": False,
        }
    ]
    return projections, False


def _fallback_querier(monkeypatch: pytest.MonkeyPatch, *, rows: str, query_end: datetime) -> Any:
    from services.orchestrator import reconcile as reconcile_module

    monkeypatch.setattr("services.orchestrator.reconcile_comment_sacct._bounded_sacct_stdout", lambda command: rows)
    return reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=lambda: False,
        now=lambda: query_end,
    )


def _fallback_row(master_id: str, *, submit: str) -> str:
    return f"{master_id}|nhms_forecast|COMPLETED|0:0||scheduler|account|{submit}\n"


def _reserve_gfs_sibling(repository: Any, *, created_at: datetime) -> str:
    record = _versioned_master_reservation_record(
        created_at=created_at,
        member_count=1,
        expected_user="scheduler",
        expected_account="account",
        source_id="gfs",
    )
    repository.reserve_pipeline_job(record)
    repository.transition_pipeline_job_submit_evidence(
        record["job_id"],
        AcceptedSubmitTransition.timeout(),
        accepted_submit_contract_version=ACCEPTED_SUBMIT_CONTRACT_VERSION,
        expected_submission_attempt=1,
        expected_statuses=("reserved",),
        require_unbound=True,
    )
    _append_cohort_placeholders(repository, 1, source_id="gfs")
    return str(record["job_id"])
