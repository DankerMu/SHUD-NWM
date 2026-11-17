"""Shared helpers of the claimant-exclusivity suite (#3177).

Split out of ``tests/test_gateway_reconcile_claimant_exclusivity.py``; its
partitions (``tests/test_gateway_reconcile_claimant_*.py``) import from here.
Not a test module: pytest does not collect it.

``_fallback_row``, ``_fallback_querier`` and ``_second_master_reservation_record``
are also imported by ``tests/orchestrator_bind_reserved_job_helpers.py``,
``tests/test_gateway_reconcile_submitline_exact.py``,
``tests/test_orchestrator_bind_reserved_job_cas.py`` and
``tests/test_orchestrator_bind_reserved_job_lane.py``. Same-named helpers in the
binding-provenance and comment-accounting suites have different bodies and are
not interchangeable with these.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    AcceptedSubmitTransition,
    forecast_cohort_digest,
)
from tests.gateway_reconcile_helpers import _append_cohort_placeholders, _versioned_master_reservation_record


def _fallback_row(
    master_id: str,
    *,
    submit: str,
    user: str = "scheduler",
    account: str = "account",
    comment: str = "",
    job_name: str = "nhms_forecast",
    submit_line: str | None = None,
) -> str:
    """One ``--parsable2`` fallback row.

    ``submit_line=None`` keeps the pre-#2655 eight-field shape (older sacct /
    no ``SubmitLine`` column); a string appends it as the ninth field.
    """

    row = f"{master_id}|{job_name}|COMPLETED|0:0|{comment}|{user}|{account}|{submit}"
    if submit_line is not None:
        row += f"|{submit_line}"
    return row + "\n"


def _fallback_querier(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rows: str,
    query_end: datetime,
) -> Any:
    from services.orchestrator import reconcile as reconcile_module

    commands: list[list[str]] = []
    monkeypatch.setattr(
        "services.orchestrator.reconcile_comment_sacct._bounded_sacct_stdout",
        lambda command: commands.append(list(command)) or rows,
    )
    query = reconcile_module.default_comment_sacct_querier(
        global_visibility_probe=lambda: True,
        comment_storage_probe=lambda: False,
        now=lambda: query_end,
    )
    return query, commands


def _second_master_reservation_record(
    *,
    source_id: str = "ifs",
    created_at: datetime,
    member_count: int = 1,
    expected_user: str = "scheduler",
    expected_account: str = "account",
    submit_outcome: str | None = "submit_result_ambiguous",
) -> dict[str, Any]:
    """A second reserved-unbound forecast master for a sibling source/cycle.

    The GFS fixture owns ``cycle_gfs_2026071200``; this IFS fixture owns
    ``cycle_ifs_2026071200`` so both masters are current accepted-submit
    forecast cohorts with the same expected Slurm user/account.
    """

    record = _versioned_master_reservation_record(
        created_at=created_at,
        member_count=member_count,
        expected_user=expected_user,
        expected_account=expected_account,
        submit_outcome=submit_outcome,
        versioned=True,
        source_id=source_id,
    )
    canonical_source = source_id.upper() if source_id == "ifs" else source_id.upper()
    if source_id != "gfs":
        record = {**record}
        record["job_id"] = f"job_cycle_{source_id}_2026071200_forecast_fixture_forecast"
        record["run_id"] = f"cycle_{source_id}_2026071200_forecast_fixture"
        record["cycle_id"] = f"{source_id}_2026071200"
        record["idempotency_key"] = f"cycle_{source_id}_2026071200_forecast_fixture:forecast"
        record["slurm_comment"] = f"nhms_idem:cycle_{source_id}_2026071200_forecast_fixture:forecast"
        record["cohort_members"] = [
            {
                "array_task_id": member["array_task_id"],
                "candidate_id": (
                    f"{canonical_source}:2026-07-12T00:00:00Z:model_{member['array_task_id']}:"
                    f"{record['cohort_members'][0].get('scenario_id')}"
                ),
                "run_id": f"fcst_{source_id}_2026071200_model_{member['array_task_id']}",
                "model_id": f"model_{member['array_task_id']}",
                "basin_id": f"basin_{member['array_task_id']}",
                "scenario_id": member.get("scenario_id"),
                "restart_stage": "forecast",
            }
            for member in record["cohort_members"]
        ]
        record["cohort_digest"] = forecast_cohort_digest(record)
    return record


def _reserve_second_master(
    repository: Any,
    *,
    source_id: str,
    created_at: datetime,
    member_count: int = 1,
    expected_user: str = "scheduler",
    expected_account: str = "account",
) -> tuple[str, str]:
    """Reserve + timeout-transition a second master; return (job_id, key)."""

    record = _second_master_reservation_record(
        source_id=source_id,
        created_at=created_at,
        member_count=member_count,
        expected_user=expected_user,
        expected_account=expected_account,
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
    _append_cohort_placeholders(repository, member_count, source_id=source_id)
    return str(record["job_id"]), str(record["idempotency_key"])


def _reserve_gfs_master_on(
    repository: Any,
    *,
    created_at: datetime,
    member_count: int = 1,
    expected_user: str = "scheduler",
    expected_account: str = "account",
) -> tuple[str, str]:
    """Reserve + timeout-transition a GFS master onto an existing journal.

    Used by the tests that need a sibling GFS claimant created AFTER another
    source has already bound/owned a master.
    """

    record = _versioned_master_reservation_record(
        created_at=created_at,
        member_count=member_count,
        expected_user=expected_user,
        expected_account=expected_account,
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
    _append_cohort_placeholders(repository, member_count, source_id="gfs")
    return str(record["job_id"]), str(record["idempotency_key"])


def _file_cohort_single_projection(repository: Any, job_id: str) -> tuple[list[dict[str, Any]], bool]:
    """Build a single-member cohort projection the way the terminal reconcile does."""

    row = repository.get_pipeline_job(job_id)
    members = row["cohort_members"]
    projections = [
        {
            "candidate_id": members[0].get("candidate_id"),
            "run_id": members[0].get("run_id"),
            "model_id": members[0].get("model_id"),
            "array_task_id": members[0]["array_task_id"],
            "array_task_outcome": "succeeded",
            "task_slurm_job_id": f"72001_{members[0]['array_task_id']}",
            "error_code": None,
            "restart_stage": "state_save_qc",
            "native_shud_resubmitted": False,
        }
    ]
    return projections, True
