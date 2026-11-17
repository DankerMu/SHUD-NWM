"""Accounting-record identity matching for restart reconcile."""

from __future__ import annotations

from typing import Any, Mapping

from services.orchestrator.accepted_submit_identity import (
    FORECAST_COHORT_STAGE_ALIASES,
    accepted_submit_contract_is_current,
)
from services.orchestrator.forcing_submit_identity import forcing_submit_identity_is_complete
from services.orchestrator.reconcile_records import SacctRecord
from services.slurm_gateway.real_backend import SLURM_JOB_ID_RE


def _expected_job_name_token(stage: str | None, job_type: str | None) -> str | None:
    """The ``nhms_<stage>`` token sbatch templates set as ``--job-name``."""

    token = (stage or job_type or "").strip()
    if not token:
        return None
    return f"nhms_{token}"


_GENERIC_ARRAY_JOB_NAMES = frozenset({"nhms_forecast", "nhms_forcing"})
_FORECAST_STAGE_ALIASES = frozenset({"forecast", "run_shud_forecast_array", "run_shud_forecast"})
_FORCING_STAGE_ALIASES = frozenset({"forcing", "produce_forcing", "forcing_package"})


def _identity_matches(
    record: SacctRecord,
    expected_token: str | None,
    job: Any | None = None,
    *,
    require_durable_identity: bool = True,
) -> bool:
    """Confirm the accounting row belongs to the recorded candidate.

    When the durable row gives a stage/job_type, the sacct ``JobName`` must carry
    the matching ``nhms_<stage>`` token.
    """

    # The durable 1:1 slurm_job_id↔candidate binding is the primary identity; the
    # JobName stage token is a secondary anti-reuse guard against a recycled
    # slurm_job_id. With no token available we cannot run that guard, so be
    # conservative and judge unverified (do not accept, do not resubmit).
    if expected_token is None:
        return False
    job_name = record.job_name.strip()
    generic_job_name = job_name in _GENERIC_ARRAY_JOB_NAMES
    expected_stage_token = expected_token.removeprefix("nhms_")
    job_stage_token = job_name.removeprefix("nhms_")
    if job_name != expected_token and not (generic_job_name and _stages_match(job_stage_token, expected_stage_token)):
        return False
    if generic_job_name and require_durable_identity:
        return job is not None and _record_has_durable_identity_proof(record, job)
    return True


def _record_has_durable_identity_proof(record: SacctRecord, job: Any) -> bool:
    if _array_master_record_matches_job(record, job):
        return True
    for identity in _record_identity_mappings(record):
        if _identity_mapping_matches_job(identity, job):
            return True
    return False


def _array_master_record_matches_job(record: SacctRecord, job: Any) -> bool:
    """Prove a synthesized array master against its durable master binding."""

    if not record.array_member_job_ids:
        return False
    expected_master = str(getattr(job, "slurm_job_id", "") or "")
    if (
        not expected_master
        or "_" in expected_master
        or record.slurm_job_id != expected_master
        or getattr(job, "array_task_id", None) not in (None, "")
    ):
        return False
    member_prefix = f"{expected_master}_"
    return all(
        member_id.startswith(member_prefix) and SLURM_JOB_ID_RE.fullmatch(member_id)
        for member_id in record.array_member_job_ids
    )


def _record_identity_mappings(record: SacctRecord) -> list[Mapping[str, Any]]:
    mappings: list[Mapping[str, Any]] = []
    direct = {
        "run_id": record.run_id,
        "model_id": record.model_id,
        "stage": record.stage,
        "pipeline_job_id": record.pipeline_job_id,
        "array_task_id": record.array_task_id,
        "task_id": record.task_id,
    }
    if any(value not in (None, "") for value in direct.values()):
        mappings.append(direct)
    for value in (record.submitted_manifest, record.stdout_identity, record.file_journal_identity):
        if isinstance(value, Mapping):
            mappings.append(value)
    return mappings


