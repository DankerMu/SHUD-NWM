from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol, Sequence

from packages.common.redaction import redact_payload
from services.orchestrator.chain_types import (
    ArrayAggregation,
    ArrayTaskResult,
    CycleOrchestrationContext,
    OrchestratorError,
    StageDefinition,
)
from services.orchestrator.production_contract import production_status_for


class _ObjectStoreLike(Protocol):
    def uri_for_key(self, key: str) -> str: ...


@dataclass(frozen=True)
class ArrayAccountingDependencies:
    coerce_mapping: Callable[[Any], dict[str, Any]]
    safe_candidate_outcome_payload: Callable[[Mapping[str, Any]], dict[str, Any]]
    safe_pipeline_event_details: Callable[[Mapping[str, Any]], dict[str, Any]]
    record_array_task_outcomes: Callable[..., None]
    stage_task_result_evidence: Callable[..., tuple[Mapping[str, Any], ...]]
    parse_sacct_array_results: Callable[..., ArrayAggregation]
    coerce_array_aggregation: Callable[..., ArrayAggregation]
    aggregation_from_task_results: Callable[[Sequence[ArrayTaskResult]], ArrayAggregation]
    aggregation_error_code: Callable[[ArrayAggregation | None], str | None]
    aggregation_error_message: Callable[[ArrayAggregation | None], str | None]
    sacct_extra_fields: Callable[[Sequence[str]], dict[str, Any]]
    slurm_accounting_from_payload: Callable[[Mapping[str, Any]], dict[str, Any]]
    resource_metrics_from_payload: Callable[[Mapping[str, Any]], dict[str, Any]]
    context_array_log_uri: Callable[[CycleOrchestrationContext | None, Any, str, int], str | None]
    array_task_status: Callable[[str], str]
    parse_slurm_exit_code: Callable[[str], int | None]
    basin_key: Callable[[Mapping[str, Any]], tuple[str, str]]
    basin_original_task_id: Callable[[Mapping[str, Any], int], int]
    status_from_gateway_job: Callable[[Mapping[str, Any]], str]
    parse_gateway_time: Callable[[Any], datetime | None]
    utcnow: Callable[[], datetime]
    build_reindexed_manifest: Callable[[Sequence[Mapping[str, Any]], Sequence[int]], list[dict[str, Any]]]


