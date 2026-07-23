from __future__ import annotations

import inspect
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    AcceptedSubmitTransition,
    accepted_submit_pipeline_job_model_id,
)
from services.orchestrator.chain_config import SubmitDisposition
from services.orchestrator.chain_types import (
    ArrayAggregation,
    CycleOrchestrationContext,
    DisplayLogPublication,
    DisplayLogPublicationAttempt,
    OrchestratorError,
    StageDefinition,
    StageRunResult,
    TerminalJobObservation,
)


class StageExecutionOrchestrator(Protocol):
    config: Any
    repository: Any
    slurm_client: Any
    object_store: Any


_FORECAST_STAGE_ALIASES = frozenset({"forecast", "run_shud_forecast", "run_shud_forecast_array"})
_ACCEPTED_GATEWAY_SUBMIT_STATUSES = frozenset(
    {
        "submitted",
        "pending",
        "queued",
        "running",
        "succeeded",
        "partially_failed",
        "failed",
        "cancelled",
        "submission_failed",
        "reservation_lost",
        "permanently_failed",
    }
)


def is_forecast_cohort_stage(stage: StageDefinition) -> bool:
    """Return whether a stage belongs to the canonical native forecast family."""
    stage_name = str(stage.stage or "")
    if stage_name:
        return stage_name in _FORECAST_STAGE_ALIASES
    return str(stage.job_type or "") in _FORECAST_STAGE_ALIASES


def _submit_error_is_ambiguous(error: Exception, *, gateway_boundary_entered: bool) -> bool:
    if not gateway_boundary_entered:
        return False
    disposition = getattr(error, "submit_disposition", None)
    try:
        return SubmitDisposition(disposition) is not SubmitDisposition.REJECTED
    except (TypeError, ValueError):
        # Once the Gateway call boundary is entered, missing/unknown proof is
        # ambiguity-safe. Only an explicit REJECTED disposition may reopen the
        # attempt for a later submit.
        return True


def _accepts_keyword(callable_value: Callable[..., Any], name: str) -> bool:
    try:
        parameters = tuple(inspect.signature(callable_value).parameters.values())
    except (TypeError, ValueError):
        return False
    return any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters) or any(
        parameter.name == name for parameter in parameters
    )


def _update_runtime_pipeline_status(
    orchestrator: StageExecutionOrchestrator,
    stage: StageDefinition,
    pipeline_job_id: str,
    status: str,
    *,
    current_status: str,
    started_at: Any = None,
    finished_at: Any = None,
    exit_code: Any = None,
    error_code: Any = None,
    error_message: Any = None,
    log_uri: Any = None,
) -> tuple[str | None, dict[str, Any]]:
    accepted_submit_runtime = bool(
        getattr(orchestrator.repository, "supports_accepted_submit_reconcile", False)
        and is_forecast_cohort_stage(stage)
    )
    if accepted_submit_runtime:
        transitioner = getattr(orchestrator.repository, "transition_pipeline_job_runtime_status", None)
        if not callable(transitioner):
            raise OrchestratorError(
                "ACCEPTED_SUBMIT_RUNTIME_TRANSITION_UNAVAILABLE",
                "forecast cohort runtime transition API is unavailable",
            )
        result = transitioner(
            pipeline_job_id,
            status,
            expected_statuses=(current_status,),
            started_at=started_at,
            exit_code=exit_code,
        )
        if not getattr(result, "committed", False):
            raise OrchestratorError(
                "ACCEPTED_SUBMIT_RUNTIME_TRANSITION_CONFLICT",
                "forecast cohort runtime state no longer matches the observed status",
            )
        return current_status, dict(getattr(result, "row", None) or {})
    return orchestrator.repository.update_pipeline_job_status(
        pipeline_job_id,
        status,
        started_at=started_at,
        finished_at=finished_at,
        exit_code=exit_code,
        error_code=error_code,
        error_message=error_message,
        log_uri=log_uri,
    )


@dataclass(frozen=True)
class StageExecutionDependencies:
    terminal_job_statuses: frozenset[str]
    pipeline_job_id: Callable[[str, str], str]
    published_artifact_root_configured: Callable[[], bool]
    cycle_stage_idempotency_key: Callable[..., str]
    slurm_comment_for: Callable[[str], str]
    cycle_payload_model_id: Callable[[CycleOrchestrationContext], str]
    cycle_pipeline_job_model_id: Callable[[CycleOrchestrationContext], str | None]
    coerce_mapping: Callable[[Any], dict[str, Any]]
    coerce_array_task_id: Callable[[Any], int | None]
    status_from_gateway_job: Callable[[Mapping[str, Any]], str]
    parse_gateway_time: Callable[[Any], Any]
    utcnow: Callable[[], Any]
    format_time: Callable[[Any], str]
    safe_pipeline_event_details: Callable[[Mapping[str, Any]], dict[str, Any]]
    submission_runtime_root_contract: Callable[[Mapping[str, Any]], dict[str, Any]]
    aggregation_error_code: Callable[[ArrayAggregation | None], str | None]
    aggregation_error_message: Callable[[ArrayAggregation | None], str | None]
    slurm_accounting_from_payload: Callable[[Mapping[str, Any]], dict[str, Any]]
    resource_metrics_from_payload: Callable[[Mapping[str, Any]], dict[str, Any]]
    stage_task_result_evidence: Callable[..., tuple[Mapping[str, Any], ...]]
    stage_status_message: Callable[[str, str, dict[str, Any]], str]
    make_slurm_client_error: Callable[[str, str, dict[str, Any]], Exception]
    tile_publisher_cls: type[Any]
    publish_error_cls: type[BaseException] | tuple[type[BaseException], ...]
    failure_payload: Callable[[str, Any], dict[str, Any]]
    redact_payload: Callable[[Any], Any]


