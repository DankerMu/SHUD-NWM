"""Forecast-cohort projection cluster of the file orchestration journal, as a repository mixin.

The accounting-pass writers for an accepted forecast-cohort master.
``project_forecast_cohort_tasks`` projects one terminal Slurm accounting pass
onto the master and its cohort member rows under one cycle write lock: it
verifies every array task against the durable cohort, appends the member
rows with their reconcile events, the hydro runs they settle and the
derived master in one batch, and materializes each touched model once.
``defer_forecast_cohort_projection`` and its locked half
``_defer_forecast_cohort_projection_unlocked`` fail one current projection
closed on ``reconcile_unverified`` without touching the cohort members; the
projection writer reuses the locked half for a mismatched master id and for
incomplete task accounting.  The lock, the row lookups, the journal appends and
the materialization are facade-class methods reached through ``self``.

The journal's error type and the ``PROJECTION_DERIVED_MASTER_STATUSES`` /
``TERMINAL_PIPELINE_STATUSES`` sets stay in the facade and are imported inside
the methods that read them.  The facade-resident patched clock is looked up on
the facade module at call time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from packages.common.contracts.time_normalization import format_cycle_time
from services.orchestrator.accepted_submit_identity import (
    ACCEPTED_PROJECTION_FIELDS,
    ACCEPTED_SUBMIT_CONTRACT_VERSION,
    INIT_STATE_IDENTITY_FIELD,
    MAX_FORECAST_COHORT_MEMBERS,
    OPERATOR_VERIFIED_ABSENCE_DECISION,
    SLURM_BINDING_SOURCE_FIELD,
    AcceptedSubmitCommitResult,
    AcceptedSubmitEvidenceError,
    AcceptedSubmitTransition,
    accepted_submit_contract_is_current,
    accepted_submit_row_kind,
    apply_accepted_submit_transition,
    init_state_identity_for_task,
    matched_bound_reconciliation_source,
    normalize_candidate_projections,
    ordered_cohort_members,
)
from services.orchestrator.file_journal_candidates import (
    _accepted_submit_source_cycle_from_job_id,
    _resolved_caller_evidence,
)
from services.orchestrator.file_journal_paths import _cycle_read_source_segments, _optional_safe_identity
from services.orchestrator.file_journal_read_attribution import _install_public_read_attribution
from services.orchestrator.file_journal_replay import _dedupe_events
from services.orchestrator.file_journal_retry_helpers import (
    _bounded_candidate_projections,
    _durable_error_message,
    _journal_record_for_write,
)
from services.orchestrator.retry_identity import (
    RETRY_ATTEMPT_FLOORS_FIELD,
    effective_retry_attempt,
    member_charged_retry_attempt,
)
from services.orchestrator.scheduler_state import _format_utc
from services.orchestrator.scheduler_state_types import ACTIVE_HYDRO_STATUSES


@_install_public_read_attribution
class _FileJournalCohortMixin:
    def project_forecast_cohort_tasks(
        self,
        job_id: str,
        *,
        master_slurm_job_id: str,
        projections: Sequence[Mapping[str, Any]],
        complete: bool,
        master_status: str,
        master_error_code: str | None,
        reconciliation_decision: str,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        master_error_message: str | None = None,
        log_uri: str | None = None,
    ) -> dict[str, int]:
        """Project one accounting pass under one cycle lock and one materialization/model."""

        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import (
            PROJECTION_DERIVED_MASTER_STATUSES,
            TERMINAL_PIPELINE_STATUSES,
            FileOrchestrationJournalError,
        )

        if type(master_status) is not str or master_status not in {
            "succeeded",
            "partially_failed",
            "failed",
            "cancelled",
        }:
            raise FileOrchestrationJournalError(
                "file_journal_evidence_enum_invalid", field="master_status"
            )
        # Writer-authority closed world (#1564 D8): the operator decision is
        # never a raw caller-supplied cohort-projection decision, on either the
        # bound or the mismatched-ID branch.  Refuse before any source parsing,
        # lock acquisition, mutation, or event.
        if reconciliation_decision == OPERATOR_VERIFIED_ABSENCE_DECISION:
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="reconciliation_decision",
            )
        del master_status, master_error_code
        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if (
                existing is None
                or not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return {"total": 0, "pipeline_status": 0, "pipeline_event": 0}
            if str(existing.get("slurm_job_id") or "") != master_slurm_job_id:
                result = self._defer_forecast_cohort_projection_unlocked(
                    existing,
                    reconciliation_decision="identity_mismatch_blocked",
                    reconciliation_reason_class=None,
                    error_code="SLURM_MASTER_IDENTITY_MISMATCH",
                    error_message="terminal Slurm master id did not match the durable accepted submit",
                    finished_at=finished_at,
                    exit_code=exit_code,
                    log_uri=log_uri,
                )
                return {
                    "total": 1 if result.wrote else 0,
                    "pipeline_status": 1 if result.wrote else 0,
                    "pipeline_event": 0,
                }
            if reconciliation_decision != "matched_bound":
                raise FileOrchestrationJournalError(
                    "file_journal_evidence_enum_invalid", field="reconciliation_decision"
                )
            existing_projections = {
                int(item["array_task_id"]): dict(item)
                for item in _bounded_candidate_projections(existing.get("candidate_projections"))
                if str(item.get("array_task_id", "")).isdigit()
            }
            if len(projections) > MAX_FORECAST_COHORT_MEMBERS:
                raise FileOrchestrationJournalError(
                    "file_journal_evidence_limit_exceeded", field="candidate_projections"
                )
            members = {
                int(member["array_task_id"]): dict(member)
                for member in ordered_cohort_members(existing.get("cohort_members"))
            }
            normalized_projections: list[dict[str, Any]] = []
            seen_task_ids: set[int] = set()
            for projection in projections:
                raw_task_id = projection.get("array_task_id")
                if type(raw_task_id) is not int or raw_task_id in seen_task_ids:
                    raise FileOrchestrationJournalError(
                        "file_journal_task_identity_mismatch",
                        field="array_task_id",
                    )
                seen_task_ids.add(raw_task_id)
                bounded = _bounded_candidate_projections([projection])
                try:
                    item = normalize_candidate_projections(
                        bounded,
                        cohort_members=existing.get("cohort_members"),
                    )[0]
                except (AcceptedSubmitEvidenceError, IndexError) as error:
                    raise FileOrchestrationJournalError(
                        "file_journal_task_identity_mismatch",
                        field=getattr(error, "field", "candidate_projections"),
                    ) from error
                outcome = item.get("array_task_outcome")
                task_slurm_job_id = projection.get("task_slurm_job_id")
                expected_task_slurm_job_id = f"{master_slurm_job_id}_{raw_task_id}"
                if outcome in {"succeeded", "failed"} and str(task_slurm_job_id or "") != expected_task_slurm_job_id:
                    raise FileOrchestrationJournalError(
                        "file_journal_task_identity_mismatch",
                        field="task_slurm_job_id",
                    )
                if task_slurm_job_id not in (None, "") and str(task_slurm_job_id) != expected_task_slurm_job_id:
                    raise FileOrchestrationJournalError(
                        "file_journal_task_identity_mismatch",
                        field="task_slurm_job_id",
                    )
                normalized_projections.append(
                    {
                        **item,
                        "task_slurm_job_id": task_slurm_job_id,
                        "error_code": projection.get("error_code"),
                    }
                )
            if seen_task_ids != set(members) or (
                complete
                and any(
                    item.get("array_task_outcome") not in {"succeeded", "failed"}
                    for item in normalized_projections
                )
            ):
                raise FileOrchestrationJournalError(
                    "file_journal_task_identity_mismatch",
                    field="candidate_projections",
                )
            if not complete:
                result = self._defer_forecast_cohort_projection_unlocked(
                    existing,
                    reconciliation_decision="accounting_unavailable",
                    reconciliation_reason_class="coverage_incomplete",
                    error_code="SLURM_TASK_ACCOUNTING_INCOMPLETE",
                    error_message="terminal Slurm array task accounting was incomplete",
                    finished_at=finished_at,
                    exit_code=exit_code,
                    log_uri=log_uri,
                )
                return {
                    "total": 1 if result.wrote else 0,
                    "pipeline_status": 1 if result.wrote else 0,
                    "pipeline_event": 0,
                }
            succeeded_tasks = sum(
                item.get("array_task_outcome") == "succeeded"
                for item in normalized_projections
            )
            if succeeded_tasks == len(normalized_projections):
                projected_master_status = "succeeded"
                projected_master_error_code = None
            elif succeeded_tasks == 0:
                projected_master_status = "failed"
                projected_master_error_code = next(
                    (
                        str(item["error_code"])
                        for item in normalized_projections
                        if item.get("error_code") not in (None, "")
                    ),
                    "SLURM_ARRAY_TASK_FAILED",
                )
            else:
                projected_master_status = "partially_failed"
                projected_master_error_code = next(
                    (
                        str(item["error_code"])
                        for item in normalized_projections
                        if item.get("error_code") not in (None, "")
                    ),
                    "SLURM_ARRAY_TASK_FAILED",
                )
            verified: list[dict[str, Any]] = []
            for projection in sorted(normalized_projections, key=lambda item: int(item["array_task_id"])):
                if projection.get("array_task_outcome") not in {"succeeded", "failed"}:
                    continue
                task_id = int(projection["array_task_id"])
                previous = existing_projections.get(task_id)
                if previous is not None and previous.get("array_task_outcome") in {"succeeded", "failed"}:
                    continue
                existing_projections[task_id] = {
                    key: projection.get(key)
                    for key in ACCEPTED_PROJECTION_FIELDS
                }
                verified.append(projection)

            payloads: list[tuple[str, dict[str, Any], str | None]] = []
            direct_jobs: list[dict[str, Any]] = []
            touched_models: set[str] = set()
            event_id = self._next_accepted_submit_event_id_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
            )
            model_rows = self._cycle_rows_by_model_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                model_ids=(
                    str(projection["model_id"])
                    for projection in verified
                    if projection.get("model_id") not in (None, "")
                ),
            )
            for projection in verified:
                run_id = str(projection.get("run_id") or "")
                model_id = str(projection.get("model_id") or "")
                candidate_id = str(projection.get("candidate_id") or "")
                task_id = int(projection["array_task_id"])
                if not run_id or not model_id or not candidate_id:
                    continue
                task_status = str(projection["array_task_outcome"])
                task_status = "succeeded" if task_status == "succeeded" else "failed"
                candidate_job_id = f"job_{run_id}_forecast_reconciled_{master_slurm_job_id}_{task_id}"
                # Forward accounting (#1183): the master row captured every
                # task's warm-start identity at reservation time, when the
                # planning context still existed.  This accounting pass owns no
                # planning context, so it copies this task's entry verbatim;
                # pre-change masters recorded none and yield an empty map.
                task_init_state_identity = init_state_identity_for_task(
                    existing.get(INIT_STATE_IDENTITY_FIELD),
                    task_id,
                )
                candidate_job = self._pipeline_job_row(
                    {
                        "job_id": candidate_job_id,
                        "run_id": run_id,
                        "cycle_id": existing["cycle_id"],
                        "job_type": "run_shud_forecast_array",
                        "slurm_job_id": str(projection.get("task_slurm_job_id") or f"{master_slurm_job_id}_{task_id}"),
                        "array_task_id": task_id,
                        "model_id": model_id,
                        "status": task_status,
                        "stage": "forecast",
                        "candidate_id": candidate_id,
                        "error_code": None if task_status == "succeeded" else projection.get("error_code"),
                        "submit_outcome": "accepted",
                        "accepted_submit_contract_version": ACCEPTED_SUBMIT_CONTRACT_VERSION,
                        INIT_STATE_IDENTITY_FIELD: (
                            [task_init_state_identity] if task_init_state_identity is not None else []
                        ),
                        "restart_stage": projection.get("restart_stage"),
                        "native_shud_resubmitted": False,
                        # #2404: the member's charged forecast attempt.  The
                        # model-less master proves no candidate authority (#1586)
                        # and this id carries no ``_retry_<n>`` suffix, so without
                        # it a cohort member's stage budget never advanced.
                        # #2542: charged from the member's own recorded floor, not
                        # the master's (shared) attempt when floors were recorded.
                        "retry_count": member_charged_retry_attempt(
                            existing.get(RETRY_ATTEMPT_FLOORS_FIELD),
                            model_id,
                            effective_retry_attempt(existing.get("job_id"), existing.get("retry_count")),
                        ),
                    }
                )
                payloads.append(("pipeline_job", candidate_job, model_id))
                direct_jobs.append(candidate_job)
                event = {
                    "event_id": event_id,
                    "entity_type": "pipeline_job",
                    "entity_id": candidate_job_id,
                    "event_type": "array_task_reconciled",
                    "status_from": "reconciling",
                    "status_to": task_status,
                    "message": None,
                    "details": _bounded_candidate_projections([projection])[0],
                    "created_at": _format_utc(file_orchestration_journal._utcnow()),
                }
                event_id += 1
                payloads.append(("pipeline_event", event, model_id))
                model_snapshot = model_rows.get(model_id)
                hydro = model_snapshot.hydro_run if model_snapshot is not None else None
                if isinstance(hydro, Mapping) and str(hydro.get("run_id") or "") != run_id:
                    hydro = None
                hydro_status = str(hydro.get("status") or "") if isinstance(hydro, Mapping) else ""
                hydro_error_code = hydro.get("error_code") if isinstance(hydro, Mapping) else None
                hydro_is_retryable = (
                    hydro_status in ACTIVE_HYDRO_STATUSES
                    or (
                        hydro_status == "failed"
                        and hydro_error_code in {"SLURM_GATEWAY_UNAVAILABLE", "SLURM_RESERVATION_LOST"}
                    )
                )
                if (
                    task_status == "succeeded"
                    and isinstance(hydro, Mapping)
                    and hydro_is_retryable
                    and hydro_error_code in {None, "SLURM_GATEWAY_UNAVAILABLE", "SLURM_RESERVATION_LOST"}
                ):
                    hydro_row = dict(hydro)
                    hydro_row.update(
                        {
                            "status": "succeeded",
                            "slurm_job_id": candidate_job["slurm_job_id"],
                            "error_code": None,
                            "error_message": None,
                            "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                        }
                    )
                    payloads.append(("hydro_run", hydro_row, model_id))
                elif (
                    task_status == "failed"
                    and isinstance(hydro, Mapping)
                    and hydro_is_retryable
                    and hydro_error_code in {None, "SLURM_GATEWAY_UNAVAILABLE", "SLURM_RESERVATION_LOST"}
                ):
                    hydro_row = dict(hydro)
                    hydro_row.update(
                        {
                            "status": "failed",
                            "slurm_job_id": candidate_job["slurm_job_id"],
                            "error_code": projection.get("error_code") or "SLURM_JOB_FAILED",
                            "error_message": "terminal Slurm array task failed",
                            "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                        }
                    )
                    payloads.append(("hydro_run", hydro_row, model_id))
                touched_models.add(model_id)

            # Terminal stickiness (#1312, widened by #1589/#1629): a master
            # whose persisted status the projection cannot derive keeps that
            # status, because the projection owns exactly
            # ``PROJECTION_DERIVED_MASTER_STATUSES`` and must not overwrite a
            # terminal truth it has no authority to produce.  Under the routed
            # domain that means ``permanently_failed`` and ``cancelled``.  The
            # attribution family sticks ONLY for ``permanently_failed``: a row
            # saying "permanently failed" while its error code is recomputed by
            # every pass is self-contradictory.  A ``cancelled`` row gets status
            # stickiness only -- it still takes the current task-derived error
            # family and refreshes ``candidate_projections`` plus the
            # observational family (``finished_at`` / ``exit_code`` /
            # ``log_uri``), because status preservation must not become a
            # whole-row freeze.  Refreshing observational evidence under the
            # mark is intended behaviour and contradicts nothing, so it is
            # deliberately NOT frozen (the defer path's whole-row short circuit
            # is the rejected alternative).
            #
            # The trigger is "terminal status outside the derived domain":
            # widening to the derived values would pin the first pass's
            # conclusion forever, i.e. disable the projection, and widening to
            # every non-derived status (including live ``reserved``/``submitted``
            # rows) would freeze a bound master before the projection could ever
            # derive its terminal outcome.  ``submission_failed`` and
            # ``reservation_lost`` are terminal yet still rejected before this
            # projection by their submit-outcome/inventory gates, so they are
            # never rewritten through a ``matched_bound`` accounting tuple; the
            # terminal-membership predicate never sees them as ``existing``.
            persisted_status = str(existing.get("status") or "")
            # The derived domain is the *only* set this projection can overwrite:
            # sticky means "the projection cannot derive this status", and the
            # projection owns no non-terminal status (a ``reserved``/``submitted``
            # bound master still derives its terminal outcome here).  Requiring
            # terminal membership keeps the predicate from pinning a live row.
            master_is_sticky = (
                persisted_status in TERMINAL_PIPELINE_STATUSES
                and persisted_status not in PROJECTION_DERIVED_MASTER_STATUSES
            )
            master_is_permanently_failed = persisted_status == "permanently_failed"
            sticky_master_status = persisted_status if master_is_sticky else projected_master_status
            cohort_row = apply_accepted_submit_transition(
                existing,
                AcceptedSubmitTransition.accounting(
                    reconciliation_decision,
                    submit_outcome="accepted",
                    matched_slurm_job_id=master_slurm_job_id,
                    status=sticky_master_status,
                    # #1850 Fix B: a complete matched-bound terminal projection
                    # derives the legal current source from immutable binding
                    # provenance instead of the exact-comment factory default,
                    # so a name-window fallback-bound row keeps/restores its
                    # name-window source. Binding provenance itself is carried
                    # by ``apply_accepted_submit_transition`` (the transition
                    # carries no new binding fields, so the prior values
                    # survive).
                    reconciliation_source=matched_bound_reconciliation_source(
                        existing.get(SLURM_BINDING_SOURCE_FIELD)
                    ),
                ),
            )
            cohort_row.update(
                {
                    "candidate_projections": [
                        existing_projections[task_id] for task_id in sorted(existing_projections)
                    ],
                    "error_code": (
                        existing.get("error_code")
                        if master_is_permanently_failed
                        else projected_master_error_code
                    ),
                    "updated_at": _format_utc(file_orchestration_journal._utcnow()),
                }
            )
            # #1589 (design D3): a display placeholder is a withheld value, not
            # an instruction to overwrite.  Resolving the caller's evidence
            # restores what these ``is not None`` guards were meant to ask --
            # "did the caller supply a real value?" -- so a round-tripped public
            # row can no longer displace a real durable value that the write
            # boundary would then withhold, losing it outright.  Resolved here,
            # above the guards, which also puts it above the ``cohort_changed``
            # comparison below.
            finished_at = _resolved_caller_evidence(finished_at)
            exit_code = _resolved_caller_evidence(exit_code)
            master_error_message = _resolved_caller_evidence(master_error_message)
            log_uri = _resolved_caller_evidence(log_uri)
            if finished_at is not None:
                cohort_row["finished_at"] = _format_utc(finished_at)
            if exit_code is not None:
                cohort_row["exit_code"] = exit_code
            if master_error_message is not None and not master_is_permanently_failed:
                cohort_row["error_message"] = _durable_error_message(master_error_message)
            if log_uri is not None:
                cohort_row["log_uri"] = log_uri
            cohort_changed = any(
                cohort_row.get(key) != existing.get(key)
                for key in (
                    "candidate_projections",
                    "status",
                    "error_code",
                    "reconciliation_source",
                    "reconciliation_decision",
                    "matched_slurm_job_id",
                    "finished_at",
                    "exit_code",
                    "error_message",
                    "log_uri",
                )
            )
            if cohort_changed:
                payloads.append(("pipeline_job", cohort_row, _optional_safe_identity(cohort_row, "model_id")))
                payloads.append(
                    (
                        "pipeline_event",
                        {
                            "event_id": event_id,
                            "entity_type": "pipeline_job",
                            "entity_id": str(cohort_row["job_id"]),
                            "event_type": "status_change",
                            "status_from": str(existing.get("status") or "") or None,
                            "status_to": str(cohort_row.get("status") or "") or None,
                            "message": "forecast cohort terminal projection committed",
                            "details": {
                                "slurm_job_id": master_slurm_job_id,
                                "projection_complete": complete,
                                "projected_task_count": len(existing_projections),
                            },
                            "created_at": _format_utc(file_orchestration_journal._utcnow()),
                        },
                        None,
                    )
                )
            if not payloads:
                return {"total": 0, "pipeline_status": 0, "pipeline_event": 0}

            next_sequence = self._next_sequence_unlocked(source_id=source_id, cycle_time=cycle_time)
            records: list[dict[str, Any]] = []
            for offset, (record_type, payload, model_id) in enumerate(payloads):
                record = _journal_record_for_write(
                    record_type,
                    payload,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    sequence=next_sequence + offset,
                )
                self._validate_outgoing_record(
                    record,
                    source_id=source_id,
                    cycle_time=cycle_time,
                    record_type=record_type,
                    model_id=model_id,
                )
                records.append(record)
            self._append_journal_records_unlocked(
                source_id=source_id,
                cycle_time=cycle_time,
                records=records,
            )
            materialization_next_sequence = next_sequence + len(records)
            self._apply_records_to_model_rows(
                model_rows,
                records,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            source_segments = _cycle_read_source_segments(
                source_id=source_id,
                source_segment_override=None,
                root=self.root,
            )
            cycle_segment = format_cycle_time(cycle_time)
            for model_id, rows in model_rows.items():
                rows.pipeline_events = _dedupe_events(rows.pipeline_events)
                self._cache_cycle_rows(
                    (source_id, cycle_segment, model_id, source_segments),
                    rows,
                    fingerprint=None,
                )
            pipeline_records = [record for record in records if str(record.get("record_type") or "") == "pipeline_job"]
            for record, direct_job in zip(pipeline_records[: len(direct_jobs)], direct_jobs, strict=True):
                self._write_pipeline_job_direct_unlocked(direct_job, record)
            if cohort_changed:
                self._write_pipeline_job_direct_unlocked(cohort_row, pipeline_records[-1])
            for model_id in sorted(touched_models):
                self._materialize_latest_unlocked(
                    source_id=source_id,
                    cycle_time=cycle_time,
                    model_id=model_id,
                    next_sequence=materialization_next_sequence,
                )
            event_writes = sum(record_type == "pipeline_event" for record_type, _payload, _model in payloads)
            return {
                "total": len(records),
                "pipeline_status": len(records) - event_writes,
                "pipeline_event": event_writes,
            }

    def defer_forecast_cohort_projection(
        self,
        job_id: str,
        *,
        reconciliation_decision: str,
        reconciliation_reason_class: str | None,
        error_code: str,
        error_message: str,
        finished_at: datetime | None = None,
        exit_code: int | None = None,
        log_uri: str | None = None,
    ) -> AcceptedSubmitCommitResult:
        """Fail one current forecast projection closed without touching cohort members."""

        from services.orchestrator.file_orchestration_journal import FileOrchestrationJournalError

        # Writer-authority closed world (#1564 D8): the operator decision is
        # never a raw caller-supplied defer decision; only the dedicated typed
        # demotion may write it.  Refuse before any mutation.
        if reconciliation_decision == OPERATOR_VERIFIED_ABSENCE_DECISION:
            raise FileOrchestrationJournalError(
                "file_journal_authority_transition_requires_typed_api",
                field="reconciliation_decision",
            )
        source_id, cycle_time = _accepted_submit_source_cycle_from_job_id(job_id)
        with self._locked_cycle_write(source_id=source_id, cycle_time=cycle_time):
            existing = self._accepted_submit_job_for_id_unlocked(
                job_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            if (
                existing is None
                or not accepted_submit_contract_is_current(existing)
                or accepted_submit_row_kind(existing) != "master"
            ):
                return AcceptedSubmitCommitResult("stale", dict(existing or {}))
            return self._defer_forecast_cohort_projection_unlocked(
                existing,
                reconciliation_decision=reconciliation_decision,
                reconciliation_reason_class=reconciliation_reason_class,
                error_code=error_code,
                error_message=error_message,
                finished_at=finished_at,
                exit_code=exit_code,
                log_uri=log_uri,
            )

    def _defer_forecast_cohort_projection_unlocked(
        self,
        existing: Mapping[str, Any],
        *,
        reconciliation_decision: str,
        reconciliation_reason_class: str | None,
        error_code: str,
        error_message: str,
        finished_at: datetime | None,
        exit_code: int | None,
        log_uri: str | None,
    ) -> AcceptedSubmitCommitResult:
        from services.orchestrator import file_orchestration_journal
        from services.orchestrator.file_orchestration_journal import TERMINAL_PIPELINE_STATUSES

        current_status = str(existing.get("status") or "")
        if current_status in TERMINAL_PIPELINE_STATUSES:
            return AcceptedSubmitCommitResult("idempotent", dict(existing))
        if (
            existing.get("submit_outcome") != "accepted"
            or not str(existing.get("slurm_job_id") or "").isdigit()
        ):
            return AcceptedSubmitCommitResult("stale", dict(existing))
        row = apply_accepted_submit_transition(
            existing,
            AcceptedSubmitTransition.accounting(
                reconciliation_decision,
                submit_outcome="accepted",
                reconciliation_reason_class=reconciliation_reason_class,
                status="reconcile_unverified",
                # #1850 Fix B: a defer/incomplete-projection transition reasserts
                # the held tuple with the EXACT-COMMENT current source and no
                # matched id, but must retain the attempt's immutable binding
                # provenance. ``apply_accepted_submit_transition`` carries the
                # prior binding fields through because this transition sets none.
                reconciliation_source="slurm_exact_comment",
            ),
        )
        # #1589 (design D3): same rule as the batched projection leg -- a
        # display placeholder is a withheld value, not an overwrite.  This leg
        # is separately reachable: pass 1 parks the row on
        # ``reconcile_unverified``, which is not terminal, so the whole-row
        # short circuit above does not stop a pass 2 carrying a placeholder.
        # Everything below is resolved above the ``changed_fields`` equality
        # gate, so the value compared is the value persisted.
        #
        # The leg has TWO regimes and they take different resolutions.  The
        # error family here is written UNCONDITIONALLY, so declining is not an
        # option and a withheld value resolving to ``None`` would destroy the
        # persisted one -- and the gate would then compare a raw placeholder
        # against erased durable state and never converge, appending a record
        # per replay.  Hence ``durable=``, resolved BEFORE the
        # ``_durable_error_message`` wrap, exactly as
        # ``complete_pipeline_job_cancellation`` does it.  The trio below is
        # guarded by ``is not None``, where ``None`` makes the guard decline and
        # declining IS keeping, so it takes no ``durable=``.
        resolved_error_code = _resolved_caller_evidence(error_code, durable=existing.get("error_code"))
        resolved_error_message = _durable_error_message(
            _resolved_caller_evidence(error_message, durable=existing.get("error_message"))
        )
        row.update(
            {
                "status": "reconcile_unverified",
                "error_code": resolved_error_code,
                "error_message": resolved_error_message,
                "updated_at": _format_utc(file_orchestration_journal._utcnow()),
            }
        )
        finished_at = _resolved_caller_evidence(finished_at)
        exit_code = _resolved_caller_evidence(exit_code)
        log_uri = _resolved_caller_evidence(log_uri)
        if finished_at is not None:
            row["finished_at"] = _format_utc(finished_at)
        if exit_code is not None:
            row["exit_code"] = exit_code
        if log_uri is not None:
            row["log_uri"] = log_uri
        changed_fields = (
            "status",
            "reconciliation_source",
            "reconciliation_decision",
            "reconciliation_reason_class",
            "matched_slurm_job_id",
            "error_code",
            "error_message",
            "finished_at",
            "exit_code",
            "log_uri",
        )
        if all(row.get(field) == existing.get(field) for field in changed_fields):
            return AcceptedSubmitCommitResult("idempotent", dict(existing))
        written = self._write_pipeline_job_unlocked(
            row,
            exclusive_direct=False,
            model_id=None,
        )
        return AcceptedSubmitCommitResult("applied", written)
