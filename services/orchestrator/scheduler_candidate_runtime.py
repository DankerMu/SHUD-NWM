# ruff: noqa: E402,E501,F401,F821,I001
from __future__ import annotations

from services.orchestrator import scheduler as _scheduler

globals().update(_scheduler.__dict__)

_SCHEDULER_CANDIDATE_EXECUTION_EVIDENCE_FORWARDER_NAMES = (
    "_candidate_execution_attempted",
    "_pipeline_result_slurm_submit_called",
    "_pipeline_result_pipeline_status_write",
    "_pipeline_result_pipeline_event_write",
    "_pipeline_result_pipeline_write_value",
    "_pipeline_result_has_pipeline_job_evidence",
    "_pipeline_result_write_absence_proven",
    "_candidate_slurm_submit_called",
    "_candidate_pipeline_write_value",
    "_execution_mutation_value",
    "_nonempty_evidence_value",
    "_candidate_identity_evidence",
    "_candidate_preflight_blocked_evidence",
    "_candidate_slurm_preflight_blocked_evidence",
    "_candidate_evidence_write_blocked_evidence",
    "_cancel_candidate_evidence_write_blocked_evidence",
    "_sync_candidate_evidence_write_blocked_evidence",
    "_candidate_secret_manifest_blocked_evidence",
    "_slurm_resource_profile_blockers",
    "_resource_profile_evidence",
    "_redact_digest_keys_for_evidence",
    "_is_digest_evidence_key",
    "_candidate_execution_evidence",
    "_candidate_forcing_ready_evidence",
    "_candidate_forcing_blocked_evidence",
    "_candidate_execution_evidence_item",
    "_candidate_status_from_outcome",
    "_candidate_model_run_review_evidence",
    "_candidate_stage_evidence",
    "_candidate_stage_evidence_item",
    "_stage_run_evidence",
    "_stage_task_results",
    "_task_result_matches_candidate",
    "_task_candidate_matching_available",
    "_normalized_identity",
    "_task_result_evidence",
    "_resource_metrics_from_mapping",
)


def _scheduler_candidate_execution_evidence_forwarder(name: str) -> Callable[..., Any]:
    def forwarder(*args: Any, **kwargs: Any) -> Any:
        from services.orchestrator import scheduler_candidate_execution_evidence

        return getattr(scheduler_candidate_execution_evidence, name)(*args, **kwargs)

    forwarder.__name__ = name
    return forwarder


_candidate_execution_attempted = _scheduler_candidate_execution_evidence_forwarder("_candidate_execution_attempted")
_pipeline_result_slurm_submit_called = _scheduler_candidate_execution_evidence_forwarder(
    "_pipeline_result_slurm_submit_called"
)
_pipeline_result_pipeline_status_write = _scheduler_candidate_execution_evidence_forwarder(
    "_pipeline_result_pipeline_status_write"
)
_pipeline_result_pipeline_event_write = _scheduler_candidate_execution_evidence_forwarder(
    "_pipeline_result_pipeline_event_write"
)
_pipeline_result_pipeline_write_value = _scheduler_candidate_execution_evidence_forwarder(
    "_pipeline_result_pipeline_write_value"
)
_pipeline_result_has_pipeline_job_evidence = _scheduler_candidate_execution_evidence_forwarder(
    "_pipeline_result_has_pipeline_job_evidence"
)
_pipeline_result_write_absence_proven = _scheduler_candidate_execution_evidence_forwarder(
    "_pipeline_result_write_absence_proven"
)
_candidate_slurm_submit_called = _scheduler_candidate_execution_evidence_forwarder("_candidate_slurm_submit_called")
_candidate_pipeline_write_value = _scheduler_candidate_execution_evidence_forwarder("_candidate_pipeline_write_value")
_execution_mutation_value = _scheduler_candidate_execution_evidence_forwarder("_execution_mutation_value")
_nonempty_evidence_value = _scheduler_candidate_execution_evidence_forwarder("_nonempty_evidence_value")
_candidate_identity_evidence = _scheduler_candidate_execution_evidence_forwarder("_candidate_identity_evidence")
_candidate_preflight_blocked_evidence = _scheduler_candidate_execution_evidence_forwarder(
    "_candidate_preflight_blocked_evidence"
)
_candidate_slurm_preflight_blocked_evidence = _scheduler_candidate_execution_evidence_forwarder(
    "_candidate_slurm_preflight_blocked_evidence"
)


