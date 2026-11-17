"""Restart reconcile of bound in-flight jobs (``reconcile_inflight_jobs``)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Mapping

from services.orchestrator.accepted_submit_identity import (
    AcceptedSubmitEvidenceError,
    forecast_cohort_identity_is_valid,
    is_forecast_cohort_stage_name,
    ordered_cohort_members,
)
from services.orchestrator.forcing_submit_identity import FORCING_EXACT_COMMENT_RECONCILIATION_SOURCES
from services.orchestrator.reconcile_identity import (
    _GENERIC_ARRAY_JOB_NAMES,
    _accepted_submit_versioned_job,
    _expected_job_name_token,
    _file_cohort_runtime_identity_matches,
    _forcing_accounting_identity_matches,
    _forcing_submit_reconcile_job,
    _identity_matches,
    _is_forecast_cohort_job,
)
from services.orchestrator.reconcile_records import (
    RECONCILE_UNVERIFIED_STATUS,
    ReconcileQueryUnavailable,
    SacctQuerier,
    SacctRecord,
)
from services.slurm_gateway.models import TERMINAL_STATUSES, SlurmJobStatus
from services.slurm_gateway.real_backend import SLURM_STATE_MAP, _normalize_slurm_state, map_slurm_error_code

LOGGER = logging.getLogger("services.orchestrator.reconcile")


@dataclass(frozen=True)
class ReconcileOutcome:
    job_id: str
    slurm_job_id: str
    # "terminal" | "still_running" | "unverified" | "query_unavailable"
    action: str
    status: str
    durable_write_kind: str | None = None
    durable_write_count: int = 0
    pipeline_status_write_count: int = 0
    pipeline_event_write_count: int = 0
    # #1795: optional stable clause-level reason for terminal file-cohort
    # identity blocks. Pass evidence only — never written to durable
    # accepted-submit state by the inflight leg.
    reconciliation_reason_class: str | None = None


def _sacct_query_job_id(job: Any) -> str:
    slurm_job_id = str(getattr(job, "slurm_job_id", "") or "")
    array_task_id = getattr(job, "array_task_id", None)
    if not slurm_job_id or "_" in slurm_job_id or array_task_id in (None, ""):
        return slurm_job_id
    return f"{slurm_job_id}_{array_task_id}"


def reconcile_inflight_jobs(
    store,
    *,
    sacct_query: SacctQuerier,
) -> list[ReconcileOutcome]:
    """Reconcile durable in-flight jobs against ``sacct``; never resubmit.

    For each in-flight ``pipeline_job`` (read from durable storage), query
    ``sacct`` for its ``slurm_job_id``, verify candidate identity, and update the
    durable status to the terminal/running state reported by accounting. Returns
    a per-job outcome. Submission is never triggered here.
    """

    outcomes: list[ReconcileOutcome] = []
    for job in store.query_inflight_jobs():
        slurm_job_id = job.slurm_job_id
        if not slurm_job_id:
            continue

        query_slurm_job_id = _sacct_query_job_id(job)
        try:
            record = sacct_query(query_slurm_job_id)
        except ReconcileQueryUnavailable as error:
            LOGGER.warning(
                "reconcile inflight query unavailable for %s: %s",
                query_slurm_job_id,
                error,
            )
            outcomes.append(
                ReconcileOutcome(
                    job_id=job.job_id,
                    slurm_job_id=str(slurm_job_id),
                    action="query_unavailable",
                    status=str(job.status),
                )
            )
            continue
        expected_token = _expected_job_name_token(job.stage, job.job_type)
        normalized = _normalize_slurm_state(record.raw_state) if record is not None else None
        slurm_status = (
            SLURM_STATE_MAP.get(normalized, SlurmJobStatus.FAILED)
            if normalized is not None
            else None
        )
        cohort_members = getattr(job, "cohort_members", None)
        file_cohort = bool(
            getattr(store, "supports_accepted_submit_reconcile", False)
            and _accepted_submit_versioned_job(job)
            and _is_forecast_cohort_job(job)
            and isinstance(cohort_members, Sequence)
            and not isinstance(cohort_members, str | bytes)
            and cohort_members
        )
        forcing_bound = _bound_forcing_submit_reconcile_job(job)
        requires_durable_identity = (
            slurm_status in TERMINAL_STATUSES and not file_cohort and not forcing_bound
        )
        identity_matches = (
            _forcing_accounting_identity_matches(
                record,
                job,
                require_exact_comment=False,
                require_bound_master=True,
            )
            if record is not None and forcing_bound
            else record is not None
            and _identity_matches(
                record,
                expected_token,
                job,
                require_durable_identity=requires_durable_identity,
            )
        )
        if not identity_matches:
            # Cannot prove this is our candidate: mark typed, do NOT resubmit.
            if file_cohort:
                write_count = _transition_file_runtime_status(
                    store,
                    job,
                    RECONCILE_UNVERIFIED_STATUS,
                )
            else:
                store.update_job_status(
                    job.job_id,
                    RECONCILE_UNVERIFIED_STATUS,
                    error_code="SLURM_RECONCILE_UNVERIFIED",
                    error_message=(f"sacct could not verify the candidate identity for slurm_job_id={slurm_job_id}."),
                )
                write_count = 1
            outcomes.append(
                ReconcileOutcome(
                    job_id=job.job_id,
                    slurm_job_id=str(slurm_job_id),
                    action="unverified",
                    status=RECONCILE_UNVERIFIED_STATUS,
                    durable_write_kind="pipeline_job_status",
                    durable_write_count=write_count,
                )
            )
            continue

        if file_cohort:
            identity_verdict, identity_reason = _terminal_file_cohort_identity_matches(store, record, job)
            if not identity_verdict:
                outcomes.append(
                    ReconcileOutcome(
                        job_id=job.job_id,
                        slurm_job_id=str(slurm_job_id),
                        action="identity_mismatch_blocked",
                        status=str(job.status),
                        durable_write_kind=None,
                        durable_write_count=0,
                        reconciliation_reason_class=identity_reason,
                    )
                )
                continue

        slurm_status = slurm_status or SlurmJobStatus.FAILED

        if slurm_status in TERMINAL_STATUSES:
            if file_cohort:
                projections, accounting_complete = _file_cohort_task_projections(job, record)
                error_code = map_slurm_error_code(record.raw_state) if slurm_status == SlurmJobStatus.FAILED else None
                write_result = store.project_forecast_cohort_tasks(
                    job.job_id,
                    master_slurm_job_id=str(record.slurm_job_id),
                    projections=projections,
                    complete=accounting_complete,
                    master_status=slurm_status.value,
                    master_error_code=error_code,
                    reconciliation_decision="matched_bound",
                )
                write_count = int(write_result.get("total") or 0)
                projected_status = slurm_status.value
                get_pipeline_job = getattr(store, "get_pipeline_job", None)
                if accounting_complete and callable(get_pipeline_job):
                    durable_master = get_pipeline_job(job.job_id)
                    # The operator receipt reports the terminal status READ BACK
                    # from the durable master after projection, because the
                    # projection may preserve an externally assigned terminal
                    # truth it cannot derive (#1629).  ``cancelled`` is one such
                    # sticky status: complete accounting projects it, the durable
                    # master stays ``cancelled``, and the receipt must agree with
                    # the durable/public master instead of reporting the
                    # projection-derived value.  The readback stays NARROW: only
                    # the three projection-derived statuses plus ``cancelled``;
                    # ``submission_failed`` / ``reservation_lost`` never reach
                    # this projection (their submit-outcome/inventory gates reject
                    # them first), so they must not be admitted here either.
                    if isinstance(durable_master, Mapping) and durable_master.get("status") in {
                        "succeeded",
                        "partially_failed",
                        "failed",
                        "cancelled",
                    }:
                        projected_status = str(durable_master["status"])
                outcomes.append(
                    ReconcileOutcome(
                        job_id=job.job_id,
                        slurm_job_id=str(slurm_job_id),
                        action="terminal" if accounting_complete else "task_accounting_incomplete",
                        status=projected_status if accounting_complete else RECONCILE_UNVERIFIED_STATUS,
                        durable_write_kind="forecast_cohort_projection",
                        durable_write_count=write_count,
                        pipeline_status_write_count=int(write_result.get("pipeline_status") or 0),
                        pipeline_event_write_count=int(write_result.get("pipeline_event") or 0),
                    )
                )
                continue
            error_code = map_slurm_error_code(record.raw_state) if slurm_status == SlurmJobStatus.FAILED else None
            store.update_job_status(
                job.job_id,
                slurm_status.value,
                error_code=error_code,
            )
            outcomes.append(
                ReconcileOutcome(
                    job_id=job.job_id,
                    slurm_job_id=str(slurm_job_id),
                    action="terminal",
                    status=slurm_status.value,
                    durable_write_kind="pipeline_job_status",
                    durable_write_count=1,
                )
            )
            continue

        # Still running/queued: keep current status, never resubmit.
        if file_cohort:
            write_count = _transition_file_runtime_status(store, job, slurm_status.value)
        else:
            store.update_job_status(job.job_id, slurm_status.value)
            write_count = 1
        outcomes.append(
            ReconcileOutcome(
                job_id=job.job_id,
                slurm_job_id=str(slurm_job_id),
                action="still_running",
                status=slurm_status.value,
                durable_write_kind="pipeline_job_status",
                durable_write_count=write_count,
            )
        )

    return outcomes


def _bound_forcing_submit_reconcile_job(job: Any) -> bool:
    if not _forcing_submit_reconcile_job(job):
        return False
    slurm_job_id = str(getattr(job, "slurm_job_id", None) or "")
    return bool(
        slurm_job_id.isdigit()
        and getattr(job, "submit_outcome", None) == "accepted"
        and getattr(job, "reconciliation_source", None)
        in FORCING_EXACT_COMMENT_RECONCILIATION_SOURCES
        and getattr(job, "reconciliation_decision", None) == "matched_bound"
        and getattr(job, "matched_slurm_job_id", None) == slurm_job_id
    )


def _terminal_file_cohort_identity_matches(store: Any, record: SacctRecord, job: Any) -> tuple[bool, str | None]:
    """One terminal file-cohort identity verdict plus its stable failure reason.

    The match verdict is False exactly when one clause failed; the reason token
    names the failed clause (#1795 D5). The folded cohort-valid/runtime
    predicate is split so ``cohort_identity_invalid`` and
    ``runtime_identity_mismatch`` are distinct.
    """

    identity = vars(job) if hasattr(job, "__dict__") else {}
    if not forecast_cohort_identity_is_valid(identity):
        return False, "cohort_identity_invalid"
    if not _file_cohort_runtime_identity_matches(store, identity):
        return False, "runtime_identity_mismatch"
    expected_master = str(getattr(job, "slurm_job_id", None) or "")
    if not expected_master or record.slurm_job_id != expected_master:
        return False, "master_id_mismatch"
    # Clusters without ``AccountingStoreFlags=job_comment`` never persist the
    # sbatch comment into accounting, so an empty record comment is "not
    # stored", not "different job". Identity then rests on the remaining
    # gates: exact master id (queried by our own durable binding), ownership
    # (user/account), stage-family job name, and the exact task-id bijection
    # against cohort_members. A present-but-different comment stays fatal.
    record_comment = str(record.comment or "")
    if record_comment and record_comment != str(getattr(job, "slurm_comment", None) or ""):
        return False, "comment_mismatch"
    if record.stage not in (None, "") and not is_forecast_cohort_stage_name(record.stage):
        return False, "stage_family_mismatch"
    expected_user = str(getattr(job, "expected_slurm_user", None) or "")
    expected_account = str(getattr(job, "expected_slurm_account", None) or "")
    if bool(getattr(job, "slurm_ownership_required", False)) and (
        not expected_user or not expected_account or not record.user or not record.account
    ):
        return False, "ownership_unproven"
    if expected_user and record.user != expected_user:
        return False, "ownership_user_mismatch"
    if expected_account and record.account != expected_account:
        return False, "ownership_account_mismatch"
    try:
        member_ids = {
            int(member["array_task_id"])
            for member in ordered_cohort_members(getattr(job, "cohort_members", ()))
        }
    except (AcceptedSubmitEvidenceError, KeyError, TypeError, ValueError):
        return False, "cohort_members_unparsable"
    observed_task_ids: set[int] = set()
    observed_slurm_ids: set[str] = set()
    for task in record.array_task_records:
        raw_array_task_id = task.array_task_id
        raw_task_id = task.task_id
        if raw_array_task_id not in (None, "") and raw_task_id not in (None, ""):
            try:
                if int(raw_array_task_id) != int(raw_task_id):
                    return False, "task_identity_values_mismatch"
            except (TypeError, ValueError):
                return False, "task_identity_values_unparsable"
        raw_id = raw_array_task_id if raw_array_task_id not in (None, "") else raw_task_id
        try:
            task_id = int(raw_id)
        except (TypeError, ValueError):
            return False, "task_id_unparsable"
        task_slurm_job_id = str(task.slurm_job_id or "")
        if (
            task_id not in member_ids
            or task_id in observed_task_ids
            or task_slurm_job_id in observed_slurm_ids
            or task_slurm_job_id != f"{expected_master}_{task_id}"
        ):
            return False, "task_mapping_mismatch"
        observed_task_ids.add(task_id)
        observed_slurm_ids.add(task_slurm_job_id)
        if task.job_name.strip() not in _GENERIC_ARRAY_JOB_NAMES and not is_forecast_cohort_stage_name(
            task.job_name.removeprefix("nhms_")
        ):
            return False, "task_job_name_mismatch"
        if task.comment not in (None, "", getattr(job, "slurm_comment", None)):
            return False, "task_comment_mismatch"
    return True, None


def _file_cohort_task_projections(job: Any, record: SacctRecord) -> tuple[list[dict[str, Any]], bool]:
    members = {
        int(member["array_task_id"]): dict(member)
        for member in ordered_cohort_members(getattr(job, "cohort_members", ()))
    }
    tasks: dict[int, list[SacctRecord]] = {}
    accounting_complete = True
    for task in record.array_task_records:
        raw_task_id = task.array_task_id if task.array_task_id is not None else task.task_id
        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError):
            accounting_complete = False
            continue
        if task_id not in members:
            accounting_complete = False
            continue
        tasks.setdefault(task_id, []).append(task)
    if set(tasks) != set(members) or len(record.array_task_records) != len(members):
        accounting_complete = False

    projections: list[dict[str, Any]] = []
    for task_id, member in sorted(members.items()):
        task_records = tasks.get(task_id, [])
        task = task_records[0] if len(task_records) == 1 else None
        outcome = "unverified"
        if len(task_records) > 1:
            accounting_complete = False
        elif task is not None:
            normalized = _normalize_slurm_state(task.raw_state)
            status = SLURM_STATE_MAP.get(normalized)
            if status == SlurmJobStatus.SUCCEEDED:
                outcome = "succeeded"
            elif status in TERMINAL_STATUSES:
                outcome = "failed"
            else:
                accounting_complete = False
        if outcome == "unverified":
            accounting_complete = False
        projections.append(
            {
                "candidate_id": member.get("candidate_id"),
                "run_id": member.get("run_id"),
                "model_id": member.get("model_id"),
                "array_task_id": task_id,
                "array_task_outcome": outcome,
                "task_slurm_job_id": task.slurm_job_id if task is not None else None,
                "error_code": (None if outcome != "failed" or task is None else map_slurm_error_code(task.raw_state)),
                "restart_stage": "state_save_qc" if outcome == "succeeded" else member.get("restart_stage"),
                "native_shud_resubmitted": False,
            }
        )
    return projections, accounting_complete


def _transition_file_runtime_status(store: Any, job: Any, status: str) -> int:
    transitioner = getattr(store, "transition_pipeline_job_runtime_status", None)
    if not callable(transitioner):
        return 0
    result = transitioner(
        job.job_id,
        status,
        expected_statuses=(str(job.status),),
    )
    return int(getattr(result, "wrote", False))