def _identity_mapping_matches_job(identity: Mapping[str, Any], job: Any) -> bool:
    proof = False
    expected_job_id = str(getattr(job, "job_id", "") or "")
    expected_run_id = str(getattr(job, "run_id", "") or "")
    expected_model_id = str(getattr(job, "model_id", "") or "")
    expected_stage = str(getattr(job, "stage", None) or getattr(job, "job_type", "") or "")
    expected_task_id = getattr(job, "array_task_id", None)

    actual_job_id = _identity_text(identity, "pipeline_job_id", "job_id")
    if actual_job_id:
        if actual_job_id != expected_job_id:
            return False
        proof = True

    actual_run_id = _identity_text(identity, "run_id")
    if actual_run_id:
        if expected_run_id and actual_run_id != expected_run_id:
            return False
        proof = True

    actual_model_id = _identity_text(identity, "model_id")
    if actual_model_id and expected_model_id and actual_model_id != expected_model_id:
        return False

    actual_stage = _identity_text(identity, "stage", "job_type", "stage_name")
    if actual_stage and not _stages_match(actual_stage, expected_stage):
        return False

    if expected_task_id not in (None, ""):
        actual_task_id = _identity_text(identity, "array_task_id", "task_id", "original_task_id")
        if actual_task_id != str(expected_task_id):
            return False
        proof = True

    return proof


def _identity_text(identity: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = identity.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _stages_match(actual: str | None, expected: str | None) -> bool:
    if not actual or not expected:
        return False
    actual_value = actual.strip()
    expected_value = expected.strip()
    if actual_value == expected_value:
        return True
    return _stage_family(actual_value) is not None and _stage_family(actual_value) == _stage_family(expected_value)


def _stage_family(value: str) -> str | None:
    if value in _FORECAST_STAGE_ALIASES:
        return "forecast"
    if value in _FORCING_STAGE_ALIASES:
        return "forcing"
    return None


def _is_forecast_cohort_job(job: Any) -> bool:
    stage = str(getattr(job, "stage", None) or "")
    if stage:
        return stage in FORECAST_COHORT_STAGE_ALIASES
    return str(getattr(job, "job_type", None) or "") in FORECAST_COHORT_STAGE_ALIASES


def _forcing_submit_reconcile_job(job: Any) -> bool:
    identity = vars(job) if hasattr(job, "__dict__") else {}
    return forcing_submit_identity_is_complete(identity)


def _forcing_accounting_identity_matches(
    record: SacctRecord,
    job: Any,
    *,
    require_exact_comment: bool,
    require_bound_master: bool,
) -> bool:
    if not _forcing_submit_reconcile_job(job):
        return False
    if not SLURM_JOB_ID_RE.fullmatch(str(record.slurm_job_id)):
        return False
    expected_comment = str(getattr(job, "slurm_comment", None) or "")
    if not expected_comment:
        return False
    if require_exact_comment and str(record.comment or "") != expected_comment:
        return False
    if not require_exact_comment and record.comment not in (None, ""):
        if str(record.comment) != expected_comment:
            return False
    expected_master = str(getattr(job, "slurm_job_id", None) or "")
    if require_bound_master and (not expected_master or record.slurm_job_id != expected_master):
        return False
    expected_token = _expected_job_name_token(
        getattr(job, "stage", None), getattr(job, "job_type", None)
    )
    if not _identity_matches(record, expected_token, job, require_durable_identity=False):
        return False
    for record_value, job_field in (
        (record.run_id, "run_id"),
        (record.stage, "stage"),
        (record.pipeline_job_id, "job_id"),
    ):
        expected = getattr(job, job_field, None)
        if record_value not in (None, "") and expected not in (None, ""):
            if str(record_value) != str(expected):
                return False
    expected_user = str(getattr(job, "expected_slurm_user", None) or "")
    expected_account = str(getattr(job, "expected_slurm_account", None) or "")
    if bool(getattr(job, "slurm_ownership_required", False)) and (
        not expected_user or not expected_account or not record.user or not record.account
    ):
        return False
    if expected_user and record.user != expected_user:
        return False
    if expected_account and record.account != expected_account:
        return False
    return True


def _file_cohort_runtime_identity_matches(store: Any, identity: Mapping[str, Any]) -> bool:
    validator = getattr(store, "forecast_cohort_runtime_identity_matches", None)
    return callable(validator) and bool(validator(identity))


def _fallback_comment_allowed(record_comment: str | None, reservation_comment: str | None) -> bool:
    """One named final-gate predicate for the name-window fallback comment.

    On the comment-less fallback an empty (not stored) comment is allowed and a
    present comment equal to the reservation's idempotency comment is allowed;
    any other present value is fatal. Shared by the reserved identity gate and
    the final bind guard so the two gates cannot drift apart (#1565 Fix 3).
    """

    comment = str(record_comment or "")
    if not comment:
        return True
    return comment == str(reservation_comment or "")


def _accepted_submit_versioned_job(job: Any) -> bool:
    identity = vars(job) if hasattr(job, "__dict__") else job
    return isinstance(identity, Mapping) and accepted_submit_contract_is_current(identity)