def _dependencies(
    orchestrator: StageExecutionOrchestrator,
    deps: StageExecutionDependencies | None,
) -> StageExecutionDependencies:
    if deps is not None:
        return deps
    provider = getattr(orchestrator, "_chain_stage_execution_dependencies", None)
    if callable(provider):
        return provider()
    raise RuntimeError("chain stage execution dependencies are unavailable")


def _call_orchestrator_helper(orchestrator: StageExecutionOrchestrator, name: str, *args: Any, **kwargs: Any) -> Any:
    helper = getattr(orchestrator, name, None)
    if callable(helper):
        return helper(*args, **kwargs)
    return globals()[name.removeprefix("_")](
        orchestrator,
        *args,
        **kwargs,
    )


def submit_and_wait_cycle_stage(
    orchestrator: StageExecutionOrchestrator,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    *,
    pipeline_job_id: str | None = None,
    deps: StageExecutionDependencies | None = None,
) -> tuple[StageRunResult, ArrayAggregation | None]:
    deps = _dependencies(orchestrator, deps)
    pipeline_job_id = pipeline_job_id or deps.pipeline_job_id(context.run_id, stage.stage)
    if stage.stage == "publish" and deps.published_artifact_root_configured():
        return (
            _call_orchestrator_helper(
                orchestrator,
                "_run_local_publish_stage",
                stage,
                context,
                pipeline_job_id=pipeline_job_id,
            ),
            None,
        )
    if stage.is_array and not context.active_basins:
        orchestrator.repository.update_forecast_cycle_status(
            source_id=context.source_id,
            cycle_time=context.cycle_time,
            status=stage.failure_cycle_status,
            error_code="NO_ACTIVE_BASINS",
            error_message=f"No basins available for {stage.stage}.",
        )
        return (
            StageRunResult(
                stage=stage.stage,
                job_type=stage.job_type,
                pipeline_job_id=pipeline_job_id,
                slurm_job_id="",
                status="failed",
                error_code="NO_ACTIVE_BASINS",
                error_message=f"No basins available for {stage.stage}.",
                task_results=(),
            ),
            None,
        )

    orchestrator._before_cycle_stage_submit(stage, context)

    # M24 §3A phase 1: durable reservation BEFORE sbatch. Idempotent across
    # overlapping passes and the submit-crash window. Best-effort against
    # repositories that predate the reservation methods.
    idempotency_key = deps.cycle_stage_idempotency_key(context, stage, pipeline_job_id=pipeline_job_id)
    reservation = orchestrator._reserve_cycle_stage(stage, context, pipeline_job_id, idempotency_key)

    # M24 §3A reserve gate: when a concurrent pass already holds an active
    # reservation for this candidate+stage, skip sbatch entirely (no double
    # submission). Only a pass that truly won the reservation (created) - or
    # a legacy repo without the reservation surface - proceeds to sbatch.
    if orchestrator._reservation_already_inflight(reservation):
        return orchestrator._skip_duplicate_submission(stage, context, pipeline_job_id, reservation), None
    if reservation is not None and reservation.created:
        # A reclaimed durable reservation increments the authoritative attempt.
        # Runtime manifests/placeholders must carry that exact attempt so a
        # later accepted-submit ambiguity releases the current, not stale, rows.
        context.retry_attempt = reservation.submission_attempt

    submitted: dict[str, Any]
    manifest_index_path: Path | None = None
    stage_manifest = orchestrator._build_cycle_stage_manifest(stage, context)
    gateway_boundary_entered = False
    try:
        orchestrator._prepare_forecast_runtime_manifests(stage, context)
        if stage.stage == "forecast":
            stage_manifest = orchestrator._build_cycle_stage_manifest(stage, context)
        if stage.is_array:
            tasks = orchestrator._reindexed_manifest_entries(context.active_basins)
            manifest_index_path = orchestrator._write_cycle_manifest_index(context, stage, tasks)
            stage_manifest["manifest_index_path"] = str(manifest_index_path)
            # Array path must carry the same idempotency --comment as the
            # single-job path so crash-recovery can reconcile array masters.
            stage_manifest["comment"] = deps.slurm_comment_for(idempotency_key)
            gateway_boundary_entered = True
            submitted = _call_orchestrator_helper(
                orchestrator,
                "_submit_array_stage",
                stage,
                context,
                tasks,
                stage_manifest,
            )
        else:
            submit_payload = {
                "run_id": context.run_id,
                "model_id": deps.cycle_payload_model_id(context),
                "job_type": stage.job_type,
                "manifest": _call_orchestrator_helper(
                    orchestrator,
                    "_slurm_submission_manifest",
                    stage_manifest,
                ),
                "comment": deps.slurm_comment_for(idempotency_key),
            }
            gateway_boundary_entered = True
            submitted = deps.coerce_mapping(
                orchestrator.slurm_client.submit_job(submit_payload)
            )
        submitted_job_id = submitted.get("job_id")
        accepted_submit_repository = bool(
            getattr(orchestrator.repository, "supports_accepted_submit_reconcile", False)
            and is_forecast_cohort_stage(stage)
        )
        valid_legacy_mock_id = (
            isinstance(submitted_job_id, str)
            and submitted_job_id.startswith("mock_")
            and submitted_job_id.removeprefix("mock_").isdigit()
        )
        if not isinstance(submitted_job_id, str) or not (
            submitted_job_id.isdigit() or (valid_legacy_mock_id and not accepted_submit_repository)
        ):
            error = OrchestratorError(
                "SLURM_GATEWAY_INVALID_RESPONSE",
                "Slurm submit response did not contain a valid master job id.",
            )
            error.submit_disposition = SubmitDisposition.AMBIGUOUS
            raise error
        raw_submitted_status = submitted.get("status")
        raw_status_value = getattr(raw_submitted_status, "value", raw_submitted_status)
        if (
            accepted_submit_repository
            and (
                type(raw_status_value) is not str
                or not raw_status_value
                or raw_status_value not in _ACCEPTED_GATEWAY_SUBMIT_STATUSES
            )
        ):
            error = OrchestratorError(
                "SLURM_GATEWAY_INVALID_RESPONSE",
                "Slurm submit response did not contain a recognized status.",
            )
            error.submit_disposition = SubmitDisposition.AMBIGUOUS
            raise error
    except Exception as error:
        if (
            getattr(orchestrator.repository, "supports_accepted_submit_reconcile", False)
            and is_forecast_cohort_stage(stage)
            and _submit_error_is_ambiguous(error, gateway_boundary_entered=gateway_boundary_entered)
        ):
            transition = getattr(
                orchestrator.repository,
                "transition_pipeline_job_submit_evidence",
                None,
            )
            if not callable(transition):
                raise OrchestratorError(
                    "ACCEPTED_SUBMIT_COMMIT_UNAVAILABLE",
                    "submit timeout cannot be durably committed",
                    {"stage": stage.stage},
                )
            if callable(transition):
                transition_kwargs: dict[str, Any] = {
                    "expected_submission_attempt": (
                        reservation.submission_attempt if reservation is not None else context.retry_attempt
                    ),
                    "expected_statuses": ("reserved",),
                    "require_unbound": True,
                }
                if _accepts_keyword(transition, "accepted_submit_contract_version"):
                    transition_kwargs["accepted_submit_contract_version"] = ACCEPTED_SUBMIT_CONTRACT_VERSION
                transition_result = transition(
                    pipeline_job_id,
                    AcceptedSubmitTransition.timeout(),
                    **transition_kwargs,
                )
                if not getattr(transition_result, "committed", False):
                    raise OrchestratorError(
                        "ACCEPTED_SUBMIT_TRANSITION_CONFLICT",
                        "submit timeout did not match the current durable reservation attempt",
                        {
                            "stage": stage.stage,
                            "transition_outcome": getattr(transition_result, "outcome", "unknown"),
                        },
                    )
            orchestrator.repository.insert_pipeline_event(
                entity_type="pipeline_job",
                entity_id=pipeline_job_id,
                event_type="submission_ambiguous",
                status_from="reserved",
                status_to="reserved",
                message=f"{stage.stage} submit result is ambiguous; exact-comment reconcile required.",
                details={
                    "stage": stage.stage,
                    "job_type": stage.job_type,
                    "submit_outcome": "submit_result_ambiguous",
                    "reconciliation_source": None,
                    "reconciliation_decision": None,
                    "matched_slurm_job_id": None,
                    "restart_stage": context.restart_stage or stage.stage,
                    "native_shud_resubmitted": is_forecast_cohort_stage(stage),
                },
            )
            return (
                StageRunResult(
                    stage=stage.stage,
                    job_type=stage.job_type,
                    pipeline_job_id=pipeline_job_id,
                    slurm_job_id="",
                    status="submit_result_ambiguous",
                    error_code=getattr(error, "error_code", None) or "SBATCH_SUBMIT_RESULT_AMBIGUOUS",
                    error_message="Submit result is ambiguous; exact-comment reconciliation pending.",
                    task_results=(),
                ),
                None,
            )
        accepted_submit_failure = bool(
            getattr(orchestrator.repository, "supports_accepted_submit_reconcile", False)
            and is_forecast_cohort_stage(stage)
        )
        rejection_batch_committed = False
        if accepted_submit_failure:
            expected_attempt = reservation.submission_attempt if reservation is not None else context.retry_attempt
            rejecter = getattr(orchestrator.repository, "reject_pipeline_job_submit_attempt", None)
            if callable(rejecter):
                reject_kwargs: dict[str, Any] = {
                    "expected_submission_attempt": expected_attempt,
                    "finished_at": deps.utcnow(),
                    "error_code": getattr(error, "error_code", None) or "SBATCH_SUBMISSION_FAILED",
                    "error_message": str(deps.redact_payload(str(error))),
                    "stage": stage.stage,
                    "job_type": stage.job_type,
                }
                if _accepts_keyword(rejecter, "pipeline_job_id"):
                    reject_kwargs["pipeline_job_id"] = pipeline_job_id
                transition_result = rejecter(
                    idempotency_key,
                    **reject_kwargs,
                )
                rejection_batch_committed = getattr(transition_result, "committed", False)
            else:
                raise OrchestratorError(
                    "ACCEPTED_SUBMIT_COMMIT_UNAVAILABLE",
                    "submit rejection requires the dedicated durable transition API",
                    {"stage": stage.stage},
                )
            if not getattr(transition_result, "committed", False):
                raise OrchestratorError(
                    "ACCEPTED_SUBMIT_TRANSITION_CONFLICT",
                    "submit rejection did not match the current durable reservation attempt",
                    {
                        "stage": stage.stage,
                        "transition_outcome": getattr(transition_result, "outcome", "unknown"),
                    },
                )
        result = orchestrator._record_submission_failure(
            stage,
            context,
            error,
            pipeline_job_id=pipeline_job_id,
            persist_pipeline_job=not accepted_submit_failure,
            persist_pipeline_event=not rejection_batch_committed,
        )
        if stage.stage == "forecast" and not rejection_batch_committed:
            orchestrator._mark_staged_hydro_runs_failed(
                [str(basin["run_id"]) for basin in context.active_basins if basin.get("run_id")],
                error_code=result.error_code or "SBATCH_SUBMISSION_FAILED",
                error_message=result.error_message or str(error),
            )
        return result, None

    slurm_job_id = str(submitted["job_id"])
    log_publication = orchestrator._display_log_publication_for_stage(
        source_id=context.source_id,
        cycle_time=context.cycle_time,
        run_id=context.run_id,
        job_id=pipeline_job_id,
        stage=stage.stage,
    )
    submitted_status = deps.status_from_gateway_job(submitted)
    submitted_log_uri = log_publication.advertised_uri
    submitted_array_task_id = deps.coerce_array_task_id(submitted.get("array_task_id"))
    accepted_submit_reconcile = bool(
        getattr(orchestrator.repository, "supports_accepted_submit_reconcile", False)
        and is_forecast_cohort_stage(stage)
    )
    if accepted_submit_reconcile:
        committer = getattr(orchestrator.repository, "commit_pipeline_job_submit_attempt", None)
        if not callable(committer) or reservation is None:
            raise OrchestratorError(
                "ACCEPTED_SUBMIT_COMMIT_UNAVAILABLE",
                "accepted submit cannot be durably committed to its reservation attempt",
                {"stage": stage.stage},
            )
        persisted_submitted_status = (
            "submitted" if submitted_status in deps.terminal_job_statuses else submitted_status
        )
        commit_kwargs: dict[str, Any] = {
            "expected_submission_attempt": reservation.submission_attempt,
            "slurm_job_id": slurm_job_id,
            "transition": AcceptedSubmitTransition.accepted(status=persisted_submitted_status),
            "array_task_id": submitted_array_task_id,
            "submitted_at": deps.parse_gateway_time(submitted.get("submitted_at")) or deps.utcnow(),
            "started_at": deps.parse_gateway_time(submitted.get("started_at")),
            "finished_at": (
                None
                if submitted_status in deps.terminal_job_statuses
                else deps.parse_gateway_time(submitted.get("finished_at"))
            ),
            "exit_code": None if submitted_status in deps.terminal_job_statuses else submitted.get("exit_code"),
            "error_code": None if submitted_status in deps.terminal_job_statuses else submitted.get("error_code"),
            "error_message": (
                None if submitted_status in deps.terminal_job_statuses else submitted.get("error_message")
            ),
            "log_uri": submitted_log_uri,
        }
        if _accepts_keyword(committer, "pipeline_job_id"):
            commit_kwargs["pipeline_job_id"] = pipeline_job_id
        commit_result = committer(
            idempotency_key,
            **commit_kwargs,
        )
        if not getattr(commit_result, "committed", False):
            raise OrchestratorError(
                "ACCEPTED_SUBMIT_COMMIT_CONFLICT",
                "accepted submit did not match the current durable reservation attempt",
                {"stage": stage.stage, "commit_outcome": getattr(commit_result, "outcome", "unknown")},
            )
    else:
        orchestrator._bind_cycle_stage_reservation(
            idempotency_key,
            slurm_job_id=slurm_job_id,
            array_task_id=submitted_array_task_id,
        )
    submitted_publish_attempt: DisplayLogPublicationAttempt | None = None
    if submitted_status in deps.terminal_job_statuses:
        submitted_publish_attempt = orchestrator._try_publish_log_for_advertise(slurm_job_id, log_publication)
        submitted_log_uri = submitted_publish_attempt.advertised_uri
        if submitted_log_uri:
            submitted["log_uri"] = submitted_log_uri
    submitted_manifest = submitted.get("manifest") if isinstance(submitted.get("manifest"), Mapping) else {}
    submitted_manifest_index_path = (
        str(submitted_manifest.get("manifest_index_path") or submitted.get("manifest_index_path") or "")
        if isinstance(submitted_manifest, Mapping)
        else str(submitted.get("manifest_index_path") or "")
    )
    actual_manifest_index_path = submitted_manifest_index_path or (
        str(manifest_index_path) if manifest_index_path else ""
    )
    if not accepted_submit_reconcile:
        orchestrator.repository.upsert_pipeline_job(
            {
                "job_id": pipeline_job_id,
                "run_id": context.run_id,
                "cycle_id": context.cycle_id,
                "job_type": stage.job_type,
                "slurm_job_id": slurm_job_id,
                "array_task_id": submitted_array_task_id,
                "model_id": accepted_submit_pipeline_job_model_id(
                    supports_accepted_submit_reconcile=getattr(
                        orchestrator.repository, "supports_accepted_submit_reconcile", False
                    ),
                    stage=stage.stage,
                    job_type=stage.job_type,
                    model_id=deps.cycle_pipeline_job_model_id(context),
                ),
                "status": submitted_status,
                "stage": stage.stage,
                "idempotency_key": idempotency_key,
                "submitted_at": deps.parse_gateway_time(submitted.get("submitted_at")) or deps.utcnow(),
                "started_at": deps.parse_gateway_time(submitted.get("started_at")),
                "finished_at": deps.parse_gateway_time(submitted.get("finished_at")),
                "exit_code": submitted.get("exit_code"),
                "error_code": submitted.get("error_code"),
                "error_message": submitted.get("error_message"),
                "log_uri": submitted_log_uri,
            }
        )
    submission_event_status = (
        "submitted"
        if accepted_submit_reconcile and submitted_status in deps.terminal_job_statuses
        else submitted_status
    )
    orchestrator.repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=pipeline_job_id,
        event_type="submission",
        status_from=None,
        status_to=submission_event_status,
        message=f"{stage.stage} submitted as Slurm job {slurm_job_id}",
        details=deps.safe_pipeline_event_details(
            {
                "stage": stage.stage,
                "job_type": stage.job_type,
                "slurm_job_id": slurm_job_id,
                "slurm": {
                    "job_id": slurm_job_id,
                    "state": submitted_status,
                    "array_task_id": submitted_array_task_id,
                    "exit_code": submitted.get("exit_code"),
                    "log_uri": submitted_log_uri,
                },
                "manifest_index_path": actual_manifest_index_path or None,
                "runtime_root_contract": deps.submission_runtime_root_contract(stage_manifest),
            }
        ),
    )
    if submitted_status in deps.terminal_job_statuses:
        terminal_observation = TerminalJobObservation(
            job=submitted,
            publication_attempt=submitted_publish_attempt,
        )
    else:
        terminal_observation = _call_orchestrator_helper(
            orchestrator,
            "_poll_cycle_stage_until_terminal",
            stage=stage,
            context=context,
            pipeline_job_id=pipeline_job_id,
            initial_job=submitted,
            initial_status=submitted_status,
            log_publication=log_publication,
        )
    terminal = terminal_observation.job
    publication_attempt = terminal_observation.publication_attempt
    log_uri = str(terminal.get("log_uri") or "")
    if not log_uri:
        if publication_attempt is None:
            publication_attempt = orchestrator._try_publish_log_for_advertise(slurm_job_id, log_publication)
        log_uri = str(publication_attempt.advertised_uri or "")

    poll_timed_out = isinstance(terminal, dict) and terminal.get("error_code") == "SLURM_JOB_TIMEOUT"
    aggregation = (
        orchestrator._aggregate_array_stage(stage, context, slurm_job_id, terminal, pipeline_job_id)
        if stage.is_array and not poll_timed_out
        else None
    )
    result_status = aggregation.status if aggregation is not None else deps.status_from_gateway_job(terminal)
    result_error_code = deps.aggregation_error_code(aggregation) if aggregation is not None else terminal.get(
        "error_code"
    )
    result_error_message = (
        deps.aggregation_error_message(aggregation) if aggregation is not None else terminal.get("error_message")
    )
    if aggregation is not None:
        durable_outcome = orchestrator._record_cycle_stage_status_override(
            stage,
            context,
            pipeline_job_id,
            terminal,
            aggregation,
            log_uri or None,
        )
        result_status = str(durable_outcome.get("status") or result_status)
        result_error_code = durable_outcome.get("error_code")
        result_error_message = durable_outcome.get("error_message")
    else:
        orchestrator._record_cycle_stage_accounting_event(stage, context, pipeline_job_id, terminal, log_uri=log_uri)

    orchestrator._after_cycle_stage_terminal(stage, context, result_status, terminal, aggregation)
    orchestrator._raise_publish_error_after_durable_update(publication_attempt)
    return (
        StageRunResult(
            stage=stage.stage,
            job_type=stage.job_type,
            pipeline_job_id=pipeline_job_id,
            slurm_job_id=slurm_job_id,
            status=result_status,
            exit_code=terminal.get("exit_code"),
            error_code=result_error_code,
            error_message=result_error_message,
            log_uri=log_uri,
            accounting=deps.slurm_accounting_from_payload(terminal),
            task_results=deps.stage_task_result_evidence(aggregation, context=context),
            finished_at=deps.parse_gateway_time(terminal.get("finished_at")),
        ),
        aggregation,
    )


