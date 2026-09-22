"""Execution-cohort grouping by gateway resource profile and array budgets (#2543).

The Slurm gateway sizes a whole array from task 0's resource profile, so the
scheduler splits each restart-compatible cohort by the gateway's override keys
and then allocates the strict cross-array Slurm concurrency budget.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

RESOURCE_PROFILE_DEFAULT_KEY = "default"


def resource_profile_candidate_groups(
    candidates: Sequence[Any],
    override_model_ids: frozenset[str] | set[str],
) -> list[list[Any]]:
    """Split one restart-compatible cohort by gateway resource-profile key (#2543).

    The gateway sizes a whole Slurm array from task 0's profile, so every model
    with an ``overrides`` entry gets its own group (and is task 0 there); all
    other models stay together in one ``default`` group, in their original
    order.  With no override member this returns the input as the single group,
    keeping membership and cohort run ids unchanged.
    """

    default_group: list[Any] = []
    override_groups: dict[str, list[Any]] = {}
    for candidate in candidates:
        if candidate.model_id in override_model_ids:
            override_groups.setdefault(candidate.model_id, []).append(candidate)
        else:
            default_group.append(candidate)
    groups = [default_group] if default_group else []
    groups.extend(override_groups[model_id] for model_id in sorted(override_groups))
    return groups


def resource_profile_split_evidence(
    units: Sequence[Any],
    override_model_ids: frozenset[str] | None,
) -> dict[str, Any]:
    """Pass evidence: was the #2543 resource-profile split active, and what did it do."""

    return {
        "active": override_model_ids is not None,
        "inactive_reason": None if override_model_ids is not None else "gateway_backend_not_slurm",
        "override_model_ids": sorted(override_model_ids or ()),
        "applied_override_model_ids": sorted(
            {unit.profile_key for unit in units if unit.profile_key != RESOURCE_PROFILE_DEFAULT_KEY}
        ),
        "units": [
            {
                "source_id": unit.source_id,
                "cycle_id": unit.cycle_id,
                "cohort_run_id": unit.cohort_run_id,
                "profile_key": unit.profile_key,
                "task_count": len(unit.execution_candidates),
                "array_max_concurrent": unit.array_max_concurrent,
            }
            for unit in units
        ],
    }


def resource_profile_override_model_ids() -> frozenset[str] | None:
    """Override keys of the gateway's resource profile file (fail closed).

    Resolves the path exactly like the gateway (``SLURM_GATEWAY_RESOURCE_PROFILES_PATH``,
    default ``config/resource_profiles.yaml`` relative to the working directory)
    and loads it through the gateway's own loader.  Only the real ``slurm``
    backend reads that file; the mock backend never sizes arrays from it, so
    there is no profile to split by and the cohort stays whole.
    """

    from services.slurm_gateway.config import SlurmGatewaySettings
    from services.slurm_gateway.resource_profiles import (
        resource_profile_override_model_ids as _override_model_ids,
    )

    settings = SlurmGatewaySettings()
    if settings.backend != "slurm":  # same selector as services.slurm_gateway.gateway.create_gateway
        return None
    return _override_model_ids(settings.resource_profiles_path)


def _array_concurrency_budgets(
    units: Sequence[Any],
    *,
    global_bound: int,
    worker_bound: int,
) -> list[int]:
    """Allocate a strict cross-cohort Slurm array budget.

    When every unit can start immediately, round-robin water filling preserves
    small cohorts and uses the entire global budget whenever enough tasks
    exist.  If units must queue behind the control-thread bound, every active
    slot receives the same conservative ceiling; any combination of running
    slots therefore remains within ``global_bound`` as queued units advance.
    """

    if not units:
        return []
    global_bound = max(int(global_bound), 1)
    active_slots = min(len(units), max(int(worker_bound), 1), global_bound)
    task_counts = [max(len(unit.execution_candidates), 1) for unit in units]
    if len(units) > active_slots:
        # #2543: weight by member count.  Water-fill the global budget over the
        # ``active_slots`` largest units only; every other unit is capped at the
        # smallest of those budgets.  Budgets are then monotone in task count,
        # so any ``active_slots`` units running together sum to at most the
        # water-filled total (<= global_bound), while a 1-task override unit no
        # longer claims a full ``global_bound // active_slots`` share.
        order = sorted(range(len(units)), key=lambda index: -task_counts[index])
        leaders = order[:active_slots]
        leader_budgets = _water_fill([task_counts[index] for index in leaders], global_bound)
        ceiling = min(leader_budgets)
        budgets = [min(task_count, ceiling) for task_count in task_counts]
        for index, budget in zip(leaders, leader_budgets, strict=True):
            budgets[index] = budget
        return budgets
    return _water_fill(task_counts, global_bound)


def _water_fill(task_counts: Sequence[int], global_bound: int) -> list[int]:
    budgets = [0] * len(task_counts)
    remaining = global_bound
    while remaining > 0:
        advanced = False
        for index, task_count in enumerate(task_counts):
            if budgets[index] >= task_count:
                continue
            budgets[index] += 1
            remaining -= 1
            advanced = True
            if remaining == 0:
                break
        if not advanced:
            break
    return [max(budget, 1) for budget in budgets]
