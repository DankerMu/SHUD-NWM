from __future__ import annotations

from collections.abc import Callable, MutableMapping
from contextlib import contextmanager
from functools import wraps
from threading import RLock
from types import FunctionType, MappingProxyType, ModuleType
from typing import Any

SCHEDULER_STATE_COMPAT_REEXPORT_NAMES = (
    "ACTIVE_HYDRO_STATUSES",
    "ACTIVE_PIPELINE_STATUSES",
    "CANDIDATE_STATE_TASK_RESULT_LIMIT",
    "DEFAULT_CANDIDATE_STATE_EVENT_LIMIT",
    "DEFAULT_CANDIDATE_STATE_JOB_LIMIT",
    "DEFAULT_RETRY_LIMIT",
    "DOWNSTREAM_RESTART_STAGES",
    "DOWNSTREAM_STAGE_ALIASES",
    "DURABLE_HYDRO_SUCCESS_STATUSES",
    "FAILED_PIPELINE_STATUSES",
    "NATIVE_SHUD_STAGE_ALIASES",
    "STATE_CANDIDATE_SCOPED_PROOF_FIELDS",
    "STATE_M23_COMPARISON_FIELDS",
    "STATE_STRONG_CANDIDATE_SCOPED_PROOF_FIELDS",
    "TERMINAL_PIPELINE_SUCCESS_STATUSES",
    "TRANSIENT_RETRY_REASON_CODES",
    "CandidateStateDecision",
    "_bounded_active_slurm_jobs",
    "_bounded_candidate_event",
    "_bounded_candidate_state",
    "_bounded_task_result_rows",
    "_bounded_task_result_sample",
    "_call_active_slurm_jobs_provider",
    "_call_candidate_state_provider",
    "_cancelled_state_evidence",
    "_candidate_canonical_product_id",
    "_candidate_contract_pipeline_job_id",
    "_candidate_identity_from_evidence",
    "_candidate_production_identity",
    "_candidate_published_manifest_id",
    "_candidate_repaired_state_audit_evidence",
    "_candidate_scoped_shared_cycle_aggregate_state",
    "_candidate_scoped_shared_cycle_events",
    "_candidate_state_decision",
    "_candidate_state_decision_event",
    "_candidate_state_decision_state",
    "_candidate_state_evidence",
    "_candidate_state_filtered_decision_state",
    "_candidate_state_has_identity_mismatch",
    "_candidate_state_identity_validation",
    "_candidate_state_is_candidate_scoped_retry",
    "_candidate_state_source_allows_nested_authority",
    "_candidate_state_source_has_authoritative_ancestor",
    "_canonical_downstream_stage",
    "_coerce_int",
    "_coerce_mapping_for_state",
    "_coerce_optional_nonnegative_int",
    "_downstream_failure_restartable",
    "_downstream_retry_evidence",
    "_durable_shud_output_exists",
    "_ensure_utc",
    "_event_has_candidate_scoped_failure",
    "_event_has_failure_signal",
    "_event_identity_containers",
    "_event_is_manual_retry_marker",
    "_evidence_safe",
    "_failed_stage",
    "_failure_policy_payload",
    "_first_nested_state_value",
    "_first_nonempty",
    "_first_state_datetime",
    "_first_state_int",
    "_force_native_shud_rerun",
    "_forecast_cycle_manifest_uri",
    "_format_utc",
    "_global_source_cycle_download_blocker_job",
    "_has_candidate_task_failure",
    "_has_successful_download_stage",
    "_inconclusive_source_cycle_decision_state",
    "_inconclusive_source_cycle_unresolved_job_ids",
    "_is_raw_manifest_object_uri",
    "_is_source_cycle_download_stage",
    "_job_is_unsubmitted_auto_retry_placeholder",
    "_job_state_evidence",
    "_job_terminal_sort_key",
    "_job_terminal_time",
    "_latest_failed_job_for_stage",
    "_latest_failure_truth_timestamp",
    "_latest_manual_retry_blocker",
    "_latest_manual_retry_marker",
    "_latest_successful_download_stage",
    "_legacy_compatible_state_row",
    "_legacy_identity_values",
    "_legacy_non_authoritative_state_row",
    "_legacy_values_prove_same_candidate",
    "_looks_like_production_job_id",
    "_manual_retry_blocker_record",
    "_manual_retry_blocking_hydro_status",
    "_manual_retry_blocking_pipeline_status",
    "_manual_retry_marker_bound_to_blocker",
    "_manual_retry_marker_overrides_blocker",
    "_manual_retry_marker_record",
    "_manual_retry_marker_repairs_historical_failure",
    "_manual_retry_markers",
    "_manual_retry_new_attempt",
    "_manual_retry_payload",
    "_manual_retry_requested",
    "_manual_retry_state_evidence",
    "_missing_raw_manifest_repair_evidence",
    "_nested_state_identity_payloads",
    "_object_manifest_is_missing",
    "_optional_mapping_state",
    "_parse_state_datetime",
    "_permanent_failure_evidence",
    "_permanent_reason",
    "_pipeline_job_is_repaired_stage_evidence",
    "_pipeline_terminal_success_is_candidate_scoped",
    "_prior_failure_reason",
    "_redact_secret_manifest_for_evidence",
    "_repaired_raw_manifest_downstream_retry_evidence",
    "_repaired_stage_decision_state",
    "_restore_top_level_source_cycle_download_blocker",
    "_retry_failure_evidence",
    "_shared_cycle_aggregate_has_candidate_failure",
    "_shared_cycle_identity_values_match_candidate",
    "_shared_cycle_row_is_candidate_scoped",
    "_source_cycle_identity_matches_expected",
    "_stage_cycle_run_matches_candidate",
    "_state_active_jobs",
    "_state_error_code",
    "_state_error_message",
    "_state_event_limit",
    "_state_event_references_job_ids",
    "_state_events",
    "_state_has_failure_signal",
    "_state_has_only_repaired_pipeline_failure_signal",
    "_state_has_only_unsubmitted_auto_retry_placeholders",
    "_state_job_limit",
    "_state_jobs",
    "_state_output_uri",
    "_state_overflow_evidence",
    "_state_retry_attempt",
    "_state_retry_limit",
    "_state_row_has_authoritative_candidate_proof",
    "_state_row_has_m23_comparison_evidence",
    "_state_row_has_m23_comparison_fields",
    "_state_row_is_scoped_to_other_candidate",
    "_state_row_references_job_ids",
    "_state_status",
    "_state_task_identity",
    "_state_task_payload_failed",
    "_state_truth_sequence",
    "_state_truth_sort_key",
    "_state_values_are_scoped_to_other_candidate",
    "_state_values_have_authoritative_candidate_proof",
    "_state_values_have_candidate_scoped_m23_proof",
    "_state_values_have_complete_m23_identity",
    "_strip_top_level_candidate_state_decision_fields",
    "_strip_top_level_hydro_decision_fields",
    "_strip_top_level_pipeline_decision_fields",
    "_task_result_is_candidate_scoped",
    "_terminal_hydro_truth_supersedes_failure",
    "_top_level_source_cycle_download_blocker",
)