def _default_dependencies() -> ArrayAccountingDependencies:
    return ArrayAccountingDependencies(
        coerce_mapping=_coerce_mapping,
        safe_candidate_outcome_payload=safe_candidate_outcome_payload,
        safe_pipeline_event_details=safe_pipeline_event_details,
        record_array_task_outcomes=record_array_task_outcomes,
        stage_task_result_evidence=stage_task_result_evidence,
        parse_sacct_array_results=parse_sacct_array_results,
        coerce_array_aggregation=coerce_array_aggregation,
        aggregation_from_task_results=aggregation_from_task_results,
        aggregation_error_code=aggregation_error_code,
        aggregation_error_message=aggregation_error_message,
        sacct_extra_fields=sacct_extra_fields,
        slurm_accounting_from_payload=slurm_accounting_from_payload,
        resource_metrics_from_payload=resource_metrics_from_payload,
        context_array_log_uri=context_array_log_uri,
        array_task_status=array_task_status,
        parse_slurm_exit_code=parse_slurm_exit_code,
        basin_key=basin_key,
        basin_original_task_id=basin_original_task_id,
        status_from_gateway_job=status_from_gateway_job,
        parse_gateway_time=parse_gateway_time,
        utcnow=utcnow,
        build_reindexed_manifest=build_reindexed_manifest,
    )


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
        outcomes.append(
            deps.safe_candidate_outcome_payload(
                {
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
            )
        )
    return tuple(outcomes)


def aggregate_array_stage(
    orchestrator: Any,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    slurm_job_id: str,
    terminal: dict[str, Any],
    pipeline_job_id: str,
    *,
    deps: ArrayAccountingDependencies | None = None,
) -> ArrayAggregation:
    deps = deps or _default_dependencies()
    provider = getattr(orchestrator.slurm_client, "get_array_task_results", None)
    if callable(provider):
        try:
            raw_results = provider(slurm_job_id)
        except (KeyError, LookupError):
            raw_results = None
        except (TypeError, ValueError, OrchestratorError) as error:
            orchestrator._record_cycle_stage_accounting_gap(
                stage,
                context,
                pipeline_job_id,
                slurm_job_id=slurm_job_id,
                message="Slurm array accounting was unavailable or malformed.",
                details={"error": str(error), "error_code": getattr(error, "error_code", None)},
            )
            raw_results = None
        if raw_results is not None:
            try:
                aggregation = deps.coerce_array_aggregation(
                    raw_results,
                    slurm_job_id,
                    context=context,
                    object_store=orchestrator.object_store,
                )
                return orchestrator._require_complete_array_accounting(
                    aggregation,
                    stage=stage,
                    context=context,
                    slurm_job_id=slurm_job_id,
                )
            except (TypeError, ValueError, OrchestratorError) as error:
                orchestrator._record_cycle_stage_accounting_gap(
                    stage,
                    context,
                    pipeline_job_id,
                    slurm_job_id=slurm_job_id,
                    message="Slurm array accounting was unavailable or malformed.",
                    details={"error": str(error), "error_code": getattr(error, "error_code", None)},
                )
            raw_results = None

    stdout_provider = getattr(orchestrator.slurm_client, "get_array_sacct_output", None)
    if callable(stdout_provider):
        try:
            aggregation = deps.parse_sacct_array_results(
                str(stdout_provider(slurm_job_id)),
                slurm_job_id,
                context=context,
                object_store=orchestrator.object_store,
            )
            return orchestrator._require_complete_array_accounting(
                aggregation,
                stage=stage,
                context=context,
                slurm_job_id=slurm_job_id,
            )
        except OrchestratorError as error:
            orchestrator._record_cycle_stage_accounting_gap(
                stage,
                context,
                pipeline_job_id,
                slurm_job_id=slurm_job_id,
                message="Slurm array accounting was unavailable or malformed.",
                details={"error": error.message, "error_code": error.error_code},
            )

    orchestrator._record_cycle_stage_accounting_gap(
        stage,
        context,
        pipeline_job_id,
        slurm_job_id=slurm_job_id,
        message="Slurm array accounting was unavailable or incomplete.",
        details={
            "reason": "array_task_accounting_unavailable",
            "master_status": deps.status_from_gateway_job(terminal),
            "expected_task_count": len(context.active_basins),
        },
    )
    return ArrayAggregation(total=0, succeeded=0, failed=0, cancelled=0, task_results=())


def require_complete_array_accounting(
    aggregation: ArrayAggregation,
    *,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    slurm_job_id: str,
) -> ArrayAggregation:
    expected_task_ids = set(range(len(context.active_basins)))
    observed_task_ids = {task.task_id for task in aggregation.task_results}
    if observed_task_ids == expected_task_ids:
        return aggregation
    missing_task_ids = sorted(expected_task_ids - observed_task_ids)
    unexpected_task_ids = sorted(observed_task_ids - expected_task_ids)
    raise OrchestratorError(
        "SLURM_ARRAY_ACCOUNTING_INCOMPLETE",
        "Slurm array accounting did not include exactly the submitted task ids.",
        {
            "slurm_job_id": slurm_job_id,
            "stage": stage.stage,
            "expected_task_count": len(context.active_basins),
            "observed_task_count": len(observed_task_ids),
            "missing_task_ids": missing_task_ids,
            "unexpected_task_ids": unexpected_task_ids,
        },
    )


def record_cycle_stage_status_override(
    orchestrator: Any,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    pipeline_job_id: str,
    terminal: dict[str, Any],
    aggregation: ArrayAggregation,
    log_uri: str | None,
    *,
    deps: ArrayAccountingDependencies | None = None,
) -> None:
    deps = deps or _default_dependencies()
    previous_status, record = orchestrator.repository.update_pipeline_job_status(
        pipeline_job_id,
        aggregation.status,
        finished_at=deps.parse_gateway_time(terminal.get("finished_at")) or deps.utcnow(),
        exit_code=terminal.get("exit_code"),
        error_code=deps.aggregation_error_code(aggregation) or terminal.get("error_code"),
        error_message=deps.aggregation_error_message(aggregation) or terminal.get("error_message"),
        log_uri=log_uri,
    )
    if str(record.get("status")) != aggregation.status:
        return
    task_payload = deps.stage_task_result_evidence(aggregation, context=context)
    orchestrator.repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=pipeline_job_id,
        event_type="status_change",
        status_from=previous_status or deps.status_from_gateway_job(terminal),
        status_to=aggregation.status,
        message=f"{stage.stage} array aggregated as {aggregation.status}",
        details=deps.safe_pipeline_event_details(
            {
                "stage": stage.stage,
                "job_type": stage.job_type,
                "total": aggregation.total,
                "succeeded": aggregation.succeeded,
                "failed": aggregation.failed,
                "cancelled": aggregation.cancelled,
                "pipeline_job_id": pipeline_job_id,
                "slurm": {
                    "job_id": terminal.get("job_id") or terminal.get("slurm_job_id"),
                    "state": aggregation.status,
                    "exit_code": terminal.get("exit_code"),
                    "log_uri": log_uri,
                    "accounting": deps.slurm_accounting_from_payload(terminal),
                    "task_results": task_payload,
                    "resource_metrics": deps.resource_metrics_from_payload(terminal),
                },
                "task_results": task_payload,
            }
        ),
    )


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


