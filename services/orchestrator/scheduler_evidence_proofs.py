from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from services.orchestrator import scheduler_evidence as _scheduler_evidence


def scheduler_pass_status_from_cancellation(cancellation_evidence: Sequence[Mapping[str, Any]]) -> str:
    if not cancellation_evidence:
        return "planned"
    statuses = {str(item.get("status") or "") for item in cancellation_evidence}
    if statuses == {"cancelled"}:
        return "slurm_cancelled"
    if "cancelled" in statuses or "partially_cancelled" in statuses:
        return "slurm_partially_cancelled"
    if statuses == {"preflight_blocked"}:
        return "preflight_blocked"
    return "slurm_cancellation_blocked"


def scheduler_execution_boundary_from_cancellation(cancellation_evidence: Sequence[Mapping[str, Any]]) -> str:
    if not cancellation_evidence:
        return "planning_only"
    if all(str(item.get("status") or "") == "preflight_blocked" for item in cancellation_evidence):
        return "evidence_preflight_blocked"
    return "slurm_cancellation"


def slurm_status_sync_proof(
    *,
    sync_required: bool = False,
    reservation: Mapping[str, Any] | None = None,
    blocked: bool = False,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "sync_required": sync_required,
        "sync_called": False,
        "mutation_occurred": False,
        "protected_by_pre_execution_evidence": False,
    }
    if blocked:
        proof["status"] = "preflight_blocked"
    elif sync_required:
        proof["status"] = "pending_reservation"
    else:
        proof["status"] = "not_required"
    if reservation is not None:
        proof["evidence_pre_execution_status"] = reservation.get("status")
        proof["protected_by_pre_execution_evidence"] = reservation.get("status") == "reserved"
        if reservation.get("status") == "blocked":
            proof["block_reason"] = reservation.get("reason")
    return proof


