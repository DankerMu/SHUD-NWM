"""Bounded forcing-submit identity, overlap, and resume helpers (#2447).

Forcing keeps its own member identity and must not enter the forecast
accepted-submit contract, digest, or stage aliases. New attempts persist the
actual reindexed task mapping before Gateway. Sparse historical rows without
that mapping are never treated as authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from services.orchestrator.accepted_submit_identity import ordered_cohort_members
from services.orchestrator.chain_types import TERMINAL_JOB_STATUSES, StageDefinition
from services.orchestrator.reservation import slurm_comment_for, validate_idempotency_key

FORCING_STAGE_ALIASES = frozenset(
    {"forcing", "produce_forcing", "produce_forcing_array", "forcing_package"}
)

FORCING_EXACT_COMMENT_RECONCILIATION_SOURCES = frozenset(
    {"slurm_exact_comment", "slurm_controller_exact_comment"}
)

FORCING_ATTEMPT_COMMENT_PREFIX = "nhms_forcing_attempt:"


def forcing_attempt_comment_for(idempotency_key: str, submission_attempt: int) -> str:
    """Return the forcing-only Slurm comment for one durable attempt."""

    if type(submission_attempt) is not int or submission_attempt < 1:
        raise ValueError("submission_attempt must be a positive integer")
    return (
        f"{FORCING_ATTEMPT_COMMENT_PREFIX}{validate_idempotency_key(idempotency_key)}"
        f":a{submission_attempt}"
    )


def is_current_forcing_attempt_comment(row: Mapping[str, Any] | None) -> bool:
    """Whether ``row`` carries its current forcing attempt's exact comment."""

    if not isinstance(row, Mapping):
        return False
    try:
        expected_comment = forcing_attempt_comment_for(
            str(row.get("idempotency_key") or ""),
            row.get("submission_attempt"),
        )
    except ValueError:
        return False
    return str(row.get("slurm_comment") or "") == expected_comment


def is_forcing_stage_name(stage: Any, job_type: Any = None) -> bool:
    stage_text = str(stage or "").strip()
    if stage_text:
        return stage_text in FORCING_STAGE_ALIASES
    return str(job_type or "").strip() in FORCING_STAGE_ALIASES


def is_forcing_array_stage(stage: StageDefinition) -> bool:
    return bool(stage.is_array) and is_forcing_stage_name(stage.stage, stage.job_type)