def apply_array_progress(
    orchestrator: Any,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    aggregation: ArrayAggregation,
    *,
    deps: ArrayAccountingDependencies | None = None,
) -> None:
    deps = deps or _default_dependencies()
    deps.record_array_task_outcomes(context, stage=stage.stage, aggregation=aggregation)
    if aggregation.status == "succeeded":
        if context.had_partial and stage.stage in {"parse", "state_save_qc", "frequency"}:
            context.last_partial_status = "parsed_partial"
        return
    if aggregation.status == "failed":
        context.active_basins = []
        return
    context.had_partial = True
    context.last_partial_status = orchestrator._partial_cycle_status(stage)
    context.active_basins = deps.build_reindexed_manifest(context.active_basins, aggregation.succeeded_task_ids)


def safe_candidate_outcome_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_payload(json_safe_pipeline_event_value(payload))
    return dict(redacted) if isinstance(redacted, Mapping) else {}


def parse_sacct_array_results(
    stdout: str,
    master_job_id: str,
    *,
    context: CycleOrchestrationContext | None = None,
    object_store: _ObjectStoreLike | None = None,
    deps: ArrayAccountingDependencies | None = None,
) -> ArrayAggregation:
    deps = deps or _default_dependencies()
    task_pattern = re.compile(rf"^{re.escape(master_job_id)}_(\d+)$")
    results: list[ArrayTaskResult] = []
    for raw_line in stdout.splitlines():
        if not raw_line.strip():
            continue
        fields = raw_line.rstrip("\n").split("|")
        if len(fields) < 3:
            raise OrchestratorError(
                "SLURM_SACCT_PARSE_ERROR",
                "Unable to parse array sacct output.",
                {"line": raw_line, "master_job_id": master_job_id},
            )
        job_id, raw_state, raw_exit_code = fields[0], fields[1], fields[2]
        match = task_pattern.fullmatch(job_id)
        if match is None:
            continue
        task_id = int(match.group(1))
        extras = deps.sacct_extra_fields(fields[3:])
        task_status = deps.array_task_status(raw_state)
        results.append(
            ArrayTaskResult(
                task_id=task_id,
                slurm_job_id=job_id,
                status=task_status,
                exit_code=deps.parse_slurm_exit_code(raw_exit_code),
                error_code=None if task_status == "succeeded" else "NODE_FAILURE",
                log_uri=deps.context_array_log_uri(context, object_store, master_job_id, task_id),
                accounting=extras,
            )
        )
    return deps.aggregation_from_task_results(tuple(sorted(results, key=lambda result: result.task_id)))


