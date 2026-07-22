from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from services.orchestrator.reservation import slurm_comment_for
from workers.data_adapters.base import format_cycle_time, parse_cycle_time

FORECAST_COHORT_STAGE_ALIASES = frozenset({"forecast", "run_shud_forecast", "run_shud_forecast_array"})
MAX_FORECAST_COHORT_MEMBERS = 256


def canonical_forecast_stage(value: Any) -> str | None:
    return "forecast" if str(value or "").strip() in FORECAST_COHORT_STAGE_ALIASES else None


def is_forecast_cohort_stage_name(stage: Any, job_type: Any = None) -> bool:
    stage_text = str(stage or "").strip()
    if stage_text:
        return canonical_forecast_stage(stage_text) is not None
    return canonical_forecast_stage(job_type) is not None


def ordered_cohort_members(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return ()
    allowed = (
        "array_task_id",
        "candidate_id",
        "run_id",
        "model_id",
        "basin_id",
        "restart_stage",
    )
    return tuple(
        {key: item.get(key) for key in allowed}
        for item in value[:MAX_FORECAST_COHORT_MEMBERS]
        if isinstance(item, Mapping)
    )


def forecast_cohort_digest(identity: Mapping[str, Any]) -> str:
    payload = {
        "job_id": str(identity.get("job_id") or ""),
        "run_id": str(identity.get("run_id") or ""),
        "source_id": str(identity.get("source_id") or "").lower(),
        "cycle_id": str(identity.get("cycle_id") or ""),
        "stage": canonical_forecast_stage(identity.get("stage") or identity.get("job_type")),
        "idempotency_key": str(identity.get("idempotency_key") or ""),
        "slurm_comment": str(identity.get("slurm_comment") or ""),
        "cohort_members": ordered_cohort_members(identity.get("cohort_members")),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def forecast_cohort_identity_is_valid(identity: Mapping[str, Any]) -> bool:
    if not is_forecast_cohort_stage_name(identity.get("stage"), identity.get("job_type")):
        return False
    key = str(identity.get("idempotency_key") or "")
    run_id = str(identity.get("run_id") or "")
    job_id = str(identity.get("job_id") or "")
    source_id = str(identity.get("source_id") or "").lower()
    cycle_id = str(identity.get("cycle_id") or "")
    if not key or not run_id or not job_id or not source_id or not cycle_id:
        return False
    if str(identity.get("slurm_comment") or "") != slurm_comment_for(key):
        return False
    key_parts = key.split(":")
    if len(key_parts) not in {2, 3} or key_parts[:2] != [run_id, "forecast"]:
        return False
    expected_job_id = f"job_{run_id}_forecast"
    if job_id != expected_job_id and not job_id.startswith(f"{expected_job_id}_"):
        return False
    try:
        cycle_time = parse_cycle_time(cycle_id.removeprefix(f"{source_id}_"))
    except (TypeError, ValueError):
        return False
    if cycle_id != f"{source_id}_{format_cycle_time(cycle_time)}":
        return False
    if not run_id.startswith(f"cycle_{source_id}_{format_cycle_time(cycle_time)}"):
        return False
    members = ordered_cohort_members(identity.get("cohort_members"))
    if not members or len(members) > MAX_FORECAST_COHORT_MEMBERS:
        return False
    for index, member in enumerate(members):
        if member.get("array_task_id") != index:
            return False
        model_id = str(member.get("model_id") or "")
        member_run_id = str(member.get("run_id") or "")
        candidate_id = str(member.get("candidate_id") or "")
        if not model_id or member_run_id != f"fcst_{source_id}_{format_cycle_time(cycle_time)}_{model_id}":
            return False
        if not candidate_id.lower().startswith(f"{source_id}:") or f":{model_id}:" not in candidate_id:
            return False
        if canonical_forecast_stage(member.get("restart_stage")) is None:
            return False
    digest = str(identity.get("cohort_digest") or "")
    return bool(digest and digest == forecast_cohort_digest(identity))


__all__ = (
    "FORECAST_COHORT_STAGE_ALIASES",
    "MAX_FORECAST_COHORT_MEMBERS",
    "canonical_forecast_stage",
    "forecast_cohort_digest",
    "forecast_cohort_identity_is_valid",
    "is_forecast_cohort_stage_name",
    "ordered_cohort_members",
)