def canonical_forcing_cohort_members(
    *, tasks: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """Capture the exact task→model map handed to the Gateway."""

    members: list[dict[str, Any]] = []
    for index, task in enumerate(tasks):
        members.append(
            {
                "array_task_id": int(task.get("task_id", index)),
                "candidate_id": str(task.get("candidate_id") or ""),
                "run_id": str(task.get("run_id") or ""),
                "model_id": str(task.get("model_id") or ""),
                "basin_id": str(task.get("basin_id") or ""),
                "scenario_id": str(task.get("scenario_id") or ""),
                "restart_stage": "forcing",
            }
        )
    return tuple(members)


def forcing_member_identity_is_complete(row: Mapping[str, Any] | None) -> bool:
    """Return whether a new forcing row has a complete member-fence identity."""

    if not isinstance(row, Mapping) or not is_forcing_stage_name(row.get("stage"), row.get("job_type")):
        return False
    if any(not str(row.get(field) or "") for field in ("job_id", "run_id", "source_id", "cycle_id")):
        return False
    attempt = row.get("submission_attempt")
    if type(attempt) is not int or attempt < 1:
        return False
    # Keep pre-token rows readable without synthesizing a token for them. New
    # forcing reservations always write ``forcing_attempt_comment_for``.
    key = str(row.get("idempotency_key") or "")
    try:
        legacy_comment = slurm_comment_for(key)
    except ValueError:
        return False
    if not key or not (
        is_current_forcing_attempt_comment(row)
        or str(row.get("slurm_comment") or "") == legacy_comment
    ):
        return False
    if row.get("submission_attempt_started_at") in (None, ""):
        return False
    raw_members = row.get("cohort_members")
    members = ordered_cohort_members(raw_members)
    if not isinstance(raw_members, Sequence) or isinstance(raw_members, str | bytes | bytearray):
        return False
    if not members or len(members) != len(raw_members):
        return False
    models: set[str] = set()
    task_ids: set[int] = set()
    for member in members:
        model_id = str(member.get("model_id") or "")
        basin_id = str(member.get("basin_id") or "")
        candidate_id = str(member.get("candidate_id") or "")
        run_id = str(member.get("run_id") or "")
        task_id = member.get("array_task_id")
        if (
            not model_id
            or not basin_id
            or not candidate_id
            or not run_id
            or type(task_id) is not int
            or member.get("restart_stage") != "forcing"
            or model_id in models
            or task_id in task_ids
        ):
            return False
        models.add(model_id)
        task_ids.add(task_id)
    return task_ids == set(range(len(members)))


def forcing_submit_identity_is_complete(row: Mapping[str, Any] | None) -> bool:
    """Return whether a forcing row also has enough identity for a trusted bind."""

    if not forcing_member_identity_is_complete(row):
        return False
    assert row is not None
    if bool(row.get("slurm_ownership_required", False)):
        return bool(
            str(row.get("expected_slurm_user") or "")
            and str(row.get("expected_slurm_account") or "")
        )
    return True


def forcing_member_model_ids(row: Mapping[str, Any] | None) -> frozenset[str]:
    if not forcing_member_identity_is_complete(row):
        return frozenset()
    assert row is not None
    return frozenset(
        str(member.get("model_id") or "")
        for member in ordered_cohort_members(row.get("cohort_members"))
    )


def basin_model_ids(basins: Sequence[Mapping[str, Any]] | None) -> frozenset[str]:
    if not basins:
        return frozenset()
    return frozenset(
        str(basin.get("model_id") or "")
        for basin in basins
        if str(basin.get("model_id") or "")
    )


def forcing_members_overlap(left: Mapping[str, Any] | None, right_models: frozenset[str]) -> bool:
    return bool(forcing_member_model_ids(left) & right_models)


def is_unresolved_forcing_attempt(job: Mapping[str, Any] | None) -> bool:
    """Return whether a self-sufficient forcing attempt still owns its members."""

    if not forcing_member_identity_is_complete(job):
        return False
    assert job is not None
    status = str(job.get("status") or "")
    if status in TERMINAL_JOB_STATUSES or status in {"complete", "published"}:
        return False
    return bool(status)


def overlapping_unresolved_forcing_job(
    jobs: Sequence[Mapping[str, Any]] | None,
    *,
    member_models: frozenset[str],
    exclude_job_id: str | None = None,
) -> dict[str, Any] | None:
    if not member_models or not jobs:
        return None
    for job in jobs:
        if not isinstance(job, Mapping):
            continue
        job_id = str(job.get("job_id") or "")
        if exclude_job_id and job_id == exclude_job_id:
            continue
        if is_unresolved_forcing_attempt(job) and forcing_members_overlap(job, member_models):
            return dict(job)
    return None


def is_resolved_forcing_attempt(job: Mapping[str, Any] | None) -> bool:
    """Return whether trusted accounting bound this new forcing attempt."""

    if not forcing_submit_identity_is_complete(job):
        return False
    assert job is not None
    matched = str(job.get("matched_slurm_job_id") or "")
    return bool(
        str(job.get("status") or "") in {"succeeded", "complete", "published"}
        and str(job.get("submit_outcome") or "") == "accepted"
        and str(job.get("reconciliation_decision") or "") == "matched_bound"
        and matched.isdigit()
        and str(job.get("slurm_job_id") or "") == matched
    )


def reordered_forcing_resume_basins(
    basins: Sequence[Mapping[str, Any]], job: Mapping[str, Any] | None
) -> list[dict[str, Any]] | None:
    """Return current basins in the original accepted forcing task order.

    Only an equal member set is safely resumable as one array. Intersecting
    subsets or supersets remain a blocker rather than being misprojected onto
    the wrong task ids.
    """

    if not forcing_submit_identity_is_complete(job):
        return None
    assert job is not None
    current: dict[str, dict[str, Any]] = {}
    for basin in basins:
        model_id = str(basin.get("model_id") or "")
        if not model_id or model_id in current:
            return None
        current[model_id] = dict(basin)
    members = sorted(
        ordered_cohort_members(job.get("cohort_members")),
        key=lambda member: int(member["array_task_id"]),
    )
    member_models = {str(member.get("model_id") or "") for member in members}
    if set(current) != member_models:
        return None
    restored: list[dict[str, Any]] = []
    for member in members:
        basin = dict(current[str(member["model_id"])])
        if any(
            str(basin.get(field) or "") != str(member.get(field) or "")
            for field in ("candidate_id", "run_id", "basin_id", "scenario_id")
        ):
            return None
        basin["task_id"] = int(member["array_task_id"])
        restored.append(basin)
    return restored