def run_local_publish_stage(
    orchestrator: StageExecutionOrchestrator,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    *,
    pipeline_job_id: str,
    deps: StageExecutionDependencies | None = None,
) -> StageRunResult:
    """Publish display artifacts on the control node.

    Compute nodes can write the shared object-store, but the display mount
    under ``NHMS_PUBLISHED_ARTIFACT_ROOT`` is node-22-local. Keep all heavy
    SHUD work on Slurm, then mirror publish artifacts from object-store to
    the display root here where the mount is writable.
    """

    deps = _dependencies(orchestrator, deps)
    now = deps.utcnow()
    log_publication = orchestrator._display_log_publication_for_stage(
        source_id=context.source_id,
        cycle_time=context.cycle_time,
        run_id=context.run_id,
        job_id=pipeline_job_id,
        stage=stage.stage,
    )
    orchestrator.repository.upsert_pipeline_job(
        {
            "job_id": pipeline_job_id,
            "run_id": context.run_id,
            "cycle_id": context.cycle_id,
            "job_type": stage.job_type,
            "slurm_job_id": "local",
            "array_task_id": None,
            "model_id": deps.cycle_pipeline_job_model_id(context),
            "status": "running",
            "stage": stage.stage,
            "idempotency_key": deps.cycle_stage_idempotency_key(context, stage, pipeline_job_id=pipeline_job_id),
            "submitted_at": now,
            "started_at": now,
            "finished_at": None,
            "exit_code": None,
            "error_code": None,
            "error_message": None,
            "log_uri": None,
        }
    )
    orchestrator.repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=pipeline_job_id,
        event_type="submission",
        status_from=None,
        status_to="running",
        message=f"{stage.stage} running locally on the control node.",
        details=deps.safe_pipeline_event_details(
            {"stage": stage.stage, "job_type": stage.job_type, "execution": "control_node"}
        ),
    )

    try:
        publisher = deps.tile_publisher_cls(
            workspace_root=orchestrator.config.workspace_root,
            object_store_root=orchestrator.config.object_store_root,
            object_store_prefix=orchestrator.config.object_store_prefix,
            database_url=os.getenv("DATABASE_URL", ""),
            published_artifact_root=os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT", ""),
            published_artifact_uri_prefix=os.getenv("NHMS_PUBLISHED_ARTIFACT_URI_PREFIX", "published://"),
            object_store_copyback_root=os.getenv("NHMS_OBJECT_STORE_COPYBACK_ROOT", ""),
        )
        payload = publisher.publish_cycle(context.cycle_id).to_dict()
        status = "succeeded"
        exit_code = 0
        error_code = None
        error_message = None
    except deps.publish_error_cls as error:
        payload = deps.failure_payload(context.cycle_id, error)
        status = "failed"
        exit_code = 1
        error_code = error.error_code
        error_message = error.message
    except (OSError, RuntimeError, ValueError) as error:
        safe_message = str(deps.redact_payload(str(error)))
        payload = {
            "cycle_id": context.cycle_id,
            "status": "failed_publish",
            "error_code": "PUBLISH_TILES_FAILED",
            "error_message": safe_message,
            "layers": [],
        }
        status = "failed"
        exit_code = 1
        error_code = "PUBLISH_TILES_FAILED"
        error_message = safe_message

    log_uri = orchestrator._write_local_stage_log(log_publication.candidate_uri, payload)
    finished_at = deps.utcnow()
    previous_status, _record = orchestrator.repository.update_pipeline_job_status(
        pipeline_job_id,
        status,
        finished_at=finished_at,
        exit_code=exit_code,
        error_code=error_code,
        error_message=error_message,
        log_uri=log_uri,
    )
    terminal = {
        "job_id": "local",
        "status": status,
        "exit_code": exit_code,
        "error_code": error_code,
        "error_message": error_message,
        "log_uri": log_uri,
        "started_at": deps.format_time(now),
        "finished_at": deps.format_time(finished_at),
        "accounting": {"execution": "control_node"},
    }
    orchestrator.repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=pipeline_job_id,
        event_type="status_change",
        status_from=previous_status or "running",
        status_to=status,
        message=f"{stage.stage} completed locally with status {status}.",
        details=deps.safe_pipeline_event_details(
            {
                "stage": stage.stage,
                "job_type": stage.job_type,
                "execution": "control_node",
                "log_uri": log_uri,
                "result": payload,
            }
        ),
    )
    orchestrator._after_cycle_stage_terminal(stage, context, status, terminal, None)
    return StageRunResult(
        stage=stage.stage,
        job_type=stage.job_type,
        pipeline_job_id=pipeline_job_id,
        slurm_job_id="local",
        status=status,
        exit_code=exit_code,
        error_code=error_code,
        error_message=error_message,
        log_uri=log_uri,
        accounting={"execution": "control_node"},
        task_results=(),
        finished_at=finished_at,
    )


