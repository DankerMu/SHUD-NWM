from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from services.orchestrator import source_cycle_raw_manifest
from services.orchestrator.scheduler_timing import set_current_scheduler_pass_timing


class SchedulerExecutionCandidate(Protocol):
    candidate_id: str
    source_id: str
    cycle_id: str
    cycle_time_utc: datetime
    model_id: str
    basin_id: str
    basin_version_id: str | None
    river_network_version_id: str | None
    resource_profile: Mapping[str, Any]
    state_evidence: Mapping[str, Any]


class SchedulerExecutionConfig(Protocol):
    sources: tuple[str, ...]
    concurrent_submit_bound: int
    slurm_execution_enabled: bool
    slurm_env: Mapping[str, str]


@dataclass(frozen=True)
class SchedulerExecutionContext:
    config: SchedulerExecutionConfig
    forcing_producer: Any | None
    orchestrator_for: Callable[[str], Any]
    execute_candidate_cohort: Callable[..., list[dict[str, Any]]]
    set_last_submit_overlap_receipt: Callable[[Any], None]
    submit_overlap_receipt_factory: Callable[[], Any]
    timed_submission: Callable[..., Callable[[], Any]]
    run_concurrent_submissions: Callable[..., Sequence[Any]]
    cycle_id_for: Callable[[str, datetime], str]
    restart_compatible_candidate_cohorts: Callable[
        [Sequence[SchedulerExecutionCandidate]],
        list[tuple[tuple[int, str], list[SchedulerExecutionCandidate]]],
    ]
    candidate_execution_cohorts: Callable[
        [str, datetime, tuple[int, str], Sequence[SchedulerExecutionCandidate]],
        list[tuple[list[SchedulerExecutionCandidate], str | None]],
    ]
    candidate_is_fresh_full_chain: Callable[[SchedulerExecutionCandidate], bool]
    candidate_max_lead_hours: Callable[[SchedulerExecutionCandidate], int | None]
    candidate_canonical_product_id: Callable[[SchedulerExecutionCandidate], str]
    candidate_scheduler_canonical_identity: Callable[[SchedulerExecutionCandidate], dict[str, Any]]
    candidate_forcing_blocked_evidence: Callable[[SchedulerExecutionCandidate, Exception], dict[str, Any]]
    blocked_candidate: Callable[..., SchedulerExecutionCandidate]
    candidate_with_forcing_result: Callable[[SchedulerExecutionCandidate, Any], SchedulerExecutionCandidate]
    candidate_forcing_ready_evidence: Callable[[SchedulerExecutionCandidate, Any], dict[str, Any]]
    candidate_with_state_evidence: Callable[
        [SchedulerExecutionCandidate, Mapping[str, Any]],
        SchedulerExecutionCandidate,
    ]
    candidate_output_uri: Callable[..., str | None]
    candidate_identity_evidence: Callable[..., dict[str, Any]]
    candidate_model_run_review_evidence: Callable[..., dict[str, Any]]
    standard_chain_shape: Callable[[], list[Any]]
    candidate_basin_manifest: Callable[..., dict[str, Any]]
    slurm_env_check: Callable[[Mapping[str, str]], tuple[dict[str, Any], list[dict[str, Any]]]]
    candidate_slurm_preflight_blocked_evidence: Callable[
        [SchedulerExecutionCandidate, Mapping[str, Any]],
        dict[str, Any],
    ]
    secret_manifest_findings: Callable[..., Sequence[Mapping[str, str]]]
    candidate_secret_manifest_blocked_evidence: Callable[..., dict[str, Any]]
    slurm_resource_profile_blockers: Callable[[Mapping[str, Any]], list[dict[str, Any]]]
    evidence_safe: Callable[[Any], Any]
    candidate_execution_evidence: Callable[..., list[dict[str, Any]]]
    unknown_after_attempt: str
    # SUB-2 wiring for scheduler-pass-timing-instrumentation (#860): SUB-3
    # (scheduler_execution stage spans) and SUB-4 (chain_forecast_execution
    # candidate spans) consume the per-pass ``SchedulerPassTiming`` via this
    # attribute. Typed ``Any`` so this module does not import the collector
    # (avoids a cycle with ``scheduler_runtime`` on some import paths).
    # ``None`` at construction preserves back-compat for callers that build a
    # context without an active pass (e.g. unit-test fixtures for helpers
    # here that never reach a real ``run_once``).
    timing: Any | None = None