def install_scheduler_state_compat(
    facade_globals: MutableMapping[str, Any],
    owner_module: ModuleType,
) -> dict[str, Any]:
    for name in SCHEDULER_STATE_COMPAT_REEXPORT_NAMES:
        facade_globals.setdefault(name, getattr(owner_module, name))

    export_names = tuple(
        name
        for name, value in facade_globals.items()
        if name.startswith("_") and isinstance(value, FunctionType) and getattr(owner_module, name, None) is value
    )
    originals = {name: getattr(owner_module, name) for name in export_names}
    wrappers: dict[str, Callable[..., Any]] = {}
    lock = RLock()

    def compat_override(name: str, original: Any) -> Any:
        value = facade_globals.get(name, original)
        wrapper = wrappers.get(name)
        if wrapper is not None:
            return None if value is wrapper else value
        return None if value is original else value

    @contextmanager
    def compat_bindings() -> Any:
        with lock:
            previous: dict[str, Any] = {}
            for name, original in originals.items():
                override = compat_override(name, original)
                if override is None:
                    continue
                previous[name] = getattr(owner_module, name)
                setattr(owner_module, name, override)
            try:
                yield
            finally:
                for name, value in previous.items():
                    setattr(owner_module, name, value)

    def compat_wrapper(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(original)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            with compat_bindings():
                return original(*args, **kwargs)

        return wrapped

    for name, original in originals.items():
        if not isinstance(original, FunctionType):
            continue
        wrappers[name] = compat_wrapper(name, original)
        facade_globals[name] = wrappers[name]

    wrapper_names = export_names
    missing = tuple(name for name in SCHEDULER_STATE_COMPAT_REEXPORT_NAMES if not hasattr(owner_module, name))
    if missing:
        raise RuntimeError(
            "scheduler state compatibility names missing from owner module: " f"{', '.join(missing)}"
        )
    owner_reexports = MappingProxyType(
        {name: getattr(owner_module, name) for name in SCHEDULER_STATE_COMPAT_REEXPORT_NAMES}
    )
    facade_reexports = MappingProxyType(
        {name: facade_globals[name] for name in SCHEDULER_STATE_COMPAT_REEXPORT_NAMES}
    )
    reexport_function_names = tuple(
        name
        for name in SCHEDULER_STATE_COMPAT_REEXPORT_NAMES
        if name.startswith("_") and isinstance(owner_reexports[name], FunctionType)
    )
    if set(reexport_function_names) != set(wrapper_names):
        raise RuntimeError("scheduler state compatibility wrapper names drifted from owner re-export names")
    for name, owner_value in owner_reexports.items():
        if name in wrapper_names:
            continue
        if facade_reexports[name] is not owner_value:
            raise RuntimeError(f"scheduler state direct re-export drifted from owner module: {name}")

    return {
        "_SCHEDULER_STATE_COMPAT_EXPORT_NAMES": export_names,
        "_SCHEDULER_STATE_COMPAT_ORIGINALS": originals,
        "_SCHEDULER_STATE_COMPAT_WRAPPERS": wrappers,
        "_SCHEDULER_STATE_COMPAT_LOCK": lock,
        "_scheduler_state_compat_override": compat_override,
        "_scheduler_state_compat_bindings": compat_bindings,
        "_scheduler_state_compat_wrapper": compat_wrapper,
        "_SCHEDULER_STATE_COMPAT_WRAPPER_NAMES": wrapper_names,
        "_SCHEDULER_STATE_COMPAT_REEXPORT_NAMES": SCHEDULER_STATE_COMPAT_REEXPORT_NAMES,
        "_SCHEDULER_STATE_COMPAT_REEXPORT_MISSING": missing,
        "_SCHEDULER_STATE_COMPAT_OWNER_REEXPORTS": owner_reexports,
        "_SCHEDULER_STATE_COMPAT_FACADE_REEXPORTS": facade_reexports,
        "_SCHEDULER_STATE_COMPAT_REEXPORT_FUNCTION_NAMES": reexport_function_names,
        "_SCHEDULER_STATE_COMPAT_EXPORTS": tuple(
            facade_reexports[name] for name in SCHEDULER_STATE_COMPAT_REEXPORT_NAMES
        ),
    }