def resume_cycle_stage(
    orchestrator: StageExecutionOrchestrator,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    job: dict[str, Any],
    *,
    deps: StageExecutionDependencies | None = None,
) -> tuple[StageRunResult, ArrayAggregation | None]:
    deps = _dependencies(orchestrator, deps)
    status = str(job.get("status"))
    terminal = dict(job)
    deferred_publish_attempt: DisplayLogPublicationAttempt | None = None
    accepted_submit_projection = bool(
        getattr(orchestrator.repository, "supports_accepted_submit_reconcile", False)
        and is_forecast_cohort_stage(stage)
    )
    if status not in deps.terminal_job_statuses and job.get("slurm_job_id"):
        terminal_observation = _call_orchestrator_helper(
            orchestrator,
            "_poll_cycle_stage_until_terminal",
            stage=stage,
            context=context,
            pipeline_job_id=str(job["job_id"]),
            initial_job={"job_id": job["slurm_job_id"], "status": status},
            initial_status=status,
            log_publication=orchestrator._display_log_publication_for_pipeline_job(job),
        )
        terminal = terminal_observation.job
        deferred_publish_attempt = terminal_observation.publication_attempt
        status = deps.status_from_gateway_job(terminal)

    aggregation = None
    if (
        stage.is_array
        and job.get("slurm_job_id")
        and status != "reconcile_unverified"
        and (
            status
            not in {
                "failed",
                "cancelled",
                "submission_failed",
                "permanently_failed",
            }
            or (
                getattr(orchestrator.repository, "supports_accepted_submit_reconcile", False)
                and is_forecast_cohort_stage(stage)
            )
        )
    ):
        aggregation = orchestrator._aggregate_array_stage(
            stage,
            context,
            str(job["slurm_job_id"]),
            terminal,
            str(job["job_id"]),
        )
        status = aggregation.status
        if (
            accepted_submit_projection
            or str(job.get("status")) not in deps.terminal_job_statuses
            or status != str(job.get("status"))
        ):
            publication = orchestrator._display_log_publication_for_pipeline_job(job)
            publication_attempt: DisplayLogPublicationAttempt | None = None
            if publication is not None:
                publication_attempt = orchestrator._try_publish_log_for_advertise(str(job["slurm_job_id"]), publication)
                log_uri = publication_attempt.advertised_uri
            elif not deps.published_artifact_root_configured():
                legacy_log_uri = orchestrator.object_store.uri_for_key(f"runs/{context.run_id}/logs/{stage.stage}.log")
                publication_attempt = orchestrator._try_publish_log_for_advertise(
                    str(job["slurm_job_id"]),
                    DisplayLogPublication(
                        candidate_uri=legacy_log_uri,
                        advertised_uri=legacy_log_uri,
                        should_persist_logs=True,
                    ),
                )
                log_uri = publication_attempt.advertised_uri
            else:
                raise OrchestratorError(
                    "PUBLISHED_LOG_URI_UNAVAILABLE",
                    "Cannot compute a published log URI for the recovered pipeline job.",
                    {"job_id": str(job["job_id"]), "stage": stage.stage},
                )
            durable_outcome = orchestrator._record_cycle_stage_status_override(
                stage,
                context,
                str(job["job_id"]),
                terminal,
                aggregation,
                log_uri,
            )
            status = str(durable_outcome.get("status") or status)
            if publication_attempt is not None:
                deferred_publish_attempt = publication_attempt
        if status == "partially_failed":
            context.had_partial = True
            context.last_partial_status = orchestrator._partial_cycle_status(stage)

    result_log_uri = str(terminal.get("log_uri") or job.get("log_uri") or "") or None
    get_pipeline_job = getattr(orchestrator.repository, "get_pipeline_job", None)
    updated_job = get_pipeline_job(str(job["job_id"])) if callable(get_pipeline_job) else None
    if updated_job is not None:
        result_log_uri = str(updated_job.get("log_uri") or "") or None
        if accepted_submit_projection:
            status = str(updated_job.get("status") or status)
    effective_job = updated_job if accepted_submit_projection and updated_job is not None else job

    orchestrator._after_cycle_stage_terminal(stage, context, status, terminal, aggregation)
    orchestrator._raise_publish_error_after_durable_update(deferred_publish_attempt)
    return (
        StageRunResult(
            stage=stage.stage,
            job_type=stage.job_type,
            pipeline_job_id=str(job["job_id"]),
            slurm_job_id=str(job.get("slurm_job_id") or ""),
            status=status,
            exit_code=effective_job.get("exit_code"),
            error_code=effective_job.get("error_code"),
            error_message=effective_job.get("error_message"),
            log_uri=result_log_uri,
            accounting=deps.slurm_accounting_from_payload(terminal),
            task_results=deps.stage_task_result_evidence(aggregation, context=context),
            finished_at=deps.parse_gateway_time(
                (effective_job or {}).get("finished_at")
                or terminal.get("finished_at")
                or job.get("finished_at")
            ),
        ),
        aggregation,
    )