def coerce_array_aggregation(
    raw_results: Any,
    master_job_id: str,
    *,
    context: CycleOrchestrationContext | None = None,
    object_store: _ObjectStoreLike | None = None,
    deps: ArrayAccountingDependencies | None = None,
) -> ArrayAggregation:
    deps = deps or _default_dependencies()
    if isinstance(raw_results, ArrayAggregation):
        return raw_results
    if isinstance(raw_results, str):
        return deps.parse_sacct_array_results(
            raw_results,
            master_job_id,
            context=context,
            object_store=object_store,
        )
    if isinstance(raw_results, Mapping):
        if isinstance(raw_results.get("stdout"), str):
            return deps.parse_sacct_array_results(
                str(raw_results["stdout"]),
                master_job_id,
                context=context,
                object_store=object_store,
            )
        tasks = raw_results.get("tasks") or raw_results.get("task_results")
        if isinstance(tasks, Sequence) and not isinstance(tasks, str | bytes):
            return deps.coerce_array_aggregation(
                tasks,
                master_job_id,
                context=context,
                object_store=object_store,
            )
    if isinstance(raw_results, Sequence) and not isinstance(raw_results, str | bytes):
        task_results = []
        for index, item in enumerate(raw_results):
            item_dict = deps.coerce_mapping(item)
            task_id = int(item_dict.get("task_id", index))
            status = str(item_dict.get("status") or deps.array_task_status(str(item_dict.get("state", ""))))
            accounting = deps.slurm_accounting_from_payload(item_dict)
            task_results.append(
                ArrayTaskResult(
                    task_id=task_id,
                    slurm_job_id=str(
                        item_dict.get("slurm_job_id") or item_dict.get("job_id") or f"{master_job_id}_{task_id}"
                    ),
                    status=status,
                    exit_code=item_dict.get("exit_code"),
                    error_code=(
                        str(item_dict.get("error_code"))
                        if item_dict.get("error_code") not in (None, "")
                        else (None if status == "succeeded" else "NODE_FAILURE")
                    ),
                    error_message=str(item_dict.get("error_message"))
                    if item_dict.get("error_message") not in (None, "")
                    else None,
                    log_uri=str(item_dict.get("log_uri"))
                    if item_dict.get("log_uri") not in (None, "")
                    else deps.context_array_log_uri(context, object_store, master_job_id, task_id),
                    accounting=accounting,
                )
            )
        return deps.aggregation_from_task_results(tuple(sorted(task_results, key=lambda result: result.task_id)))
    raise TypeError(f"Unsupported array task result payload: {type(raw_results).__name__}")


def aggregation_from_task_results(results: Sequence[ArrayTaskResult]) -> ArrayAggregation:
    return ArrayAggregation(
        total=len(results),
        succeeded=sum(1 for result in results if result.status == "succeeded"),
        failed=sum(1 for result in results if result.status == "failed"),
        cancelled=sum(1 for result in results if result.status == "cancelled"),
        task_results=tuple(results),
    )


def aggregation_error_code(aggregation: ArrayAggregation | None) -> str | None:
    if aggregation is None or aggregation.status == "succeeded":
        return None
    for task in aggregation.task_results:
        if task.status != "succeeded" and task.error_code not in (None, ""):
            return str(task.error_code)
    return "NODE_FAILURE" if aggregation.failed else None


def aggregation_error_message(aggregation: ArrayAggregation | None) -> str | None:
    if aggregation is None or aggregation.status == "succeeded":
        return None
    for task in aggregation.task_results:
        if task.status != "succeeded" and task.error_message not in (None, ""):
            return str(task.error_message)
    return None


def sacct_extra_fields(fields: Sequence[str]) -> dict[str, Any]:
    names = ("elapsed", "max_rss", "ave_rss", "alloc_tres", "max_disk_read", "max_disk_write")
    return {name: value for name, value in zip(names, fields, strict=False) if value not in (None, "")}


