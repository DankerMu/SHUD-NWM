from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from packages.common.redaction import redact_payload
from services.orchestrator.chain_array_accounting import (
    ArrayAccountingDependencies,
    _default_dependencies,
    _format_time,
)
from services.orchestrator.chain_types import (
    ArrayAggregation,
    CycleOrchestrationContext,
    StageDefinition,
)

#: Per-task candidate-outcome / task-outcome evidence ownership for the array
#: accounting layer (#1199 structural split).  ``chain_array_accounting`` keeps
#: the dependency injection surface, the Slurm-sacct parsing, the journal/event
#: stamping, and thin compatibility wrappers for every name owned here, so the
#: legacy ``chain_array_accounting.*`` import and monkeypatch paths stay
#: byte-identical.  This module owns the candidate-outcome construction
#: (including the invocation-local mixed-cohort forced-resubmit veto attachment
#: and ``_matching_veto_basin``), task-outcome recording, safe outcome/event
#: serialization, and stage task-result evidence.


def record_array_task_outcomes(
    context: CycleOrchestrationContext,
    *,
    stage: str,
    aggregation: ArrayAggregation,
    deps: ArrayAccountingDependencies | None = None,
) -> None:
    deps = deps or _default_dependencies()
    basins_by_task = {
        int(basin.get("task_id", index)): dict(basin) for index, basin in enumerate(context.active_basins)
    }
    for task in aggregation.task_results:
        basin = basins_by_task.get(task.task_id)
        if basin is None:
            continue
        original_task_id = deps.basin_original_task_id(basin, task.task_id)
        if task.status == "succeeded":
            previous = context.task_outcomes.get(original_task_id)
            if previous is None or previous.get("status") == "active":
                context.task_outcomes[original_task_id] = deps.safe_candidate_outcome_payload(
                    {
                        "status": "active",
                        "stage": stage,
                        "task_id": task.task_id,
                        "original_task_id": original_task_id,
                        "slurm_job_id": task.slurm_job_id,
                        "exit_code": task.exit_code,
                        "log_uri": task.log_uri,
                        "accounting": dict(task.accounting),
                    }
                )
            continue
        context.task_outcomes[original_task_id] = deps.safe_candidate_outcome_payload(
            {
                "status": task.status if task.status in {"failed", "cancelled"} else "unavailable",
                "stage": stage,
                "task_id": task.task_id,
                "original_task_id": original_task_id,
                "slurm_job_id": task.slurm_job_id,
                "exit_code": task.exit_code,
                "log_uri": task.log_uri,
                "accounting": dict(task.accounting),
                "reason": f"{stage}_task_{task.status}",
            }
        )


def candidate_outcomes(
    context: CycleOrchestrationContext,
    *,
    final_status: str,
    deps: ArrayAccountingDependencies | None = None,
) -> tuple[dict[str, Any], ...]:
    deps = deps or _default_dependencies()
    active_keys = {deps.basin_key(basin) for basin in context.active_basins}
    # #1199: the mixed-cohort forced-resubmit veto record attaches ONLY to the
    # vetoing basin's returned outcome.  A safe exact candidate match means the
    # basin's candidate identity keys are present and unique; when no exact
    # match is available the boolean verdict is preserved and the evidence is
    # omitted rather than misattached to a sibling basin.
    veto = context.forced_resubmit_veto
    vetoed_basin: dict[str, Any] | None = None
    if veto is not None:
        vetoed_basin = _matching_veto_basin(context.all_basins, veto)
    outcomes: list[dict[str, Any]] = []
    for index, basin in enumerate(context.all_basins):
        original_task_id = deps.basin_original_task_id(basin, index)
        task_outcome = dict(context.task_outcomes.get(original_task_id) or {})
        is_active = deps.basin_key(basin) in active_keys
        status = str(task_outcome.get("status") or ("active" if is_active else "unavailable"))
        if final_status == "failed" and is_active and status == "active":
            status = "failed"
        reason = task_outcome.get("reason")
        if reason is None and not is_active:
            reason = str(task_outcome.get("stage") or "array_stage") + "_task_excluded"
        outcome = {
            "candidate_id": basin.get("candidate_id"),
            "run_id": basin.get("run_id"),
            "model_id": basin.get("model_id"),
            "basin_id": basin.get("basin_id"),
            "basin_version_id": basin.get("basin_version_id"),
            "river_network_version_id": basin.get("river_network_version_id"),
            "task_id": int(basin.get("task_id", index)),
            "original_task_id": original_task_id,
            "status": status,
            "reason": reason,
            "failed_stage": (
                task_outcome.get("stage") if status in {"failed", "cancelled", "unavailable"} else None
            ),
            "slurm_job_id": task_outcome.get("slurm_job_id"),
            "exit_code": task_outcome.get("exit_code"),
            "log_uri": task_outcome.get("log_uri"),
            "accounting": task_outcome.get("accounting") or {},
        }
        if vetoed_basin is basin:
            outcome["terminal_stage_forced_resubmit_veto"] = dict(veto)
        outcomes.append(deps.safe_candidate_outcome_payload(outcome))
    return tuple(outcomes)