def poll_cycle_stage_until_terminal(
    orchestrator: StageExecutionOrchestrator,
    *,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    pipeline_job_id: str,
    initial_job: dict[str, Any],
    initial_status: str,
    log_publication: DisplayLogPublication | None,
    deps: StageExecutionDependencies | None = None,
) -> TerminalJobObservation:
    deps = _dependencies(orchestrator, deps)
    job = dict(initial_job)
    current_status = initial_status
    deadline = time.monotonic() + orchestrator.config.job_timeout_seconds
    while deps.status_from_gateway_job(job) not in deps.terminal_job_statuses:
        if time.monotonic() >= deadline:
            return _call_orchestrator_helper(
                orchestrator,
                "_record_cycle_stage_poll_timeout",
                stage=stage,
                context=context,
                pipeline_job_id=pipeline_job_id,
                job=job,
                current_status=current_status,
                log_publication=log_publication,
            )
        time.sleep(orchestrator.config.poll_interval_seconds)
        job = deps.coerce_mapping(orchestrator.slurm_client.get_job_status(str(job["job_id"])))
        new_status = deps.status_from_gateway_job(job)
        if new_status == current_status:
            continue
        if stage.is_array and new_status in deps.terminal_job_statuses:
            current_status = new_status
            continue
        log_uri = log_publication.advertised_uri if log_publication is not None else None
        publication_attempt: DisplayLogPublicationAttempt | None = None
        if new_status in deps.terminal_job_statuses and log_publication is not None:
            publication_attempt = orchestrator._try_publish_log_for_advertise(str(job["job_id"]), log_publication)
            log_uri = publication_attempt.advertised_uri
        previous_status, record = _update_runtime_pipeline_status(
            orchestrator,
            stage,
            pipeline_job_id,
            new_status,
            current_status=current_status,
            started_at=deps.parse_gateway_time(job.get("started_at")),
            finished_at=deps.parse_gateway_time(job.get("finished_at")),
            exit_code=job.get("exit_code"),
            error_code=job.get("error_code"),
            error_message=job.get("error_message"),
            log_uri=log_uri if new_status in deps.terminal_job_statuses else None,
        )
        if log_uri and new_status in deps.terminal_job_statuses:
            job["log_uri"] = log_uri
        persisted_status = str(record.get("status") or new_status)
        if persisted_status != new_status:
            job["status"] = persisted_status
            current_status = persisted_status
            if persisted_status in deps.terminal_job_statuses:
                return TerminalJobObservation(job=job, publication_attempt=publication_attempt)
            continue
        orchestrator.repository.insert_pipeline_event(
            entity_type="pipeline_job",
            entity_id=pipeline_job_id,
            event_type="status_change",
            status_from=previous_status or current_status,
            status_to=new_status,
            message=deps.stage_status_message(stage.stage, new_status, job),
            details=deps.safe_pipeline_event_details(
                {
                    "stage": stage.stage,
                    "job_type": stage.job_type,
                    "slurm_job_id": job["job_id"],
                    "exit_code": job.get("exit_code"),
                    "error_code": job.get("error_code"),
                    "slurm": {
                        "job_id": job["job_id"],
                        "state": job.get("state") or job.get("status"),
                        "exit_code": job.get("exit_code"),
                        "log_uri": log_uri if new_status in deps.terminal_job_statuses else None,
                        "accounting": deps.slurm_accounting_from_payload(job),
                        "resource_metrics": deps.resource_metrics_from_payload(job),
                    },
                }
            ),
        )
        current_status = new_status
        if publication_attempt is not None and publication_attempt.error is not None:
            return TerminalJobObservation(job=job, publication_attempt=publication_attempt)
    return TerminalJobObservation(job=job)