def slurm_accounting_from_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("accounting") or payload.get("resource_metrics")
    accounting = dict(raw) if isinstance(raw, Mapping) else {}
    aliases = {
        "elapsed": ("elapsed", "elapsed_time"),
        "max_rss": ("max_rss", "MaxRSS", "maxrss"),
        "ave_rss": ("ave_rss", "AveRSS", "averss"),
        "alloc_tres": ("alloc_tres", "AllocTRES", "tres"),
        "max_disk_read": ("max_disk_read", "MaxDiskRead"),
        "max_disk_write": ("max_disk_write", "MaxDiskWrite"),
    }
    for normalized, keys in aliases.items():
        if normalized in accounting:
            continue
        for key in keys:
            if key in payload and payload[key] not in (None, ""):
                accounting[normalized] = payload[key]
                break
    return accounting


def resource_metrics_from_payload(
    payload: Mapping[str, Any],
    *,
    slurm_accounting: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    accounting = (slurm_accounting or slurm_accounting_from_payload)(payload)
    return {
        key: value
        for key, value in accounting.items()
        if key in {"elapsed", "max_rss", "ave_rss", "alloc_tres", "max_disk_read", "max_disk_write"}
    }


def safe_pipeline_event_details(details: Mapping[str, Any]) -> dict[str, Any]:
    redacted = redact_payload(json_safe_pipeline_event_value(details))
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
            "production_status": production_status_for(task.status),
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


def context_array_log_uri(
    context: CycleOrchestrationContext | None,
    object_store: _ObjectStoreLike | None,
    master_job_id: str,
    task_id: int,
) -> str | None:
    if context is None or object_store is None:
        return None
    return array_task_log_uri(object_store, context.run_id, master_job_id, task_id)


def array_task_log_uri(object_store: _ObjectStoreLike, run_id: str, master_job_id: str, task_id: int) -> str:
    return object_store.uri_for_key(f"runs/{run_id}/logs/{master_job_id}_{task_id}.out")


def status_from_gateway_job(job: Mapping[str, Any]) -> str:
    status = job.get("status", "submitted")
    value = getattr(status, "value", status)
    normalized = str(value)
    return "pending" if normalized == "submitted" else normalized


def parse_gateway_time(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return _ensure_utc(value) if isinstance(value, datetime) else None
    if isinstance(value, str):
        return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    return None


def utcnow() -> datetime:
    return datetime.now(UTC)


def build_reindexed_manifest(
    entries: Sequence[Mapping[str, Any]],
    succeeded_task_ids: Sequence[int],
) -> list[dict[str, Any]]:
    by_task_id = {int(entry.get("task_id", index)): dict(entry) for index, entry in enumerate(entries)}
    reindexed: list[dict[str, Any]] = []
    for new_task_id, previous_task_id in enumerate(succeeded_task_ids):
        entry = dict(by_task_id[int(previous_task_id)])
        entry["task_id"] = new_task_id
        entry["original_task_id"] = int(entry.get("original_task_id", previous_task_id))
        reindexed.append(entry)
    return reindexed


def array_task_status(raw_state: str) -> str:
    normalized = raw_state.strip().upper().split()[0].rstrip("+")
    if normalized == "COMPLETED":
        return "succeeded"
    if normalized == "CANCELLED":
        return "cancelled"
    return "failed"


def parse_slurm_exit_code(raw_exit_code: str) -> int | None:
    if not raw_exit_code:
        return None
    try:
        return int(raw_exit_code.split(":", maxsplit=1)[0])
    except ValueError:
        return None


def basin_key(basin: Mapping[str, Any]) -> tuple[str, str]:
    return (str(basin.get("model_id") or ""), str(basin.get("basin_id") or basin.get("model_id") or ""))


def basin_identifier(basin: Mapping[str, Any]) -> str:
    return str(basin.get("basin_id") or basin.get("model_id") or "")


def basin_original_task_id(basin: Mapping[str, Any], fallback: int) -> int:
    try:
        return int(basin.get("original_task_id", basin.get("task_id", fallback)))
    except (TypeError, ValueError):
        return fallback


def _coerce_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump(mode="json"))
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    raise TypeError(f"Expected mapping-like Slurm payload, got {type(value).__name__}")


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")