def _candidate_evidence_write_blocked_evidence(
    candidate: SchedulerCandidate,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    return _scheduler_evidence.candidate_evidence_write_blocked_evidence(
        candidate,
        reservation,
        candidate_model_run_review_evidence=_candidate_model_run_review_evidence,
        candidate_identity_evidence=_candidate_identity_evidence,
        standard_chain_shape=[stage.stage for stage in ForecastOrchestrator.stages],
        evidence_safe=_evidence_safe,
    )


def _cancel_candidate_evidence_write_blocked_evidence(
    candidate: Mapping[str, Any],
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    return _scheduler_evidence.cancel_candidate_evidence_write_blocked_evidence(
        candidate,
        reservation,
        ensure_utc=_ensure_utc,
        evidence_safe=_evidence_safe,
    )


def _sync_candidate_evidence_write_blocked_evidence(
    candidate: Mapping[str, Any],
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    return _scheduler_evidence.sync_candidate_evidence_write_blocked_evidence(
        candidate,
        reservation,
        standard_chain_shape=[stage.stage for stage in ForecastOrchestrator.stages],
        evidence_safe=_evidence_safe,
    )


_candidate_secret_manifest_blocked_evidence = _scheduler_candidate_execution_evidence_forwarder(
    "_candidate_secret_manifest_blocked_evidence"
)
_slurm_resource_profile_blockers = _scheduler_candidate_execution_evidence_forwarder("_slurm_resource_profile_blockers")
_resource_profile_evidence = _scheduler_candidate_execution_evidence_forwarder("_resource_profile_evidence")
_redact_digest_keys_for_evidence = _scheduler_candidate_execution_evidence_forwarder("_redact_digest_keys_for_evidence")
_is_digest_evidence_key = _scheduler_candidate_execution_evidence_forwarder("_is_digest_evidence_key")
_candidate_execution_evidence = _scheduler_candidate_execution_evidence_forwarder("_candidate_execution_evidence")
_candidate_forcing_ready_evidence = _scheduler_candidate_execution_evidence_forwarder(
    "_candidate_forcing_ready_evidence"
)
_candidate_forcing_blocked_evidence = _scheduler_candidate_execution_evidence_forwarder(
    "_candidate_forcing_blocked_evidence"
)
_candidate_execution_evidence_item = _scheduler_candidate_execution_evidence_forwarder(
    "_candidate_execution_evidence_item"
)
_candidate_status_from_outcome = _scheduler_candidate_execution_evidence_forwarder("_candidate_status_from_outcome")
_candidate_model_run_review_evidence = _scheduler_candidate_execution_evidence_forwarder(
    "_candidate_model_run_review_evidence"
)
_candidate_stage_evidence = _scheduler_candidate_execution_evidence_forwarder("_candidate_stage_evidence")
_candidate_stage_evidence_item = _scheduler_candidate_execution_evidence_forwarder("_candidate_stage_evidence_item")
_stage_run_evidence = _scheduler_candidate_execution_evidence_forwarder("_stage_run_evidence")
_stage_task_results = _scheduler_candidate_execution_evidence_forwarder("_stage_task_results")
_task_result_matches_candidate = _scheduler_candidate_execution_evidence_forwarder("_task_result_matches_candidate")
_task_candidate_matching_available = _scheduler_candidate_execution_evidence_forwarder(
    "_task_candidate_matching_available"
)
_normalized_identity = _scheduler_candidate_execution_evidence_forwarder("_normalized_identity")
_task_result_evidence = _scheduler_candidate_execution_evidence_forwarder("_task_result_evidence")
_resource_metrics_from_mapping = _scheduler_candidate_execution_evidence_forwarder("_resource_metrics_from_mapping")


_SCHEDULER_CANDIDATE_QUALITY_FORWARDER_NAMES = (
    "_candidate_artifact_refs",
    "_candidate_resource_summary",
    "_candidate_forcing_evidence",
    "_candidate_output_evidence",
    "_candidate_display_evidence",
    "_candidate_quality_states",
    "_candidate_frequency_evidence",
    "_candidate_residual_blockers",
    "_candidate_product_counts",
    "_candidate_output_river_manifest",
    "_first_present_value",
    "_first_present_int",
    "_model_package_manifest_uri",
    "_candidate_output_key",
    "_candidate_output_uri",
    "_has_uri_scheme",
    "_candidate_station_count",
    "_candidate_station_ids",
)


def _scheduler_candidate_quality_forwarder(name: str) -> Callable[..., Any]:
    def forwarder(*args: Any, **kwargs: Any) -> Any:
        from services.orchestrator import scheduler_candidate_quality

        return getattr(scheduler_candidate_quality, name)(*args, **kwargs)

    forwarder.__name__ = name
    return forwarder


for _scheduler_candidate_quality_name in _SCHEDULER_CANDIDATE_QUALITY_FORWARDER_NAMES:
    globals()[_scheduler_candidate_quality_name] = _scheduler_candidate_quality_forwarder(
        _scheduler_candidate_quality_name
    )
del _scheduler_candidate_quality_name
_candidate_artifact_refs = _scheduler_candidate_quality_forwarder("_candidate_artifact_refs")
_candidate_resource_summary = _scheduler_candidate_quality_forwarder("_candidate_resource_summary")
_candidate_forcing_evidence = _scheduler_candidate_quality_forwarder("_candidate_forcing_evidence")
_candidate_output_evidence = _scheduler_candidate_quality_forwarder("_candidate_output_evidence")
_candidate_display_evidence = _scheduler_candidate_quality_forwarder("_candidate_display_evidence")
_candidate_quality_states = _scheduler_candidate_quality_forwarder("_candidate_quality_states")
_candidate_frequency_evidence = _scheduler_candidate_quality_forwarder("_candidate_frequency_evidence")
_candidate_residual_blockers = _scheduler_candidate_quality_forwarder("_candidate_residual_blockers")
_candidate_product_counts = _scheduler_candidate_quality_forwarder("_candidate_product_counts")
_candidate_output_river_manifest = _scheduler_candidate_quality_forwarder("_candidate_output_river_manifest")
_first_present_value = _scheduler_candidate_quality_forwarder("_first_present_value")
_first_present_int = _scheduler_candidate_quality_forwarder("_first_present_int")
_model_package_manifest_uri = _scheduler_candidate_quality_forwarder("_model_package_manifest_uri")
_candidate_output_key = _scheduler_candidate_quality_forwarder("_candidate_output_key")
_candidate_output_uri = _scheduler_candidate_quality_forwarder("_candidate_output_uri")
_has_uri_scheme = _scheduler_candidate_quality_forwarder("_has_uri_scheme")
_candidate_station_count = _scheduler_candidate_quality_forwarder("_candidate_station_count")
_candidate_station_ids = _scheduler_candidate_quality_forwarder("_candidate_station_ids")


_scheduler_shud_executable = _scheduler_preflight._scheduler_shud_executable
_slurm_shud_executable_check = _scheduler_preflight._slurm_shud_executable_check
_gateway_endpoint = _scheduler_preflight._gateway_endpoint
_gateway_self_reference_blocker = _scheduler_preflight._gateway_self_reference_blocker
_interpret_gateway_health = _scheduler_preflight._interpret_gateway_health
_in_process_gateway_probe = _scheduler_preflight._in_process_gateway_probe
_scheduler_grib_env_root = _scheduler_preflight._scheduler_grib_env_root
_default_grib_system_eccodes_probe = _scheduler_preflight._default_grib_system_eccodes_probe
_slurm_grib_env_check = _scheduler_preflight._slurm_grib_env_check
_database_url_blocker = _scheduler_preflight._database_url_blocker
_database_host = _scheduler_preflight._database_host
_database_host_is_local = _scheduler_preflight._database_host_is_local
_database_host_is_unsafe = _scheduler_preflight._database_host_is_unsafe
_normalize_database_host = _scheduler_preflight._normalize_database_host
_database_host_ip_address = _scheduler_preflight._database_host_ip_address
_parse_noncanonical_ipv4_address = _scheduler_preflight._parse_noncanonical_ipv4_address
_is_noncanonical_numeric_ipv4_host = _scheduler_preflight._is_noncanonical_numeric_ipv4_host
_is_numeric_ipv4_like_host = _scheduler_preflight._is_numeric_ipv4_like_host
_is_ipv4_number_part = _scheduler_preflight._is_ipv4_number_part
_is_noncanonical_ipv4_part = _scheduler_preflight._is_noncanonical_ipv4_part
_is_unsafe_numeric_ipv4_like_host = _scheduler_preflight._is_unsafe_numeric_ipv4_like_host
_preflight_allowed_roots = _scheduler_preflight._preflight_allowed_roots
_storage_root_check = _scheduler_preflight._storage_root_check
_path_is_under_any = _scheduler_preflight._path_is_under_any
_slurm_template_allowlist_check = _scheduler_preflight._slurm_template_allowlist_check
_slurm_env_check = _scheduler_preflight._slurm_env_check
_production_slurm_env = _scheduler_preflight._production_slurm_env


_scheduler_gateway_module = importlib.import_module("services.orchestrator.scheduler_gateway")
_SCHEDULER_GATEWAY_FORWARDER_NAMES = (
    "_slurm_preflight",
    "_slurm_gateway_backend",
    "_default_gateway_probe",
    "_slurm_gateway_check",
)
_GATEWAY_SELF_HOSTS = _scheduler_gateway_module._GATEWAY_SELF_HOSTS
_GATEWAY_HEALTH_PATH = _scheduler_gateway_module._GATEWAY_HEALTH_PATH
_GATEWAY_REQUIRED_BINARIES = _scheduler_gateway_module._GATEWAY_REQUIRED_BINARIES
_GATEWAY_PROBE_TIMEOUT_SECONDS = _scheduler_gateway_module._GATEWAY_PROBE_TIMEOUT_SECONDS


def _scheduler_gateway_forwarder(name: str) -> Callable[..., Any]:
    def forwarder(*args: Any, **kwargs: Any) -> Any:
        return getattr(_scheduler_gateway_module, name)(*args, **kwargs)

    return forwarder


_slurm_preflight = _scheduler_gateway_forwarder("_slurm_preflight")
_slurm_gateway_backend = _scheduler_gateway_forwarder("_slurm_gateway_backend")
_default_gateway_probe = _scheduler_gateway_forwarder("_default_gateway_probe")
_slurm_gateway_check = _scheduler_gateway_forwarder("_slurm_gateway_check")


def _scheduler_cancellation_status(cancelled_jobs: Sequence[Mapping[str, Any]]) -> str:
    if not cancelled_jobs:
        return "blocked"
    cancelled_count = 0
    for job in cancelled_jobs:
        status = str(job.get("status") or "").lower()
        if job.get("error_code") or job.get("cancellation_proven") is False or status != "cancelled":
            continue
        cancelled_count += 1
    if cancelled_count == len(cancelled_jobs):
        return "cancelled"
    if cancelled_count:
        return "partially_cancelled"
    return "blocked"


def _cancelled_job_pipeline_status_write(job: Mapping[str, Any]) -> bool:
    status = str(job.get("status") or "").lower()
    return status == "cancelled" and job.get("cancellation_proven") is not False and not job.get("error_code")


def _cancelled_job_pipeline_event_write(job: Mapping[str, Any]) -> bool:
    if _cancelled_job_pipeline_status_write(job):
        return True
    return job.get("cancellation_proven") is False and str(job.get("error_code") or "") == "JOB_ALREADY_TERMINAL"


def _scheduler_pass_status_from_cancellation(cancellation_evidence: Sequence[Mapping[str, Any]]) -> str:
    return _scheduler_evidence.scheduler_pass_status_from_cancellation(cancellation_evidence)


_slurm_status_sync_failed_evidence = _scheduler_candidates._slurm_status_sync_failed_evidence


def _scheduler_evidence_forwarder(name: str, **default_kwargs: Any) -> Callable[..., Any]:
    def forwarder(*args: Any, **kwargs: Any) -> Any:
        return getattr(_scheduler_evidence, name)(*args, **{**default_kwargs, **kwargs})

    return forwarder


_scheduler_execution_boundary_from_cancellation = _scheduler_evidence_forwarder(
    "scheduler_execution_boundary_from_cancellation"
)
_slurm_status_sync_proof = _scheduler_evidence_forwarder(
    "slurm_status_sync_proof",
    sync_required=False,
    reservation=None,
    blocked=False,
)
_slurm_status_sync_proof_from_candidates = _scheduler_evidence_forwarder("slurm_status_sync_proof_from_candidates")
_execution_write_proof = _scheduler_evidence_forwarder(
    "execution_write_proof",
    reservation=None,
    execution_required=False,
    blocked=False,
)
_execution_write_proof_from_evidence = _scheduler_evidence_forwarder("execution_write_proof_from_evidence")
_slurm_cancellation_proof = _scheduler_evidence_forwarder(
    "slurm_cancellation_proof",
    cancellation_required=False,
    reservation=None,
    blocked=False,
)
_slurm_cancellation_proof_from_evidence = _scheduler_evidence_forwarder("slurm_cancellation_proof_from_evidence")
_slurm_status_sync_count = _scheduler_evidence_forwarder("slurm_status_sync_count")
_slurm_status_sync_unknown_count = _scheduler_evidence_forwarder("slurm_status_sync_unknown_count")
_slurm_status_sync_mutated = _scheduler_evidence_forwarder("slurm_status_sync_mutated")
_slurm_status_sync_failed = _scheduler_evidence_forwarder("slurm_status_sync_failed")
_slurm_cancelled_count = _scheduler_evidence_forwarder("slurm_cancelled_count")
_slurm_cancellation_blocked_count = _scheduler_evidence_forwarder("slurm_cancellation_blocked_count")
_slurm_cancellation_unknown_count = _scheduler_evidence_forwarder("slurm_cancellation_unknown_count")
_scheduler_mutation_proof = _scheduler_evidence_forwarder("scheduler_mutation_proof")
_proof_mutation_value = _scheduler_evidence_forwarder("proof_mutation_value")
_named_proof_value = _scheduler_evidence_forwarder("named_proof_value")
_slurm_submit_proof_value = _scheduler_evidence_forwarder("slurm_submit_proof_value")
_pipeline_status_write_proof_value = _scheduler_evidence_forwarder("pipeline_status_write_proof_value")
_pipeline_event_write_proof_value = _scheduler_evidence_forwarder("pipeline_event_write_proof_value")
_merge_proof_values = _scheduler_evidence_forwarder("merge_proof_values")
_positive_count = _scheduler_evidence_forwarder("positive_count")


_nested_bool = _scheduler_candidate_quality_forwarder("_nested_bool")


def _empty_counts() -> dict[str, int]:
    return _scheduler_evidence.empty_counts()


_SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_OWNER_NAMES = MappingProxyType(
    {
        "_scheduler_pass_status_from_cancellation": "scheduler_pass_status_from_cancellation",
        "_scheduler_execution_boundary_from_cancellation": "scheduler_execution_boundary_from_cancellation",
        "_slurm_status_sync_proof": "slurm_status_sync_proof",
        "_slurm_status_sync_proof_from_candidates": "slurm_status_sync_proof_from_candidates",
        "_slurm_cancellation_proof": "slurm_cancellation_proof",
        "_slurm_cancellation_proof_from_evidence": "slurm_cancellation_proof_from_evidence",
        "_slurm_status_sync_count": "slurm_status_sync_count",
        "_slurm_status_sync_unknown_count": "slurm_status_sync_unknown_count",
        "_slurm_status_sync_mutated": "slurm_status_sync_mutated",
        "_slurm_status_sync_failed": "slurm_status_sync_failed",
        "_slurm_cancelled_count": "slurm_cancelled_count",
        "_slurm_cancellation_blocked_count": "slurm_cancellation_blocked_count",
        "_slurm_cancellation_unknown_count": "slurm_cancellation_unknown_count",
        "_scheduler_mutation_proof": "scheduler_mutation_proof",
        "_proof_mutation_value": "proof_mutation_value",
        "_named_proof_value": "named_proof_value",
        "_slurm_submit_proof_value": "slurm_submit_proof_value",
        "_pipeline_status_write_proof_value": "pipeline_status_write_proof_value",
        "_pipeline_event_write_proof_value": "pipeline_event_write_proof_value",
        "_merge_proof_values": "merge_proof_values",
        "_positive_count": "positive_count",
        "_empty_counts": "empty_counts",
    }
)
_SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_NAMES = tuple(_SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_OWNER_NAMES)
_SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_OWNER_NAMES = MappingProxyType(
    {"_slurm_status_sync_failed_evidence": "_slurm_status_sync_failed_evidence"}
)
_SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_NAMES = tuple(
    _SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_OWNER_NAMES
)
_SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_METHOD_NAMES = (
    "_cancel_requested_active_slurm",
    "_cancel_orchestrator_for",
)
_SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_FUNCTION_NAMES = (
    "_scheduler_cancellation_status",
    "_cancelled_job_pipeline_status_write",
    "_cancelled_job_pipeline_event_write",
    "_execution_mutation_value",
)
_SCHEDULER_CANCELLATION_STATUS_COMPAT_OWNER_MISSING = (
    *tuple(
        owner_name
        for owner_name in _SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_OWNER_NAMES.values()
        if not hasattr(_scheduler_evidence, owner_name)
    ),
    *tuple(
        owner_name
        for owner_name in _SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_OWNER_NAMES.values()
        if not hasattr(_scheduler_candidates, owner_name)
    ),
)
_SCHEDULER_CANCELLATION_STATUS_COMPAT_FACADE_MISSING = (
    *tuple(name for name in _SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_NAMES if name not in globals()),
    *tuple(name for name in _SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_NAMES if name not in globals()),
    *tuple(
        name
        for name in _SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_METHOD_NAMES
        if not hasattr(ProductionScheduler, name)
    ),
    *tuple(
        name for name in _SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_FUNCTION_NAMES if name not in globals()
    ),
)
_SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_OVERLAP = tuple(
    sorted(
        {
            *_SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_METHOD_NAMES,
            *_SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_FUNCTION_NAMES,
        }
        & {
            *_SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_NAMES,
            *_SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_NAMES,
        }
    )
)
if _SCHEDULER_CANCELLATION_STATUS_COMPAT_OWNER_MISSING:
    raise RuntimeError(
        "scheduler cancellation/status compatibility names missing from owner module: "
        f"{', '.join(_SCHEDULER_CANCELLATION_STATUS_COMPAT_OWNER_MISSING)}"
    )
if _SCHEDULER_CANCELLATION_STATUS_COMPAT_FACADE_MISSING:
    raise RuntimeError(
        "scheduler cancellation/status compatibility names missing from facade: "
        f"{', '.join(_SCHEDULER_CANCELLATION_STATUS_COMPAT_FACADE_MISSING)}"
    )
if _SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_OVERLAP:
    raise RuntimeError(
        "scheduler cancellation/status retained local glue entered pure compatibility owner maps: "
        f"{', '.join(_SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_OVERLAP)}"
    )
_SCHEDULER_CANCELLATION_STATUS_COMPAT_OWNER_WRAPPERS = MappingProxyType(
    {
        facade_name: getattr(_scheduler_evidence, owner_name)
        for facade_name, owner_name in _SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_OWNER_NAMES.items()
    }
)
_SCHEDULER_CANCELLATION_STATUS_COMPAT_FACADE_WRAPPERS = MappingProxyType(
    {name: globals()[name] for name in _SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_NAMES}
)
_SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_OWNER_ALIASES = MappingProxyType(
    {
        facade_name: getattr(_scheduler_candidates, owner_name)
        for facade_name, owner_name in _SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_OWNER_NAMES.items()
    }
)
_SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_FACADE_ALIASES = MappingProxyType(
    {name: globals()[name] for name in _SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_NAMES}
)
for (
    _scheduler_cancellation_status_alias_name,
    _scheduler_cancellation_status_owner_value,
) in _SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_OWNER_ALIASES.items():
    if (
        _SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_FACADE_ALIASES[_scheduler_cancellation_status_alias_name]
        is not _scheduler_cancellation_status_owner_value
    ):
        raise RuntimeError(
            "scheduler cancellation/status candidate alias drifted from owner module: "
            f"{_scheduler_cancellation_status_alias_name}"
        )
del _scheduler_cancellation_status_alias_name, _scheduler_cancellation_status_owner_value


def _no_mutation_proof() -> dict[str, bool]:
    return _scheduler_evidence.no_mutation_proof()


_scheduler_runtime_roots_module = importlib.import_module("services.orchestrator.scheduler_runtime_roots")
_SCHEDULER_RUNTIME_ROOTS_FORWARDER_NAMES = (
    "_scheduler_lock_evidence_root_preflight",
    "_scheduler_runtime_root_preflight",
    "_scheduler_root_preflight_not_required",
    "_scheduler_root_preflight_payload",
    "_scheduler_root_check",
    "_scheduler_root_blocker",
    "_scheduler_root_os_error_reason",
    "_directory_is_writable",
    "_scheduler_service_role_check",
    "_scheduler_allowed_roots_policy_check",
    "_scheduler_allowed_roots",
    "_normalize_sources",
    "_confined_path",
    "_reject_blank_config_path",
    "_optional_config_path",
    "_config_path_preserve_final_component",
    "_config_path_relative_to_preserve_final",
    "_optional_config_path_relative_to_preserve_final",
    "_resolve_optional_config_path",
    "_optional_config_path_relative_to",
    "_env_flag",
    "_env_int",
    "_env_allowed_cycle_hours_utc",
    "_parse_allowed_cycle_hours_utc",
    "_normalize_allowed_cycle_hours_utc",
    "_env_path_list",
    "_require_under_workspace",
    "_require_safe_directory_final_component",
)


def _scheduler_runtime_roots_forwarder(name: str) -> Callable[..., Any]:
    def forwarder(*args: Any, **kwargs: Any) -> Any:
        return getattr(_scheduler_runtime_roots_module, name)(*args, **kwargs)

    return forwarder


_scheduler_lock_evidence_root_preflight = _scheduler_runtime_roots_forwarder("_scheduler_lock_evidence_root_preflight")
_scheduler_runtime_root_preflight = _scheduler_runtime_roots_forwarder("_scheduler_runtime_root_preflight")
_scheduler_root_preflight_not_required = _scheduler_runtime_roots_forwarder("_scheduler_root_preflight_not_required")
_scheduler_root_preflight_payload = _scheduler_runtime_roots_forwarder("_scheduler_root_preflight_payload")
_scheduler_root_check = _scheduler_runtime_roots_forwarder("_scheduler_root_check")
_scheduler_root_blocker = _scheduler_runtime_roots_forwarder("_scheduler_root_blocker")
_scheduler_root_os_error_reason = _scheduler_runtime_roots_forwarder("_scheduler_root_os_error_reason")
_directory_is_writable = _scheduler_runtime_roots_forwarder("_directory_is_writable")
_scheduler_service_role_check = _scheduler_runtime_roots_forwarder("_scheduler_service_role_check")
_scheduler_allowed_roots_policy_check = _scheduler_runtime_roots_forwarder("_scheduler_allowed_roots_policy_check")
_scheduler_allowed_roots = _scheduler_runtime_roots_forwarder("_scheduler_allowed_roots")
_normalize_sources = _scheduler_runtime_roots_forwarder("_normalize_sources")
_confined_path = _scheduler_runtime_roots_forwarder("_confined_path")
_reject_blank_config_path = _scheduler_runtime_roots_forwarder("_reject_blank_config_path")
_optional_config_path = _scheduler_runtime_roots_forwarder("_optional_config_path")
_config_path_preserve_final_component = _scheduler_runtime_roots_forwarder("_config_path_preserve_final_component")
_config_path_relative_to_preserve_final = _scheduler_runtime_roots_forwarder("_config_path_relative_to_preserve_final")
_optional_config_path_relative_to_preserve_final = _scheduler_runtime_roots_forwarder(
    "_optional_config_path_relative_to_preserve_final"
)
_resolve_optional_config_path = _scheduler_runtime_roots_forwarder("_resolve_optional_config_path")
_optional_config_path_relative_to = _scheduler_runtime_roots_forwarder("_optional_config_path_relative_to")
_env_flag = _scheduler_runtime_roots_forwarder("_env_flag")
_env_int = _scheduler_runtime_roots_forwarder("_env_int")
_env_allowed_cycle_hours_utc = _scheduler_runtime_roots_forwarder("_env_allowed_cycle_hours_utc")
_parse_allowed_cycle_hours_utc = _scheduler_runtime_roots_forwarder("_parse_allowed_cycle_hours_utc")
_normalize_allowed_cycle_hours_utc = _scheduler_runtime_roots_forwarder("_normalize_allowed_cycle_hours_utc")
_env_path_list = _scheduler_runtime_roots_forwarder("_env_path_list")
_require_under_workspace = _scheduler_runtime_roots_forwarder("_require_under_workspace")
_require_safe_directory_final_component = _scheduler_runtime_roots_forwarder("_require_safe_directory_final_component")


def _evidence_reservation_blocked_payload(
    *,
    config: Any | None = None,
    pass_id: str,
    artifact_path: Path,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return _scheduler_evidence.evidence_reservation_blocked_payload(
        config=config or type("SchedulerEvidenceConfigShim", (), {"scheduler_db_free_required": False})(),
        pass_id=pass_id,
        artifact_path=artifact_path,
        reason=reason,
        details=details,
        evidence_safe=_evidence_safe,
    )


def _evidence_write_error_payload(error: OSError, config: Any | None = None) -> dict[str, Any]:
    return _scheduler_evidence.evidence_write_error_payload(error, config)


def _scheduler_resolved_runtime_roots(config: ProductionSchedulerConfig) -> dict[str, Any]:
    return _scheduler_evidence.scheduler_resolved_runtime_roots(config)


def _root_evidence_item(
    value: Path | str | None,
    *,
    env: str,
    required: bool,
    fallback: str | None = None,
) -> dict[str, Any]:
    return _scheduler_evidence.root_evidence_item(value, env=env, required=required, fallback=fallback)


def _scheduler_runtime_config_evidence(config: ProductionSchedulerConfig) -> dict[str, Any]:
    return _scheduler_evidence.scheduler_runtime_config_evidence(config)


def _scheduler_pass_status_from_execution(execution_evidence: Sequence[Mapping[str, Any]]) -> str:
    if not execution_evidence:
        return "planned"
    if all(str(item.get("status")) == "preflight_blocked" for item in execution_evidence):
        return "preflight_blocked"
    if any(item.get("submitted") is True for item in execution_evidence):
        if _scheduler_partial_count_from_execution(execution_evidence) > 0:
            return "submitted_partial"
        return "submitted"
    if any(str(item.get("status")) in {"blocked", "failed"} for item in execution_evidence):
        return "preflight_blocked"
    return str(execution_evidence[-1].get("status") or "planned")


def _blocked_pass_status(blocked_candidates: Sequence[SchedulerCandidate]) -> str:
    reasons = {str(candidate.reason or "") for candidate in blocked_candidates}
    if any("unavailable" in reason for reason in reasons):
        return "unavailable"
    return "blocked"


def _scheduler_partial_count_from_execution(execution_evidence: Sequence[Mapping[str, Any]]) -> int:
    if not any(item.get("submitted") is True or item.get("execution_attempted") is True for item in execution_evidence):
        return 0
    return sum(1 for item in execution_evidence if _is_partial_candidate_evidence(item))


def _scheduler_failed_count_from_execution(execution_evidence: Sequence[Mapping[str, Any]]) -> int:
    return sum(1 for item in execution_evidence if _is_failed_candidate_evidence(item))


def _is_failed_candidate_evidence(item: Mapping[str, Any]) -> bool:
    return _is_failed_model_run_status(str(item.get("status") or ""))


def _is_partial_candidate_evidence(item: Mapping[str, Any]) -> bool:
    status = str(item.get("status") or "")
    if item.get("submitted") is True:
        return _is_non_submitted_terminal_or_unavailable_status(status)
    return _is_non_submitted_terminal_or_unavailable_status(status) or status.endswith("_partial")


def _is_failed_model_run_status(status: str) -> bool:
    normalized = status.strip().lower()
    return normalized in {"failed", "permanently_failed", "submission_failed"} or normalized.endswith("_failed")


def _is_non_submitted_terminal_or_unavailable_status(status: str) -> bool:
    from services.orchestrator import scheduler_candidate_quality

    return scheduler_candidate_quality._is_non_submitted_terminal_or_unavailable_status(status)


def _empty_model_discovery() -> dict[str, Any]:
    return {
        "active_model_count": 0,
        "runnable_model_count": 0,
        "selected_model_count": 0,
        "excluded_model_count": 0,
        "models": [],
        "exclusions": [],
        "operator_filters": {"expression": None, "excluded_runnable_count": 0},
    }


def _open_evidence_directory(evidence_dir: Path, workspace_root: Path) -> int:
    return _scheduler_evidence.open_evidence_directory(evidence_dir, workspace_root)


def _write_new_regular_file(
    artifact_name: str,
    serialized: str,
    *,
    dir_fd: int,
    artifact_path: Path,
) -> None:
    _scheduler_evidence.write_new_regular_file(
        artifact_name,
        serialized,
        dir_fd=dir_fd,
        artifact_path=artifact_path,
    )


def _require_evidence_artifact_available(
    artifact_name: str,
    *,
    dir_fd: int,
    artifact_path: Path,
) -> None:
    _scheduler_evidence.require_evidence_artifact_available(
        artifact_name,
        dir_fd=dir_fd,
        artifact_path=artifact_path,
    )


_source_horizon_metadata = _scheduler_discovery.source_horizon_metadata

EXPORTS = {
    name: value
    for name, value in globals().items()
    if name
    in (
        "_GATEWAY_HEALTH_PATH",
        "_GATEWAY_PROBE_TIMEOUT_SECONDS",
        "_GATEWAY_REQUIRED_BINARIES",
        "_GATEWAY_SELF_HOSTS",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_NAMES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_ALIAS_OWNER_NAMES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_FACADE_ALIASES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_CANDIDATE_OWNER_ALIASES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_FACADE_MISSING",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_FACADE_WRAPPERS",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_OWNER_MISSING",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_OWNER_WRAPPERS",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_FUNCTION_NAMES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_METHOD_NAMES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_RETAINED_LOCAL_OVERLAP",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_NAMES",
        "_SCHEDULER_CANCELLATION_STATUS_COMPAT_WRAPPER_OWNER_NAMES",
        "_SCHEDULER_CANDIDATE_EXECUTION_EVIDENCE_FORWARDER_NAMES",
        "_SCHEDULER_CANDIDATE_QUALITY_FORWARDER_NAMES",
        "_SCHEDULER_GATEWAY_FORWARDER_NAMES",
        "_SCHEDULER_RUNTIME_ROOTS_FORWARDER_NAMES",
        "_blocked_pass_status",
        "_cancel_candidate_evidence_write_blocked_evidence",
        "_cancelled_job_pipeline_event_write",
        "_cancelled_job_pipeline_status_write",
        "_candidate_artifact_refs",
        "_candidate_display_evidence",
        "_candidate_evidence_write_blocked_evidence",
        "_candidate_execution_attempted",
        "_candidate_execution_evidence",
        "_candidate_execution_evidence_item",
        "_candidate_forcing_blocked_evidence",
        "_candidate_forcing_evidence",
        "_candidate_forcing_ready_evidence",
        "_candidate_frequency_evidence",
        "_candidate_identity_evidence",
        "_candidate_model_run_review_evidence",
        "_candidate_output_evidence",
        "_candidate_output_key",
        "_candidate_output_river_manifest",
        "_candidate_output_uri",
        "_candidate_pipeline_write_value",
        "_candidate_preflight_blocked_evidence",
        "_candidate_product_counts",
        "_candidate_quality_states",
        "_candidate_residual_blockers",
        "_candidate_resource_summary",
        "_candidate_secret_manifest_blocked_evidence",
        "_candidate_slurm_preflight_blocked_evidence",
        "_candidate_slurm_submit_called",
        "_candidate_stage_evidence",
        "_candidate_stage_evidence_item",
        "_candidate_station_count",
        "_candidate_station_ids",
        "_candidate_status_from_outcome",
        "_config_path_preserve_final_component",
        "_config_path_relative_to_preserve_final",
        "_confined_path",
        "_database_host",
        "_database_host_ip_address",
        "_database_host_is_local",
        "_database_host_is_unsafe",
        "_database_url_blocker",
        "_default_gateway_probe",
        "_default_grib_system_eccodes_probe",
        "_directory_is_writable",
        "_empty_counts",
        "_empty_model_discovery",
        "_env_allowed_cycle_hours_utc",
        "_env_flag",
        "_env_int",
        "_env_path_list",
        "_evidence_reservation_blocked_payload",
        "_evidence_write_error_payload",
        "_execution_mutation_value",
        "_execution_write_proof",
        "_execution_write_proof_from_evidence",
        "_first_present_int",
        "_first_present_value",
        "_gateway_endpoint",
        "_gateway_self_reference_blocker",
        "_has_uri_scheme",
        "_in_process_gateway_probe",
        "_interpret_gateway_health",
        "_is_digest_evidence_key",
        "_is_failed_candidate_evidence",
        "_is_failed_model_run_status",
        "_is_ipv4_number_part",
        "_is_non_submitted_terminal_or_unavailable_status",
        "_is_noncanonical_ipv4_part",
        "_is_noncanonical_numeric_ipv4_host",
        "_is_numeric_ipv4_like_host",
        "_is_partial_candidate_evidence",
        "_is_unsafe_numeric_ipv4_like_host",
        "_merge_proof_values",
        "_model_package_manifest_uri",
        "_named_proof_value",
        "_nested_bool",
        "_no_mutation_proof",
        "_nonempty_evidence_value",
        "_normalize_allowed_cycle_hours_utc",
        "_normalize_database_host",
        "_normalize_sources",
        "_normalized_identity",
        "_open_evidence_directory",
        "_optional_config_path",
        "_optional_config_path_relative_to",
        "_optional_config_path_relative_to_preserve_final",
        "_parse_allowed_cycle_hours_utc",
        "_parse_noncanonical_ipv4_address",
        "_path_is_under_any",
        "_pipeline_event_write_proof_value",
        "_pipeline_result_has_pipeline_job_evidence",
        "_pipeline_result_pipeline_event_write",
        "_pipeline_result_pipeline_status_write",
        "_pipeline_result_pipeline_write_value",
        "_pipeline_result_slurm_submit_called",
        "_pipeline_result_write_absence_proven",
        "_pipeline_status_write_proof_value",
        "_positive_count",
        "_preflight_allowed_roots",
        "_production_slurm_env",
        "_proof_mutation_value",
        "_redact_digest_keys_for_evidence",
        "_reject_blank_config_path",
        "_require_evidence_artifact_available",
        "_require_safe_directory_final_component",
        "_require_under_workspace",
        "_resolve_optional_config_path",
        "_resource_metrics_from_mapping",
        "_resource_profile_evidence",
        "_root_evidence_item",
        "_scheduler_allowed_roots",
        "_scheduler_allowed_roots_policy_check",
        "_scheduler_cancellation_status",
        "_scheduler_candidate_execution_evidence_forwarder",
        "_scheduler_candidate_quality_forwarder",
        "_scheduler_evidence_forwarder",
        "_scheduler_execution_boundary_from_cancellation",
        "_scheduler_failed_count_from_execution",
        "_scheduler_gateway_forwarder",
        "_scheduler_gateway_module",
        "_scheduler_grib_env_root",
        "_scheduler_lock_evidence_root_preflight",
        "_scheduler_mutation_proof",
        "_scheduler_partial_count_from_execution",
        "_scheduler_pass_status_from_cancellation",
        "_scheduler_pass_status_from_execution",
        "_scheduler_resolved_runtime_roots",
        "_scheduler_root_blocker",
        "_scheduler_root_check",
        "_scheduler_root_os_error_reason",
        "_scheduler_root_preflight_not_required",
        "_scheduler_root_preflight_payload",
        "_scheduler_runtime_config_evidence",
        "_scheduler_runtime_root_preflight",
        "_scheduler_runtime_roots_forwarder",
        "_scheduler_runtime_roots_module",
        "_scheduler_service_role_check",
        "_scheduler_shud_executable",
        "_slurm_cancellation_blocked_count",
        "_slurm_cancellation_proof",
        "_slurm_cancellation_proof_from_evidence",
        "_slurm_cancellation_unknown_count",
        "_slurm_cancelled_count",
        "_slurm_env_check",
        "_slurm_gateway_backend",
        "_slurm_gateway_check",
        "_slurm_grib_env_check",
        "_slurm_preflight",
        "_slurm_resource_profile_blockers",
        "_slurm_shud_executable_check",
        "_slurm_status_sync_count",
        "_slurm_status_sync_failed",
        "_slurm_status_sync_failed_evidence",
        "_slurm_status_sync_mutated",
        "_slurm_status_sync_proof",
        "_slurm_status_sync_proof_from_candidates",
        "_slurm_status_sync_unknown_count",
        "_slurm_submit_proof_value",
        "_slurm_template_allowlist_check",
        "_source_horizon_metadata",
        "_stage_run_evidence",
        "_stage_task_results",
        "_storage_root_check",
        "_sync_candidate_evidence_write_blocked_evidence",
        "_task_candidate_matching_available",
        "_task_result_evidence",
        "_task_result_matches_candidate",
        "_write_new_regular_file",
    )
}