def record_cycle_stage_poll_timeout(
    orchestrator: StageExecutionOrchestrator,
    *,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    pipeline_job_id: str,
    job: dict[str, Any],
    current_status: str,
    log_publication: DisplayLogPublication | None,
    deps: StageExecutionDependencies | None = None,
) -> TerminalJobObservation:
    deps = _dependencies(orchestrator, deps)
    message = f"Stage {stage.stage} did not reach a terminal status before timeout."
    terminal = dict(job)
    terminal.update(
        {
            "status": "failed",
            "finished_at": deps.format_time(deps.utcnow()),
            "error_code": "SLURM_JOB_TIMEOUT",
            "error_message": message,
        }
    )
    publication_attempt = (
        orchestrator._try_publish_log_for_advertise(str(job["job_id"]), log_publication)
        if log_publication is not None
        else None
    )
    log_uri = publication_attempt.advertised_uri if publication_attempt is not None else None
    accepted_submit_timeout = bool(
        getattr(orchestrator.repository, "supports_accepted_submit_reconcile", False)
        and is_forecast_cohort_stage(stage)
    )
    if accepted_submit_timeout:
        transition = getattr(
            orchestrator.repository,
            "transition_pipeline_job_runtime_status",
            None,
        )
        if not callable(transition):
            raise OrchestratorError(
                "ACCEPTED_SUBMIT_RUNTIME_TRANSITION_UNAVAILABLE",
                "forecast cohort timeout transition API is unavailable",
            )
        transition_kwargs: dict[str, Any] = {
            "expected_statuses": (current_status,),
            "finished_at": deps.utcnow(),
            "exit_code": terminal.get("exit_code"),
            "error_code": "SLURM_JOB_TIMEOUT",
            "error_message": message,
            "log_uri": log_uri,
        }
        transition_result = transition(
            pipeline_job_id,
            "reconcile_unverified",
            **transition_kwargs,
        )
        if not getattr(transition_result, "committed", False):
            raise OrchestratorError(
                "ACCEPTED_SUBMIT_RUNTIME_TRANSITION_CONFLICT",
                "forecast cohort timeout state no longer matches the observed status",
            )
        previous_status = current_status
        record = dict(getattr(transition_result, "row", None) or {})
    else:
        previous_status, record = orchestrator.repository.update_pipeline_job_status(
            pipeline_job_id,
            "failed",
            finished_at=deps.utcnow(),
            exit_code=terminal.get("exit_code"),
            error_code="SLURM_JOB_TIMEOUT",
            error_message=message,
            log_uri=log_uri,
        )
    terminal.update(record)
    event_type = "reconcile_unverified" if accepted_submit_timeout else "timeout"
    event_status = "reconcile_unverified" if accepted_submit_timeout else "failed"
    orchestrator.repository.insert_pipeline_event(
        entity_type="pipeline_job",
        entity_id=pipeline_job_id,
        event_type=event_type,
        status_from=previous_status or current_status,
        status_to=event_status,
        message=(
            "Slurm accounting is not terminal; exact accounting reconciliation is required."
            if accepted_submit_timeout
            else message
        ),
        details=deps.safe_pipeline_event_details(
            {
                "stage": stage.stage,
                "job_type": stage.job_type,
                "cycle_id": context.cycle_id,
                "slurm_job_id": job["job_id"],
                "timeout_seconds": orchestrator.config.job_timeout_seconds,
                "error_code": "SLURM_JOB_TIMEOUT",
            }
        ),
    )
    orchestrator._record_cycle_stage_accounting_gap(
        stage,
        context,
        pipeline_job_id,
        slurm_job_id=str(job["job_id"]),
        message="Slurm accounting did not reach a terminal state before timeout.",
        details={"timeout_seconds": orchestrator.config.job_timeout_seconds},
    )
    if not accepted_submit_timeout:
        orchestrator.repository.update_forecast_cycle_status(
            source_id=context.source_id,
            cycle_time=context.cycle_time,
            status=stage.failure_cycle_status,
            error_code="SLURM_JOB_TIMEOUT",
            error_message=message,
        )
    return TerminalJobObservation(job=terminal, publication_attempt=publication_attempt)