def _matching_veto_basin(
    basins: Sequence[Mapping[str, Any]],
    veto: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return the ONE basin the invocation-local veto record belongs to.

    Exact identity matching on the basin's candidate keys against the record's
    ``first_veto_*`` fields.  A candidate id alone is sufficient when present
    and unique; otherwise the model/basin identity must agree.  Ambiguous or
    unmatchable shapes return ``None`` so the boolean verdict survives without
    misattaching evidence.
    """

    first_veto_candidate = str(veto.get("first_veto_candidate_id") or "")
    first_veto_model = str(veto.get("first_veto_model_id") or "")
    first_veto_basin = str(veto.get("first_veto_basin_id") or "")
    exact: list[Mapping[str, Any]] = []
    if first_veto_candidate:
        for basin in basins:
            if not isinstance(basin, Mapping):
                continue
            candidate_id = str(basin.get("candidate_id") or "")
            if candidate_id and candidate_id == first_veto_candidate:
                exact.append(basin)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None
    model_matches: list[Mapping[str, Any]] = []
    for basin in basins:
        if not isinstance(basin, Mapping):
            continue
        model_id = str(basin.get("model_id") or "")
        basin_id = str(basin.get("basin_id") or basin.get("model_id") or "")
        if model_id == first_veto_model and basin_id == first_veto_basin:
            model_matches.append(basin)
    if len(model_matches) == 1:
        return model_matches[0]
    return None


def safe_candidate_outcome_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_payload(json_safe_pipeline_event_value(payload))
    return dict(redacted) if isinstance(redacted, Mapping) else {}


def json_safe_pipeline_event_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _format_time(value)
    if isinstance(value, Mapping):
        return {str(key): json_safe_pipeline_event_value(nested) for key, nested in value.items()}
    if isinstance(value, tuple):
        return tuple(json_safe_pipeline_event_value(item) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [json_safe_pipeline_event_value(item) for item in value]
    return value


def safe_pipeline_event_details(details: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_payload(json_safe_pipeline_event_value(details))
    return dict(redacted) if isinstance(redacted, Mapping) else {}


def record_cycle_stage_accounting_event(
    orchestrator: Any,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    pipeline_job_id: str,
    terminal: Mapping[str, Any],
    *,
    log_uri: str | None,
    deps: ArrayAccountingDependencies | None = None,
) -> None:
    deps = deps or _default_dependencies()
    accounting = deps.slurm_accounting_from_payload(terminal)
    if not accounting:
        orchestrator._record_cycle_stage_accounting_gap(
            stage,
            context,
            pipeline_job_id,
            slurm_job_id=str(terminal.get("job_id") or terminal.get("slurm_job_id") or ""),
            message="Slurm accounting metrics were unavailable.",
            details={"reason": "accounting_unavailable"},
        )
        return
    orchestrator.repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=pipeline_job_id,
        event_type="slurm_accounting",
        status_from=None,
        status_to=str(terminal.get("status") or ""),
        message=f"{stage.stage} Slurm accounting captured.",
        details=deps.safe_pipeline_event_details(
            {
                "stage": stage.stage,
                "job_type": stage.job_type,
                "cycle_id": context.cycle_id,
                "slurm": {
                    "job_id": terminal.get("job_id") or terminal.get("slurm_job_id"),
                    "state": terminal.get("state") or terminal.get("status"),
                    "array_task_id": terminal.get("array_task_id"),
                    "exit_code": terminal.get("exit_code"),
                    "log_uri": log_uri,
                    "accounting": accounting,
                    "resource_metrics": deps.resource_metrics_from_payload(terminal),
                },
            }
        ),
    )


def record_cycle_stage_accounting_gap(
    orchestrator: Any,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    pipeline_job_id: str,
    *,
    slurm_job_id: str,
    message: str,
    details: Mapping[str, Any],
    deps: ArrayAccountingDependencies | None = None,
) -> None:
    deps = deps or _default_dependencies()
    orchestrator.repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=pipeline_job_id,
        event_type="slurm_accounting_gap",
        status_from=None,
        status_to="blocked",
        message=message,
        details=deps.safe_pipeline_event_details(
            {
                "stage": stage.stage,
                "job_type": stage.job_type,
                "cycle_id": context.cycle_id,
                "slurm_job_id": slurm_job_id,
                "gap": dict(details),
                "fabricated_metrics": False,
            }
        ),
    )


def stage_task_result_evidence(
    aggregation: ArrayAggregation | None,
    *,
    context: CycleOrchestrationContext | None = None,
    deps: ArrayAccountingDependencies | None = None,
) -> tuple[Mapping[str, Any], ...]:
    if aggregation is None:
        return ()
    deps = deps or _default_dependencies()
    basins_by_task: dict[int, Mapping[str, Any]] = {}
    if context is not None:
        basins_by_task = {
            int(basin.get("task_id", index)): basin for index, basin in enumerate(context.active_basins)
        }
    results: list[Mapping[str, Any]] = []
    for task in aggregation.task_results:
        basin = basins_by_task.get(task.task_id)
        original_task_id = task.task_id if basin is None else deps.basin_original_task_id(basin, task.task_id)
        payload: dict[str, Any] = {
            "array_task_id": task.task_id,
            "task_id": task.task_id,
            "original_task_id": original_task_id,
            "slurm_job_id": task.slurm_job_id,
            "state": task.status,
            "status": task.status,
            "production_status": deps.production_status_for(task.status),
            "exit_code": task.exit_code,
            "error_code": task.error_code,
            "error_message": task.error_message,
            "log_uri": task.log_uri,
            "accounting": dict(task.accounting),
            "resource_metrics": deps.resource_metrics_from_payload(task.accounting),
        }
        if basin is not None:
            for key in (
                "model_id",
                "basin_id",
                "candidate_id",
                "run_id",
                "source_id",
                "cycle_time",
                "canonical_product_id",
                "forcing_version_id",
                "hydro_run_id",
                "published_manifest_id",
            ):
                value = basin.get(key)
                if value not in (None, ""):
                    payload[key] = value
        results.append(deps.safe_pipeline_event_details(payload))
    return tuple(results)


def _basins_by_task_id(context: CycleOrchestrationContext | None) -> dict[int, Mapping[str, Any]]:
    """Rebuild the task -> basin mapping using the module's canonical keying rule.

    ``record_array_task_outcomes`` keeps its own local mapping; the stamp sites
    below run on the re-indexed cohort and must key on the member's own
    ``run_id``, never ``context.run_id``.
    """

    if context is None:
        return {}
    mapping: dict[int, Mapping[str, Any]] = {}
    for index, basin in enumerate(getattr(context, "active_basins", ()) or ()):
        if not isinstance(basin, Mapping):
            continue
        try:
            task_id = int(basin.get("task_id", index))
        except (TypeError, ValueError):
            continue
        mapping[task_id] = basin
    return mapping