def produce_forcing_for_candidates(
    context: SchedulerExecutionContext,
    candidates: Sequence[SchedulerExecutionCandidate],
) -> tuple[
    list[SchedulerExecutionCandidate],
    list[SchedulerExecutionCandidate],
    list[dict[str, Any]],
]:
    assert context.forcing_producer is not None
    ready: list[SchedulerExecutionCandidate] = []
    blocked: list[SchedulerExecutionCandidate] = []
    evidence: list[dict[str, Any]] = []
    for candidate in candidates:
        # Fresh/full-chain and raw-manifest reuse candidates have no canonical
        # forcing package yet; the Slurm chain produces it after convert.
        if _candidate_skips_pre_orchestration_forcing(context, candidate):
            ready.append(candidate)
            continue
        try:
            result = context.forcing_producer.produce(
                source_id=candidate.source_id,
                cycle_time=candidate.cycle_time_utc,
                model_id=candidate.model_id,
                max_lead_hours=context.candidate_max_lead_hours(candidate),
                basin_id=candidate.basin_id,
                basin_version_id=candidate.basin_version_id,
                river_network_version_id=candidate.river_network_version_id,
                canonical_product_id=context.candidate_canonical_product_id(candidate),
                canonical_identity=context.candidate_scheduler_canonical_identity(candidate),
            )
        except Exception as error:
            item = context.candidate_forcing_blocked_evidence(candidate, error)
            evidence.append(item)
            blocked.append(
                context.blocked_candidate(
                    candidate,
                    "forcing_production_blocked",
                    state_evidence=item,
                )
            )
            continue
        produced_candidate = context.candidate_with_forcing_result(candidate, result)
        item = context.candidate_forcing_ready_evidence(produced_candidate, result)
        evidence.append(item)
        ready.append(context.candidate_with_state_evidence(produced_candidate, {"forcing_production": item}))
    return ready, blocked, evidence


def _candidate_skips_pre_orchestration_forcing(
    context: SchedulerExecutionContext,
    candidate: SchedulerExecutionCandidate,
) -> bool:
    if context.candidate_is_fresh_full_chain(candidate):
        return True
    state_evidence = candidate.state_evidence
    if not isinstance(state_evidence, Mapping):
        return False
    fresh_ingestion = state_evidence.get("fresh_ingestion")
    raw_manifest_reuse = state_evidence.get("raw_manifest_reuse")
    return (
        isinstance(fresh_ingestion, Mapping)
        and str(fresh_ingestion.get("mode") or "") == "reuse_raw_then_convert"
        and isinstance(raw_manifest_reuse, Mapping)
        and str(raw_manifest_reuse.get("status") or "") == "ready"
    )


@dataclass(frozen=True)
class _CohortUnit:
    source_id: str
    cycle_time: datetime
    cycle_id: str
    execution_candidates: list[SchedulerExecutionCandidate]
    cohort_run_id: str | None