def submit_array_stage(
    orchestrator: StageExecutionOrchestrator,
    stage: StageDefinition,
    context: CycleOrchestrationContext,
    tasks: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    deps: StageExecutionDependencies | None = None,
) -> dict[str, Any]:
    deps = _dependencies(orchestrator, deps)
    submit_job_array = getattr(orchestrator.slurm_client, "submit_job_array", None)
    if callable(submit_job_array):
        # Carry the idempotency --comment into the array submission manifest
        # so the array master sbatch is stamped; real_backend.submit_job_array
        # reads ``manifest["comment"]`` and threads it to sbatch --comment,
        # making array-stage crash recovery reconcile-by-comment work.
        try:
            submission_manifest = _call_orchestrator_helper(
                orchestrator,
                "_slurm_submission_manifest",
                manifest,
            )
        except Exception as error:
            if getattr(error, "submit_disposition", None) is None:
                error.submit_disposition = SubmitDisposition.REJECTED
            raise
        if manifest.get("comment"):
            submission_manifest["comment"] = manifest["comment"]
        return deps.coerce_mapping(
            submit_job_array(
                stage.job_type,
                cycle_id=context.cycle_id,
                stage_name=stage.stage,
                tasks=tasks,
                manifest=submission_manifest,
            )
        )
    error = deps.make_slurm_client_error(
        "SLURM_ARRAY_SUBMIT_UNSUPPORTED",
        f"Slurm client does not support array submission for {stage.stage}.",
        {"stage": stage.stage, "job_type": stage.job_type, "cycle_id": context.cycle_id},
    )
    error.submit_disposition = SubmitDisposition.REJECTED
    raise error


def slurm_submission_manifest(
    orchestrator: StageExecutionOrchestrator,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    submission = dict(manifest)
    if orchestrator.config.slurm_job_type_templates:
        submission["slurm_job_type_templates"] = dict(orchestrator.config.slurm_job_type_templates)
    if orchestrator.config.slurm_env:
        submission["slurm_env"] = dict(orchestrator.config.slurm_env)
    return submission


__all__ = [
    "StageExecutionDependencies",
    "poll_cycle_stage_until_terminal",
    "record_cycle_stage_poll_timeout",
    "resume_cycle_stage",
    "run_local_publish_stage",
    "slurm_submission_manifest",
    "submit_and_wait_cycle_stage",
    "submit_array_stage",
]