def slurm_status_sync_proof_from_candidates(
    slurm_status_sync_evidence: Sequence[Mapping[str, Any]],
    *,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    sync_payloads = list(slurm_status_sync_evidence)
    failed_payloads = [item for item in sync_payloads if str(item.get("status") or "") == "failed"]
    update_count = sum(len(item.get("updates") or []) for item in sync_payloads)
    terminal_update_count = sum(len(item.get("terminal_updates") or []) for item in sync_payloads)
    unknown_after_attempt = any(
        item.get("mutation_outcome") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT for item in failed_payloads
    )
    status = "failed" if failed_payloads else ("synced" if sync_payloads else "not_required")
    proof: dict[str, Any] = {
        "status": status,
        "sync_required": bool(sync_payloads),
        "sync_called": bool(sync_payloads),
        "mutation_occurred": update_count > 0,
        "protected_by_pre_execution_evidence": reservation.get("status") == "reserved",
        "evidence_pre_execution_status": reservation.get("status"),
        "synced_cycle_count": len({str(item.get("cycle_id") or "") for item in sync_payloads if item.get("cycle_id")}),
        "updated_job_count": update_count,
        "terminal_update_count": terminal_update_count,
    }
    if failed_payloads:
        proof.update(
            {
                "failed_sync_count": len(failed_payloads),
                "error_code": failed_payloads[0].get("error_code"),
                "error_message": failed_payloads[0].get("error_message"),
            }
        )
    if unknown_after_attempt:
        proof["mutation_outcome"] = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        proof["mutation_occurred"] = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        proof["pipeline_status_writes_proven_absent"] = False
        proof["pipeline_event_writes_proven_absent"] = False
    return proof


def execution_write_proof(
    *,
    reservation: Mapping[str, Any] | None = None,
    execution_required: bool = False,
    blocked: bool = False,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "execution_required": execution_required,
        "orchestration_called": False,
        "mutation_occurred": False,
        "protected_by_pre_execution_evidence": False,
    }
    if blocked:
        proof["status"] = "preflight_blocked"
    elif execution_required:
        proof["status"] = "pending_reservation"
    else:
        proof["status"] = "not_required"
    if reservation is not None:
        proof["evidence_pre_execution_status"] = reservation.get("status")
        proof["protected_by_pre_execution_evidence"] = reservation.get("status") == "reserved"
        if reservation.get("status") == "blocked":
            proof["block_reason"] = reservation.get("reason")
    return proof


def execution_write_proof_from_evidence(
    execution_evidence: Sequence[Mapping[str, Any]],
    *,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    execution_payloads = list(execution_evidence)
    orchestration_called = any(item.get("execution_attempted") is True for item in execution_payloads)
    submitted_count = sum(1 for item in execution_payloads if item.get("submitted") is True)
    slurm_submit_count = sum(1 for item in execution_payloads if item.get("slurm_submit_called") is True)
    unknown_slurm_submit_count = sum(
        1
        for item in execution_payloads
        if item.get("slurm_submit_called") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    )
    pipeline_status_write_count = sum(
        1 for item in execution_payloads if item.get("pipeline_status_write") is True
    )
    pipeline_event_write_count = sum(1 for item in execution_payloads if item.get("pipeline_event_write") is True)
    unknown_pipeline_status_write_count = sum(
        1
        for item in execution_payloads
        if item.get("pipeline_status_write") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    )
    unknown_pipeline_event_write_count = sum(
        1
        for item in execution_payloads
        if item.get("pipeline_event_write") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    )
    unknown_after_attempt_count = sum(
        1 for item in execution_payloads if item.get("mutation_outcome") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    )
    hydro_result_table_write_count = sum(
        1 for item in execution_payloads if item.get("hydro_result_table_write") is True
    )
    met_result_table_write_count = sum(
        1 for item in execution_payloads if item.get("met_result_table_write") is True
    )
    unknown_hydro_result_table_write_count = sum(
        1
        for item in execution_payloads
        if item.get("hydro_result_table_write") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    )
    unknown_met_result_table_write_count = sum(
        1
        for item in execution_payloads
        if item.get("met_result_table_write") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    )
    preflight_blocked = bool(execution_payloads) and all(
        str(item.get("status") or "") == "preflight_blocked" for item in execution_payloads
    )
    if unknown_after_attempt_count:
        status = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    elif submitted_count:
        status = "submitted"
    elif preflight_blocked:
        status = "preflight_blocked"
    elif execution_payloads:
        status = "completed_no_submit"
    else:
        status = "not_required"
    if unknown_slurm_submit_count:
        slurm_submit_value: bool | str = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    else:
        slurm_submit_value = slurm_submit_count > 0
    if unknown_hydro_result_table_write_count:
        hydro_result_table_write: bool | str = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    elif hydro_result_table_write_count:
        hydro_result_table_write = True
    else:
        hydro_result_table_write = slurm_submit_value
    if unknown_met_result_table_write_count:
        met_result_table_write: bool | str = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    elif met_result_table_write_count:
        met_result_table_write = True
    else:
        met_result_table_write = slurm_submit_value
    if unknown_pipeline_status_write_count:
        pipeline_status_write: bool | str = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    else:
        pipeline_status_write = pipeline_status_write_count > 0
    if unknown_pipeline_event_write_count:
        pipeline_event_write: bool | str = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    else:
        pipeline_event_write = pipeline_event_write_count > 0
    proof: dict[str, Any] = {
        "status": status,
        "execution_required": bool(execution_payloads),
        "orchestration_called": orchestration_called,
        "mutation_occurred": _scheduler_evidence.execution_mutation_value(
            slurm_submit_value,
            hydro_result_table_write,
            met_result_table_write,
            pipeline_status_write,
            pipeline_event_write,
        ),
        "protected_by_pre_execution_evidence": reservation.get("status") == "reserved",
        "evidence_pre_execution_status": reservation.get("status"),
        "submitted_count": submitted_count,
        "slurm_submit_called": slurm_submit_value,
        "slurm_submit_count": slurm_submit_count,
        "hydro_result_table_writes": hydro_result_table_write,
        "met_result_table_writes": met_result_table_write,
        "pipeline_status_writes": pipeline_status_write,
        "pipeline_event_writes": pipeline_event_write,
        "pipeline_status_write_count": pipeline_status_write_count,
        "pipeline_event_write_count": pipeline_event_write_count,
        "hydro_result_table_write_count": hydro_result_table_write_count,
        "met_result_table_write_count": met_result_table_write_count,
    }
    if unknown_slurm_submit_count:
        proof["slurm_submit_outcome"] = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        proof["unknown_slurm_submit_count"] = unknown_slurm_submit_count
        proof["slurm_submit_proven_absent"] = False
    else:
        proof["slurm_submit_proven_absent"] = slurm_submit_count == 0
    proof["hydro_result_table_writes_proven_absent"] = hydro_result_table_write is False
    proof["met_result_table_writes_proven_absent"] = met_result_table_write is False
    if unknown_hydro_result_table_write_count:
        proof["hydro_result_table_write_outcome"] = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        proof["unknown_hydro_result_table_write_count"] = unknown_hydro_result_table_write_count
        proof["hydro_result_table_writes_proven_absent"] = False
    if unknown_met_result_table_write_count:
        proof["met_result_table_write_outcome"] = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        proof["unknown_met_result_table_write_count"] = unknown_met_result_table_write_count
        proof["met_result_table_writes_proven_absent"] = False
    if unknown_pipeline_status_write_count:
        proof["pipeline_status_write_outcome"] = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        proof["unknown_pipeline_status_write_count"] = unknown_pipeline_status_write_count
        proof["pipeline_status_writes_proven_absent"] = False
    else:
        proof["pipeline_status_writes_proven_absent"] = pipeline_status_write_count == 0
    if unknown_pipeline_event_write_count:
        proof["pipeline_event_write_outcome"] = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        proof["unknown_pipeline_event_write_count"] = unknown_pipeline_event_write_count
        proof["pipeline_event_writes_proven_absent"] = False
    else:
        proof["pipeline_event_writes_proven_absent"] = pipeline_event_write_count == 0
    if unknown_after_attempt_count:
        proof["mutation_outcome"] = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        proof["mutation_occurred"] = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        proof["unknown_execution_count"] = unknown_after_attempt_count
        if hydro_result_table_write == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
            proof["hydro_result_table_writes_proven_absent"] = False
        if met_result_table_write == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
            proof["met_result_table_writes_proven_absent"] = False
        if unknown_pipeline_status_write_count or pipeline_status_write_count:
            proof["pipeline_status_writes_proven_absent"] = False
        if unknown_pipeline_event_write_count or pipeline_event_write_count:
            proof["pipeline_event_writes_proven_absent"] = False
    return proof


def slurm_cancellation_proof(
    *,
    cancellation_required: bool = False,
    reservation: Mapping[str, Any] | None = None,
    blocked: bool = False,
) -> dict[str, Any]:
    proof: dict[str, Any] = {
        "cancellation_required": cancellation_required,
        "cancel_called": False,
        "mutation_occurred": False,
        "protected_by_pre_execution_evidence": False,
    }
    if blocked:
        proof["status"] = "preflight_blocked"
    elif cancellation_required:
        proof["status"] = "pending_reservation"
    else:
        proof["status"] = "not_required"
    if reservation is not None:
        proof["evidence_pre_execution_status"] = reservation.get("status")
        proof["protected_by_pre_execution_evidence"] = reservation.get("status") == "reserved"
        if reservation.get("status") == "blocked":
            proof["block_reason"] = reservation.get("reason")
    return proof


def slurm_cancellation_proof_from_evidence(
    cancellation_evidence: Sequence[Mapping[str, Any]],
    *,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    cancel_called = any(item.get("cancel_attempted") is True for item in cancellation_evidence)
    cancelled_count = _scheduler_evidence.slurm_cancelled_count(cancellation_evidence)
    blocked_count = _scheduler_evidence.slurm_cancellation_blocked_count(cancellation_evidence)
    unknown_after_attempt_count = sum(
        1
        for item in cancellation_evidence
        if item.get("mutation_outcome") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    )
    pipeline_status_write_count = sum(1 for item in cancellation_evidence if item.get("pipeline_status_write") is True)
    pipeline_event_write_count = sum(1 for item in cancellation_evidence if item.get("pipeline_event_write") is True)
    proof: dict[str, Any] = {
        "status": _scheduler_evidence.scheduler_pass_status_from_cancellation(cancellation_evidence),
        "cancellation_required": bool(cancellation_evidence),
        "cancel_called": cancel_called,
        "mutation_occurred": cancelled_count > 0,
        "protected_by_pre_execution_evidence": reservation.get("status") == "reserved",
        "evidence_pre_execution_status": reservation.get("status"),
        "cancelled_job_count": cancelled_count,
        "blocked_cancellation_count": blocked_count,
        "pipeline_status_write_count": pipeline_status_write_count,
        "pipeline_event_write_count": pipeline_event_write_count,
    }
    if pipeline_status_write_count or pipeline_event_write_count:
        proof["mutation_occurred"] = True
    if unknown_after_attempt_count:
        proof["mutation_outcome"] = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        proof["mutation_occurred"] = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
        proof["unknown_cancellation_count"] = unknown_after_attempt_count
        proof["slurm_cancellation_proven_absent"] = False
        proof["pipeline_status_writes_proven_absent"] = False
        proof["pipeline_event_writes_proven_absent"] = False
    else:
        proof["pipeline_status_writes_proven_absent"] = pipeline_status_write_count == 0
        proof["pipeline_event_writes_proven_absent"] = pipeline_event_write_count == 0
    return proof


def slurm_status_sync_count(proof: Mapping[str, Any]) -> int:
    value = proof.get("updated_job_count")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def slurm_status_sync_unknown_count(proof: Mapping[str, Any]) -> int:
    value = proof.get("failed_sync_count")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def slurm_status_sync_mutated(proof: Mapping[str, Any]) -> bool:
    return proof.get("mutation_occurred") is True


def slurm_status_sync_failed(proof: Mapping[str, Any]) -> bool:
    return str(proof.get("status") or "") == "failed" and proof.get("sync_called") is True


def slurm_cancelled_count(cancellation_evidence: Sequence[Mapping[str, Any]]) -> int:
    total = 0
    for item in cancellation_evidence:
        for job in item.get("cancelled_jobs") or []:
            if isinstance(job, Mapping) and str(job.get("status") or "").lower() == "cancelled":
                total += 1
    return total


def slurm_cancellation_blocked_count(cancellation_evidence: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for item in cancellation_evidence if str(item.get("status") or "") != "cancelled")


def slurm_cancellation_unknown_count(proof: Mapping[str, Any]) -> int:
    value = proof.get("unknown_cancellation_count")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 1 if proof.get("mutation_outcome") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT else 0


def scheduler_mutation_proof(
    *,
    execution_write_proof: Mapping[str, Any],
    slurm_status_sync_proof: Mapping[str, Any],
    slurm_cancellation_proof: Mapping[str, Any],
    restart_reconcile_proof: Mapping[str, Any] | None = None,
) -> dict[str, bool | str]:
    restart_reconcile_proof = restart_reconcile_proof or {}
    execution_slurm_submit = _scheduler_evidence.slurm_submit_proof_value(execution_write_proof)
    hydro_result_table_write = _scheduler_evidence.named_proof_value(
        execution_write_proof,
        "hydro_result_table_writes",
        "hydro_result_table_writes_proven_absent",
    )
    met_result_table_write = _scheduler_evidence.named_proof_value(
        execution_write_proof,
        "met_result_table_writes",
        "met_result_table_writes_proven_absent",
    )
    sync_mutation = _scheduler_evidence.proof_mutation_value(slurm_status_sync_proof)
    cancellation_mutation = _scheduler_evidence.proof_mutation_value(slurm_cancellation_proof)
    restart_reconcile_mutation = _scheduler_evidence.proof_mutation_value(restart_reconcile_proof)
    pipeline_status_write = _scheduler_evidence.merge_proof_values(
        _scheduler_evidence.pipeline_status_write_proof_value(execution_write_proof),
        sync_mutation,
        _scheduler_evidence.pipeline_status_write_proof_value(slurm_cancellation_proof),
        _scheduler_evidence.pipeline_status_write_proof_value(restart_reconcile_proof),
    )
    pipeline_event_write = _scheduler_evidence.merge_proof_values(
        _scheduler_evidence.pipeline_event_write_proof_value(execution_write_proof),
        sync_mutation,
        _scheduler_evidence.pipeline_event_write_proof_value(slurm_cancellation_proof),
        _scheduler_evidence.pipeline_event_write_proof_value(restart_reconcile_proof),
    )
    return {
        "slurm_submit_called": execution_slurm_submit,
        "hydro_result_table_writes": hydro_result_table_write,
        "met_result_table_writes": met_result_table_write,
        "pipeline_status_writes": pipeline_status_write,
        "pipeline_event_writes": pipeline_event_write,
        "slurm_status_sync_writes": sync_mutation,
        "slurm_cancellation_writes": cancellation_mutation,
        "restart_reconcile_writes": restart_reconcile_mutation,
    }


def restart_reconcile_proof(restart_reconcile_evidence: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(restart_reconcile_evidence, Mapping):
        return {
            "status": "not_required",
            "mutation_occurred": False,
            "pipeline_status_writes": False,
            "pipeline_event_writes": False,
            "pipeline_status_writes_proven_absent": True,
            "pipeline_event_writes_proven_absent": True,
        }
    reserved_unbound = restart_reconcile_evidence.get("reserved_unbound")
    inflight = restart_reconcile_evidence.get("inflight")
    reserved_outcomes = (
        list(reserved_unbound.get("outcomes") or []) if isinstance(reserved_unbound, Mapping) else []
    )
    inflight_outcomes = list(inflight.get("outcomes") or []) if isinstance(inflight, Mapping) else []
    bind_count = sum(
        1
        for outcome in reserved_outcomes
        if isinstance(outcome, Mapping) and str(outcome.get("action") or "") == "bound"
    )
    reserved_status_update_count = sum(
        1
        for outcome in reserved_outcomes
        if isinstance(outcome, Mapping) and str(outcome.get("action") or "") == "reservation_lost"
    )
    inflight_status_update_count = sum(
        1
        for outcome in inflight_outcomes
        if isinstance(outcome, Mapping)
        and str(outcome.get("action") or "") in {"terminal", "still_running", "unverified"}
    )
    pipeline_status_write_count = bind_count + reserved_status_update_count + inflight_status_update_count
    mutation_occurred = pipeline_status_write_count > 0
    return {
        "status": "mutated" if mutation_occurred else str(restart_reconcile_evidence.get("status") or "completed"),
        "mutation_occurred": mutation_occurred,
        "bind_reservation_count": bind_count,
        "update_job_status_count": reserved_status_update_count + inflight_status_update_count,
        "reserved_unbound_mutation_count": bind_count + reserved_status_update_count,
        "inflight_mutation_count": inflight_status_update_count,
        "pipeline_status_writes": mutation_occurred,
        "pipeline_event_writes": False,
        "pipeline_status_write_count": pipeline_status_write_count,
        "pipeline_event_write_count": 0,
        "pipeline_status_writes_proven_absent": not mutation_occurred,
        "pipeline_event_writes_proven_absent": True,
        "protected_by_pre_execution_evidence": False,
    }


def proof_mutation_value(proof: Mapping[str, Any]) -> bool | str:
    if proof.get("mutation_outcome") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    if proof.get("mutation_occurred") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    return proof.get("mutation_occurred") is True


def named_proof_value(proof: Mapping[str, Any], write_field: str, absent_field: str) -> bool | str:
    value = proof.get(write_field)
    if value == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    if value is True:
        return True
    if value is False:
        return False
    if proof.get(absent_field) is True:
        return False
    if proof.get(absent_field) is False and proof.get("mutation_outcome") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    return _scheduler_evidence.proof_mutation_value(proof)


def slurm_submit_proof_value(proof: Mapping[str, Any]) -> bool | str:
    value = proof.get("slurm_submit_called")
    if value == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    if value is True:
        return True
    if value is False:
        return False
    if proof.get("slurm_submit_outcome") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    if _scheduler_evidence.positive_count(proof.get("slurm_submit_count")):
        return True
    if proof.get("slurm_submit_proven_absent") is True:
        return False
    if proof.get("mutation_outcome") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    if proof.get("mutation_occurred") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    return proof.get("mutation_occurred") is True


def pipeline_status_write_proof_value(proof: Mapping[str, Any]) -> bool | str:
    value = proof.get("pipeline_status_writes")
    if value == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    if value is True:
        return True
    if value is False:
        return False
    if proof.get("pipeline_status_write_outcome") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    if proof.get("mutation_outcome") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    if "pipeline_status_write_count" in proof:
        return _scheduler_evidence.positive_count(proof.get("pipeline_status_write_count"))
    if proof.get("pipeline_status_writes_proven_absent") is True:
        return False
    if proof.get("mutation_occurred") is True:
        return True
    return False


def pipeline_event_write_proof_value(proof: Mapping[str, Any]) -> bool | str:
    value = proof.get("pipeline_event_writes")
    if value == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    if value is True:
        return True
    if value is False:
        return False
    if proof.get("pipeline_event_write_outcome") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    if proof.get("mutation_outcome") == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT:
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    if "pipeline_event_write_count" in proof:
        return _scheduler_evidence.positive_count(proof.get("pipeline_event_write_count"))
    if proof.get("pipeline_event_writes_proven_absent") is True:
        return False
    if proof.get("mutation_occurred") is True:
        return True
    return False


def merge_proof_values(*values: bool | str) -> bool | str:
    if any(value == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT for value in values):
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    return any(value is True for value in values)


def positive_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def execution_mutation_value(*values: bool | str | None) -> bool | str:
    if any(value == _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT for value in values):
        return _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
    return any(value is True for value in values)


def empty_counts() -> dict[str, int]:
    return {
        "candidate_count": 0,
        "blocked_candidate_count": 0,
        "skipped_candidate_count": 0,
        "selected_model_count": 0,
        "source_cycle_count": 0,
        "submitted_count": 0,
        "failed_count": 0,
        "partial_count": 0,
        "slurm_status_sync_count": 0,
        "slurm_status_sync_unknown_count": 0,
        "slurm_cancelled_count": 0,
        "slurm_cancellation_blocked_count": 0,
        "slurm_cancellation_unknown_count": 0,
    }


def no_mutation_proof() -> dict[str, bool]:
    return {
        "adapter_download_called": False,
        "slurm_submit_called": False,
        "slurm_status_sync_called": False,
        "slurm_cancellation_called": False,
        "shud_runtime_called": False,
        "hydro_result_table_writes": False,
        "met_result_table_writes": False,
        "pipeline_status_writes": False,
        "pipeline_event_writes": False,
    }


__all__ = [
    "empty_counts",
    "execution_mutation_value",
    "execution_write_proof",
    "execution_write_proof_from_evidence",
    "merge_proof_values",
    "named_proof_value",
    "no_mutation_proof",
    "pipeline_event_write_proof_value",
    "pipeline_status_write_proof_value",
    "positive_count",
    "proof_mutation_value",
    "scheduler_execution_boundary_from_cancellation",
    "restart_reconcile_proof",
    "scheduler_mutation_proof",
    "scheduler_pass_status_from_cancellation",
    "slurm_cancellation_blocked_count",
    "slurm_cancellation_proof",
    "slurm_cancellation_proof_from_evidence",
    "slurm_cancellation_unknown_count",
    "slurm_cancelled_count",
    "slurm_status_sync_count",
    "slurm_status_sync_failed",
    "slurm_status_sync_mutated",
    "slurm_status_sync_proof",
    "slurm_status_sync_proof_from_candidates",
    "slurm_status_sync_unknown_count",
    "slurm_submit_proof_value",
]
