from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC
from typing import Any

from packages.common.source_identity import normalize_source_id
from services.orchestrator.chain_config import scenario_for_source
from services.orchestrator.reservation import slurm_comment_for
from workers.data_adapters.base import cycle_id_for, format_cycle_time, parse_cycle_time

FORECAST_COHORT_STAGE_ALIASES = frozenset({"forecast", "run_shud_forecast", "run_shud_forecast_array"})
MAX_FORECAST_COHORT_MEMBERS = 256

_MEMBER_FIELDS = (
    "array_task_id",
    "candidate_id",
    "run_id",
    "model_id",
    "basin_id",
    "scenario_id",
    "restart_stage",
)


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
    return tuple(
        {key: item.get(key) for key in _MEMBER_FIELDS}
        for item in value[:MAX_FORECAST_COHORT_MEMBERS]
        if isinstance(item, Mapping)
    )


def canonical_forecast_cohort_members(
    *, source_id: str, cycle_time: Any, basins: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """Build the one canonical accepted-submit member identity projection."""
    source = normalize_source_id(source_id)
    parsed_cycle = parse_cycle_time(cycle_time)
    compact_cycle = format_cycle_time(parsed_cycle)
    cycle_iso = parsed_cycle.astimezone(UTC).isoformat().replace("+00:00", "Z")
    scenario_id = scenario_for_source(source)
    members: list[dict[str, Any]] = []
    for index, basin in enumerate(basins):
        model_id = str(basin.get("model_id") or "")
        members.append(
            {
                "array_task_id": int(basin.get("task_id", index)),
                "candidate_id": f"{source}:{cycle_iso}:{model_id}:{scenario_id}",
                "run_id": f"fcst_{source.lower()}_{compact_cycle}_{model_id}",
                "model_id": model_id,
                "basin_id": str(basin.get("basin_id") or ""),
                "scenario_id": scenario_id,
                "restart_stage": "forecast",
            }
        )
    return tuple(members)


def forecast_cohort_digest(identity: Mapping[str, Any]) -> str:
    payload = {
        "job_id": str(identity.get("job_id") or ""),
        "run_id": str(identity.get("run_id") or ""),
        "source_id": str(identity.get("source_id") or ""),
        "cycle_id": str(identity.get("cycle_id") or ""),
        "stage": canonical_forecast_stage(identity.get("stage") or identity.get("job_type")),
        "idempotency_key": str(identity.get("idempotency_key") or ""),
        "slurm_comment": str(identity.get("slurm_comment") or ""),
        "cohort_members": ordered_cohort_members(identity.get("cohort_members")),
        "slurm_ownership_required": bool(identity.get("slurm_ownership_required", False)),
        "expected_slurm_user": str(identity.get("expected_slurm_user") or ""),
        "expected_slurm_account": str(identity.get("expected_slurm_account") or ""),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def forecast_cohort_identity_is_valid(identity: Mapping[str, Any]) -> bool:
    if not is_forecast_cohort_stage_name(identity.get("stage"), identity.get("job_type")):
        return False
    try:
        source_id = normalize_source_id(str(identity.get("source_id") or ""))
        cycle_id = str(identity.get("cycle_id") or "")
        cycle_time = parse_cycle_time(cycle_id.split("_", maxsplit=1)[1])
    except (IndexError, TypeError, ValueError):
        return False
    if cycle_id != cycle_id_for(source_id, cycle_time):
        return False

    run_id = str(identity.get("run_id") or "")
    expected_run_prefix = f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}"
    if run_id != expected_run_prefix and not run_id.startswith(f"{expected_run_prefix}_"):
        return False
    job_id = str(identity.get("job_id") or "")
    expected_job_id = f"job_{run_id}_forecast"
    if job_id == expected_job_id:
        expected_key = f"{run_id}:forecast"
    elif job_id.startswith(f"{expected_job_id}_"):
        retry_suffix = job_id.removeprefix(f"{expected_job_id}_")
        if not retry_suffix:
            return False
        expected_key = f"{run_id}:forecast:{retry_suffix}"
    else:
        return False
    key = str(identity.get("idempotency_key") or "")
    if key != expected_key or str(identity.get("slurm_comment") or "") != slurm_comment_for(key):
        return False

    raw_members = identity.get("cohort_members")
    members = ordered_cohort_members(raw_members)
    if (
        not isinstance(raw_members, Sequence)
        or isinstance(raw_members, str | bytes | bytearray)
        or not members
        or len(members) != len(raw_members)
    ):
        return False

    scenario_id = scenario_for_source(source_id)
    cycle_iso = cycle_time.astimezone(UTC).isoformat().replace("+00:00", "Z")
    compact_cycle = format_cycle_time(cycle_time)
    unique_fields = {field: set() for field in ("candidate_id", "run_id", "model_id", "basin_id")}
    for index, member in enumerate(members):
        model_id = str(member.get("model_id") or "")
        basin_id = str(member.get("basin_id") or "")
        expected_member = {
            "array_task_id": index,
            "candidate_id": f"{source_id}:{cycle_iso}:{model_id}:{scenario_id}",
            "run_id": f"fcst_{source_id.lower()}_{compact_cycle}_{model_id}",
            "model_id": model_id,
            "basin_id": basin_id,
            "scenario_id": scenario_id,
            "restart_stage": "forecast",
        }
        if not model_id or not basin_id or member != expected_member:
            return False
        for field, seen in unique_fields.items():
            value = str(member.get(field) or "")
            if value in seen:
                return False
            seen.add(value)

    if bool(identity.get("slurm_ownership_required", False)) and (
        not str(identity.get("expected_slurm_user") or "").strip()
        or not str(identity.get("expected_slurm_account") or "").strip()
    ):
        return False
    digest = str(identity.get("cohort_digest") or "")
    return bool(digest and digest == forecast_cohort_digest(identity))


__all__ = (
    "FORECAST_COHORT_STAGE_ALIASES",
    "MAX_FORECAST_COHORT_MEMBERS",
    "canonical_forecast_cohort_members",
    "canonical_forecast_stage",
    "forecast_cohort_digest",
    "forecast_cohort_identity_is_valid",
    "is_forecast_cohort_stage_name",
    "ordered_cohort_members",
)