def execute_candidates(
    context: SchedulerExecutionContext,
    candidates: Sequence[SchedulerExecutionCandidate],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, datetime], list[SchedulerExecutionCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault((candidate.source_id, candidate.cycle_time_utc), []).append(candidate)

    units = _execution_units(context, grouped)
    receipt = context.submit_overlap_receipt_factory()
    context.set_last_submit_overlap_receipt(receipt)

    def _submitter(unit: _CohortUnit) -> list[dict[str, Any]]:
        key = f"{unit.source_id}:{unit.cycle_id}:{unit.cohort_run_id or 'full'}"
        run = context.timed_submission(
            lambda: context.execute_candidate_cohort(
                unit.source_id,
                unit.cycle_time,
                unit.cycle_id,
                unit.execution_candidates,
                orchestration_run_id=unit.cohort_run_id,
            ),
            receipt=receipt,
            idempotency_key=key,
            candidate_id=unit.cohort_run_id,
        )
        return run()

    results = context.run_concurrent_submissions(
        [(lambda u=unit: _submitter(u)) for unit in units],
        max_workers=context.config.concurrent_submit_bound,
    )

    evidence: list[dict[str, Any]] = []
    for result in results:
        if isinstance(result, Exception):
            raise result
        evidence.extend(result)
    return evidence


def _execution_units(
    context: SchedulerExecutionContext,
    grouped: Mapping[tuple[str, datetime], Sequence[SchedulerExecutionCandidate]],
) -> list[_CohortUnit]:
    source_order = {source_id.lower(): index for index, source_id in enumerate(context.config.sources)}
    units: list[_CohortUnit] = []
    for (source_id, cycle_time), cycle_candidates in sorted(
        grouped.items(),
        key=lambda item: (
            item[0][1],
            source_order.get(item[0][0].lower(), 999),
            item[0][0].lower(),
            [candidate.model_id for candidate in item[1]],
        ),
    ):
        cycle_id = context.cycle_id_for(source_id, cycle_time)
        for cohort_key, cohort_candidates in context.restart_compatible_candidate_cohorts(
            _unique_execution_candidates(cycle_candidates)
        ):
            for execution_candidates, cohort_run_id in context.candidate_execution_cohorts(
                source_id,
                cycle_time,
                cohort_key,
                cohort_candidates,
            ):
                units.append(
                    _CohortUnit(
                        source_id=source_id,
                        cycle_time=cycle_time,
                        cycle_id=cycle_id,
                        execution_candidates=list(execution_candidates),
                        cohort_run_id=cohort_run_id,
                    )
                )
    return units


def _unique_execution_candidates(
    candidates: Sequence[SchedulerExecutionCandidate],
) -> list[SchedulerExecutionCandidate]:
    unique: list[SchedulerExecutionCandidate] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for candidate in candidates:
        key = (
            str(candidate.source_id),
            str(candidate.cycle_id),
            str(candidate.model_id),
            str(candidate.basin_id),
            str(getattr(candidate, "run_id", "")),
            str(candidate.candidate_id),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique


def execute_candidate_cohort(
    context: SchedulerExecutionContext,
    source_id: str,
    cycle_time: datetime,
    cycle_id: str,
    cycle_candidates: Sequence[SchedulerExecutionCandidate],
    *,
    orchestration_run_id: str | None,
) -> list[dict[str, Any]]:
    # SUB-3 (#861) Phase 6.5 refactor: the cohort-scope ``stage_span`` opened
    # here previously used ``stage_name = orchestration_run_id or "full"``
    # (outside spec's canonical five-entry ``_FORECAST_STAGE_ORDER`` domain)
    # and stashed the collector + active span on ``orchestrator`` — a shared
    # ``ForecastOrchestrator`` instance is reused across ``ThreadPoolExecutor``
    # workers when ``concurrent_submit_bound > 1``, so those stashes raced
    # (empirically 27-35% test flake on ``test_multi_candidate_restart_...`` +
    # ``test_backfill_selects_global_oldest_cycle_...``).
    #
    # The correct wiring is: open one ``stage_span`` per pipeline stage
    # (``convert``, ``forcing``, ``forecast``, ``parse``, ``state_save_qc``)
    # inside ``chain_forecast_execution._run_cycle_chain`` — one canonical
    # record per (source_id, cycle_id, stage_name) tuple as spec.md §"Stage-
    # layer timing" requires. Downstream reads the collector via a
    # ``contextvars.ContextVar`` bound here (per-thread, not shared) rather
    # than an orchestrator attribute stash that raced across concurrent
    # cohort workers.
    orchestrator = context.orchestrator_for(source_id)
    with set_current_scheduler_pass_timing(context.timing):
        return _execute_candidate_cohort_impl(
            context,
            source_id,
            cycle_time,
            cycle_id,
            cycle_candidates,
            orchestration_run_id=orchestration_run_id,
            orchestrator=orchestrator,
        )


def _execute_candidate_cohort_impl(
    context: SchedulerExecutionContext,
    source_id: str,
    cycle_time: datetime,
    cycle_id: str,
    cycle_candidates: Sequence[SchedulerExecutionCandidate],
    *,
    orchestration_run_id: str | None,
    orchestrator: Any,
) -> list[dict[str, Any]]:
    # SUB-4 (#862): per-basin / per-cohort sub-phase timing accumulator.
    #
    # Keyed on ``candidate.candidate_id`` for both loops (loop 1 populates the
    # ``output_uri_lookup_ms`` + ``basin_manifest_build_ms`` fields for every
    # ``candidate_output_uris``-eligible basin; loop 2 populates the three
    # remaining per-basin fields for every basin that survives the Slurm
    # preflight). ``basin_ms`` is a plain ``dict[str, float]`` — timing
    # collector fan-out via ``attribute_per_basin_fields`` after
    # ``orchestrate_cycle`` returns is a no-op below level ``candidate``,
    # so measuring unconditionally here is safe (adds ~ns per basin) and
    # keeps the code path branchless.
    per_basin_ms: dict[str, dict[str, float]] = {}
    evidence: list[dict[str, Any]] = []
    basins: list[dict[str, Any]] = []
    submitted_candidates: list[SchedulerExecutionCandidate] = []
    candidate_output_uris: dict[str, str] = {}
    for candidate in cycle_candidates:
        basin_ms = per_basin_ms.setdefault(candidate.candidate_id, {})
        _ns_before = time.monotonic_ns()
        output_uri = context.candidate_output_uri(candidate, getattr(orchestrator, "object_store", None))
        basin_ms["output_uri_lookup_ms"] = (time.monotonic_ns() - _ns_before) / 1_000_000.0
        if output_uri is None:
            evidence.append(
                {
                    **context.candidate_identity_evidence(candidate),
                    "status": "blocked",
                    "submitted": False,
                    "mutation_occurred": False,
                    "cycle_id": cycle_id,
                    "error_code": "OUTPUT_URI_UNAVAILABLE",
                    "error_message": (
                        "Production orchestration requires an absolute deterministic output_uri "
                        "before runtime handoff."
                    ),
                    **context.candidate_model_run_review_evidence(
                        candidate,
                        output_uri=output_uri,
                        outcome=None,
                        status="blocked",
                        stage_statuses=[],
                    ),
                    "standard_chain_shape": context.standard_chain_shape(),
                    "qhh_script_invoked": False,
                }
            )
            continue
        candidate_output_uris[candidate.candidate_id] = output_uri
        submitted_candidates.append(candidate)
        _ns_before = time.monotonic_ns()
        basin_manifest = context.candidate_basin_manifest(
            candidate,
            output_uri=output_uri,
            orchestration_run_id=orchestration_run_id,
        )
        if context.config.slurm_execution_enabled and context.config.slurm_env:
            basin_manifest["slurm_env"] = dict(context.config.slurm_env)
        basin_ms["basin_manifest_build_ms"] = (time.monotonic_ns() - _ns_before) / 1_000_000.0
        basins.append(basin_manifest)
    if not basins:
        return evidence
    if context.config.slurm_execution_enabled:
        safe_pairs: list[tuple[SchedulerExecutionCandidate, dict[str, Any]]] = []
        for candidate, basin_manifest in zip(submitted_candidates, basins, strict=True):
            basin_ms = per_basin_ms.setdefault(candidate.candidate_id, {})
            env_value = basin_manifest.get("slurm_env") or {}
            if env_value:
                _ns_before = time.monotonic_ns()
                env_check, env_blockers = context.slurm_env_check(env_value)
                basin_ms["slurm_env_check_ms"] = (time.monotonic_ns() - _ns_before) / 1_000_000.0
                if env_blockers:
                    evidence.append(
                        context.candidate_slurm_preflight_blocked_evidence(
                            candidate,
                            {
                                "status": "blocked",
                                "enabled": True,
                                "blockers": env_blockers,
                                "checks": {"environment": env_check},
                            },
                        )
                    )
                    continue
            else:
                basin_ms["slurm_env_check_ms"] = 0.0
            _ns_before = time.monotonic_ns()
            findings = context.secret_manifest_findings(basin_manifest, "manifest")
            basin_ms["secret_manifest_scan_ms"] = (time.monotonic_ns() - _ns_before) / 1_000_000.0
            if findings:
                evidence.append(context.candidate_secret_manifest_blocked_evidence(candidate, findings=findings))
                continue
            _ns_before = time.monotonic_ns()
            resource_profile_blockers = context.slurm_resource_profile_blockers(candidate.resource_profile)
            basin_ms["resource_profile_check_ms"] = (time.monotonic_ns() - _ns_before) / 1_000_000.0
            if resource_profile_blockers:
                evidence.append(
                    context.candidate_slurm_preflight_blocked_evidence(
                        candidate,
                        {
                            "status": "blocked",
                            "enabled": True,
                            "blockers": resource_profile_blockers,
                            "checks": {"resource_profile": {"valid": False}},
                        },
                    )
                )
                continue
            safe_pairs.append((candidate, basin_manifest))
        submitted_candidates = [candidate for candidate, _basin_manifest in safe_pairs]
        basins = [basin_manifest for _candidate, basin_manifest in safe_pairs]
        if not basins:
            return evidence
    # SUB-4 (#862): per-cohort measurements attributed as equal share to every
    # surviving basin. ``stage_raw_input_ms`` and ``orchestrator_dispatch_ms``
    # are cohort-scope wall-clock; dividing by ``basin_count`` and writing the
    # share into each surviving basin's dict is the spec's accounting simplifier
    # (tasks.md §2.4).
    basin_count = max(len(submitted_candidates), 1)
    _ns_before = time.monotonic_ns()
    try:
        evidence.extend(_stage_nfs_raw_inputs_for_candidates(submitted_candidates))
    except Exception as error:
        stage_raw_input_ms_share = ((time.monotonic_ns() - _ns_before) / 1_000_000.0) / basin_count
        for candidate in submitted_candidates:
            per_basin_ms.setdefault(candidate.candidate_id, {})["stage_raw_input_ms"] = stage_raw_input_ms_share
        safe_error_message = context.evidence_safe(str(error))
        for candidate in submitted_candidates:
            output_uri = candidate_output_uris.get(candidate.candidate_id)
            evidence.append(
                {
                    **context.candidate_identity_evidence(candidate, output_uri=output_uri),
                    "status": "blocked",
                    "submitted": False,
                    "slurm_submit_called": False,
                    "execution_attempted": False,
                    "mutation_occurred": False,
                    "cycle_id": cycle_id,
                    "error_code": "RAW_INPUT_STAGING_FAILED",
                    "error_message": safe_error_message,
                    **context.candidate_model_run_review_evidence(
                        candidate,
                        output_uri=output_uri,
                        outcome=None,
                        status="blocked",
                        stage_statuses=[],
                    ),
                    "standard_chain_shape": context.standard_chain_shape(),
                    "qhh_script_invoked": False,
                }
            )
        # Fan-out is a no-op below level ``candidate``. Also a no-op when
        # ``candidate_span`` never fired for this basin (raw-input staging
        # failed before ``orchestrate_cycle`` — no candidate records exist
        # for this basin). Safe to invoke unconditionally.
        _fan_out_per_basin_ms(context, submitted_candidates, per_basin_ms)
        return evidence
    stage_raw_input_ms_share = ((time.monotonic_ns() - _ns_before) / 1_000_000.0) / basin_count
    for candidate in submitted_candidates:
        per_basin_ms.setdefault(candidate.candidate_id, {})["stage_raw_input_ms"] = stage_raw_input_ms_share
    _ns_before = time.monotonic_ns()
    try:
        result = orchestrator.orchestrate_cycle(source_id, cycle_time, basins)
    except Exception as error:
        orchestrator_dispatch_ms_share = ((time.monotonic_ns() - _ns_before) / 1_000_000.0) / basin_count
        for candidate in submitted_candidates:
            per_basin_ms.setdefault(candidate.candidate_id, {})["orchestrator_dispatch_ms"] = (
                orchestrator_dispatch_ms_share
            )
        safe_error_message = context.evidence_safe(getattr(error, "message", str(error)))
        error_code = str(getattr(error, "error_code", "PRODUCTION_ORCHESTRATION_FAILED"))
        for candidate in submitted_candidates:
            output_uri = candidate_output_uris.get(candidate.candidate_id)
            evidence.append(
                {
                    **context.candidate_identity_evidence(candidate, output_uri=output_uri),
                    "status": "submission_failed",
                    "submitted": False,
                    "slurm_submit_called": context.unknown_after_attempt,
                    "execution_attempted": True,
                    "mutation_outcome": context.unknown_after_attempt,
                    "mutation_occurred": context.unknown_after_attempt,
                    "cycle_id": cycle_id,
                    "error_code": error_code,
                    "error_message": safe_error_message,
                    **context.candidate_model_run_review_evidence(
                        candidate,
                        output_uri=output_uri,
                        outcome=None,
                        status="submission_failed",
                        stage_statuses=[],
                    ),
                    "standard_chain_shape": context.standard_chain_shape(),
                    "qhh_script_invoked": False,
                    "pipeline_status_write": context.unknown_after_attempt,
                    "pipeline_event_write": context.unknown_after_attempt,
                    "pipeline_status_writes_proven_absent": False,
                    "pipeline_event_writes_proven_absent": False,
                    "residual_blockers": [
                        {
                            "code": error_code,
                            "state": "blocked",
                            "quality_flag": "production_orchestration_failed",
                            "residual_risk": (
                                "Production orchestration raised after the downstream orchestration method "
                                "was called; production write outcome is unknown."
                            ),
                        }
                    ],
                }
            )
        _fan_out_per_basin_ms(context, submitted_candidates, per_basin_ms)
        return evidence
    orchestrator_dispatch_ms_share = ((time.monotonic_ns() - _ns_before) / 1_000_000.0) / basin_count
    for candidate in submitted_candidates:
        per_basin_ms.setdefault(candidate.candidate_id, {})["orchestrator_dispatch_ms"] = (
            orchestrator_dispatch_ms_share
        )
    evidence.extend(
        context.candidate_execution_evidence(
            result,
            submitted_candidates,
            output_uris=candidate_output_uris,
        )
    )
    # SUB-4 (#862): fan per-basin + per-cohort sub-phase timings onto every
    # candidate record ``_submit_and_wait`` created under ``orchestrate_cycle``.
    # ``attribute_per_basin_fields`` is a no-op when the collector level is
    # below ``candidate`` (no candidate records exist to update).
    _fan_out_per_basin_ms(context, submitted_candidates, per_basin_ms)
    return evidence


def _fan_out_per_basin_ms(
    context: SchedulerExecutionContext,
    submitted_candidates: Sequence[SchedulerExecutionCandidate],
    per_basin_ms: Mapping[str, Mapping[str, float]],
) -> None:
    """Attribute accumulated per-basin ``*_ms`` fields onto every candidate
    record already created under ``orchestrate_cycle`` for this cohort.

    The collector method is a no-op below level ``candidate`` so this call
    is safe to invoke unconditionally after ``execute_candidate_cohort``.
    """

    collector = context.timing
    if collector is None:
        return
    for candidate in submitted_candidates:
        fields = per_basin_ms.get(candidate.candidate_id)
        if not fields:
            continue
        collector.attribute_per_basin_fields(
            basin=candidate.basin_id,
            source_id=candidate.source_id,
            fields=dict(fields),
        )


def _stage_nfs_raw_inputs_for_candidates(candidates: Sequence[SchedulerExecutionCandidate]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    staged_keys: set[str] = set()
    for candidate in candidates:
        state_evidence = candidate.state_evidence
        if not isinstance(state_evidence, Mapping):
            continue
        nfs_raw_manifest = state_evidence.get("nfs_raw_manifest")
        if isinstance(nfs_raw_manifest, Mapping):
            key = str(
                nfs_raw_manifest.get("manifest_uri")
                or nfs_raw_manifest.get("manifest_path")
                or f"{candidate.source_id}:{candidate.cycle_id}"
            )
            if key in staged_keys:
                continue
            staged_keys.add(key)
        staged = source_cycle_raw_manifest.stage_nfs_raw_manifest_from_env(state_evidence)
        if staged is not None:
            evidence.append({"type": "nfs_raw_manifest_staging", **staged})
    return evidence


def restart_compatible_candidate_cohorts(
    candidates: Sequence[SchedulerExecutionCandidate],
    *,
    candidate_restart_stage: Callable[[SchedulerExecutionCandidate], str | None],
    candidate_restart_cohort_key: Callable[[str | None], tuple[int, str]],
) -> list[tuple[tuple[int, str], list[SchedulerExecutionCandidate]]]:
    cohorts: dict[tuple[int, str], list[SchedulerExecutionCandidate]] = {}
    for candidate in candidates:
        restart_stage = candidate_restart_stage(candidate)
        key = candidate_restart_cohort_key(restart_stage)
        cohorts.setdefault(key, []).append(candidate)
    return sorted(
        cohorts.items(),
        key=lambda item: (item[0][0], item[0][1], [candidate.model_id for candidate in item[1]]),
    )


def candidate_restart_stage(
    candidate: SchedulerExecutionCandidate,
    *,
    candidate_is_fresh_full_chain: Callable[[SchedulerExecutionCandidate], bool],
    native_shud_stage_aliases: set[str] | frozenset[str],
    canonical_downstream_stage: Callable[[str], str | None],
) -> str | None:
    if candidate_is_fresh_full_chain(candidate):
        return None
    state_evidence = candidate.state_evidence
    if not isinstance(state_evidence, Mapping):
        return None
    stage = str(state_evidence.get("restart_stage") or state_evidence.get("restart_from_stage") or "")
    if stage in native_shud_stage_aliases:
        return "forecast"
    return canonical_downstream_stage(stage)


def candidate_restart_cohort_key(
    restart_stage: str | None,
    *,
    downstream_restart_stages: Sequence[str] = (),
) -> tuple[int, str]:
    if restart_stage is None:
        return (0, "full")
    stage_order = {stage: index for index, stage in enumerate(downstream_restart_stages, start=1)}
    return (stage_order.get(restart_stage, len(stage_order) + 1), restart_stage)


def candidate_execution_cohort_run_id(
    source_id: str,
    cycle_time: datetime,
    cohort_key: tuple[int, str],
    *,
    format_cycle_time: Callable[[datetime], str],
) -> str:
    stage = re.sub(r"[^A-Za-z0-9_.-]+", "_", cohort_key[1]).strip("._-") or "full"
    return f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}_{stage}"


def candidate_execution_cohorts(
    source_id: str,
    cycle_time: datetime,
    cohort_key: tuple[int, str],
    candidates: Sequence[SchedulerExecutionCandidate],
    *,
    run_id_for_candidate: Callable[
        [str, datetime, tuple[int, str], SchedulerExecutionCandidate],
        str,
    ],
) -> list[tuple[list[SchedulerExecutionCandidate], str | None]]:
    if cohort_key[1] == "full":
        return [
            ([candidate], run_id_for_candidate(source_id, cycle_time, cohort_key, candidate))
            for candidate in candidates
        ]
    return [
        ([candidate], run_id_for_candidate(source_id, cycle_time, cohort_key, candidate))
        for candidate in candidates
    ]


def candidate_execution_cohort_run_id_for_candidate(
    source_id: str,
    cycle_time: datetime,
    cohort_key: tuple[int, str],
    candidate: SchedulerExecutionCandidate,
    *,
    format_cycle_time: Callable[[datetime], str],
) -> str:
    stage = re.sub(r"[^A-Za-z0-9_.-]+", "_", cohort_key[1]).strip("._-") or "full"
    model_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate.model_id).strip("._-") or "candidate"
    return f"cycle_{source_id.lower()}_{format_cycle_time(cycle_time)}_{stage}_{model_id}"
