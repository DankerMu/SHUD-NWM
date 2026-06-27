from __future__ import annotations

import importlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from threading import RLock
from types import FunctionType, MappingProxyType
from typing import Any
from uuid import uuid4 as _uuid4

from packages.common.model_registry import PsycopgModelRegistryStore
from packages.common.redaction import redact_payload  # noqa: F401
from packages.common.slurm_env import (
    iter_secret_manifest_findings,
)
from packages.common.source_identity import normalize_source_id
from packages.common.state_manager import StateManager
from services.orchestrator import scheduler_candidates as _scheduler_candidates
from services.orchestrator import scheduler_discovery as _scheduler_discovery
from services.orchestrator import scheduler_evidence as _scheduler_evidence
from services.orchestrator import scheduler_execution as _scheduler_execution
from services.orchestrator import scheduler_lease as _scheduler_lease_module
from services.orchestrator import scheduler_models as _scheduler_models
from services.orchestrator import scheduler_preflight as _scheduler_preflight
from services.orchestrator import scheduler_state as _scheduler_state_module
from services.orchestrator.chain import (
    ForecastOrchestrator,
    OrchestratorConfig,
    _retry_service_from_env,
    scenario_for_source,
)
from services.orchestrator.reservation import (
    SubmitOverlapReceipt,
    run_concurrent_submissions,
    timed_submission,
)
from services.orchestrator.scheduler_adapters import (  # noqa: F401
    _CANONICAL_READINESS_PROVIDER_UNSET,
    ActiveCandidateRepository,
    CanonicalReadinessProvider,
    CycleDiscoveryAdapter,
    ForcingProducerRunner,
    ModelRegistryReader,
    ProductionOrchestratorFactory,
    _active_repository_from_env,
    _canonical_readiness_provider_from_env,
    _default_adapters,
    _forcing_producer_from_env,
    _MetStoreCanonicalReadinessProvider,
    _orchestrator_repository_from_env,
    _UnavailableCanonicalReadinessProvider,
)
from services.orchestrator.scheduler_lease import (  # noqa: F401
    LOCK_OWNER,
    LOCK_SCHEMA_VERSION,
    MAX_LOCK_PAYLOAD_BYTES,
    RECONCILE_DB_CONNECT_TIMEOUT_SECONDS,
    FileSchedulerLease,
    PostgresSchedulerLease,
    UnsafeSchedulerLockError,
    _default_owner_liveness_probe,
    _LeaseHeartbeat,
    _open_lock_parent_directory,
    _open_regular_guard_file,
    _postgres_advisory_lock_key,
    _unlink_lock_file,
)
from services.orchestrator.scheduler_state import (  # noqa: F401
    ACTIVE_HYDRO_STATUSES,
    ACTIVE_PIPELINE_STATUSES,
    CANDIDATE_STATE_TASK_RESULT_LIMIT,
    DEFAULT_CANDIDATE_STATE_EVENT_LIMIT,
    DEFAULT_CANDIDATE_STATE_JOB_LIMIT,
    DEFAULT_RETRY_LIMIT,
    DOWNSTREAM_RESTART_STAGES,
    DOWNSTREAM_STAGE_ALIASES,
    DURABLE_HYDRO_SUCCESS_STATUSES,
    FAILED_PIPELINE_STATUSES,
    NATIVE_SHUD_STAGE_ALIASES,
    STATE_CANDIDATE_SCOPED_PROOF_FIELDS,
    STATE_M23_COMPARISON_FIELDS,
    STATE_STRONG_CANDIDATE_SCOPED_PROOF_FIELDS,
    TERMINAL_PIPELINE_SUCCESS_STATUSES,
    TRANSIENT_RETRY_REASON_CODES,
    CandidateStateDecision,
    _bounded_active_slurm_jobs,
    _bounded_candidate_event,
    _bounded_candidate_state,
    _bounded_task_result_rows,
    _bounded_task_result_sample,
    _call_active_slurm_jobs_provider,
    _call_candidate_state_provider,
    _cancelled_state_evidence,
    _candidate_canonical_product_id,
    _candidate_contract_pipeline_job_id,
    _candidate_identity_from_evidence,
    _candidate_production_identity,
    _candidate_published_manifest_id,
    _candidate_repaired_state_audit_evidence,
    _candidate_scoped_shared_cycle_aggregate_state,
    _candidate_scoped_shared_cycle_events,
    _candidate_state_decision,
    _candidate_state_decision_event,
    _candidate_state_decision_state,
    _candidate_state_evidence,
    _candidate_state_filtered_decision_state,
    _candidate_state_has_identity_mismatch,
    _candidate_state_identity_validation,
    _candidate_state_is_candidate_scoped_retry,
    _candidate_state_source_allows_nested_authority,
    _candidate_state_source_has_authoritative_ancestor,
    _canonical_downstream_stage,
    _coerce_int,
    _coerce_mapping_for_state,
    _coerce_optional_nonnegative_int,
    _downstream_failure_restartable,
    _downstream_retry_evidence,
    _durable_shud_output_exists,
    _ensure_utc,
    _event_has_candidate_scoped_failure,
    _event_has_failure_signal,
    _event_identity_containers,
    _event_is_manual_retry_marker,
    _evidence_safe,
    _failed_stage,
    _failure_policy_payload,
    _first_nested_state_value,
    _first_nonempty,
    _first_state_datetime,
    _first_state_int,
    _force_native_shud_rerun,
    _forecast_cycle_manifest_uri,
    _format_utc,
    _global_source_cycle_download_blocker_job,
    _has_candidate_task_failure,
    _has_successful_download_stage,
    _inconclusive_source_cycle_decision_state,
    _inconclusive_source_cycle_unresolved_job_ids,
    _is_raw_manifest_object_uri,
    _is_source_cycle_download_stage,
    _job_is_unsubmitted_auto_retry_placeholder,
    _job_state_evidence,
    _job_terminal_sort_key,
    _job_terminal_time,
    _latest_failed_job_for_stage,
    _latest_failure_truth_timestamp,
    _latest_manual_retry_blocker,
    _latest_manual_retry_marker,
    _latest_successful_download_stage,
    _legacy_compatible_state_row,
    _legacy_identity_values,
    _legacy_non_authoritative_state_row,
    _legacy_values_prove_same_candidate,
    _looks_like_production_job_id,
    _manual_retry_blocker_record,
    _manual_retry_blocking_hydro_status,
    _manual_retry_blocking_pipeline_status,
    _manual_retry_marker_bound_to_blocker,
    _manual_retry_marker_overrides_blocker,
    _manual_retry_marker_record,
    _manual_retry_marker_repairs_historical_failure,
    _manual_retry_markers,
    _manual_retry_new_attempt,
    _manual_retry_payload,
    _manual_retry_requested,
    _manual_retry_state_evidence,
    _missing_raw_manifest_repair_evidence,
    _nested_state_identity_payloads,
    _object_manifest_is_missing,
    _optional_mapping_state,
    _parse_state_datetime,
    _permanent_failure_evidence,
    _permanent_reason,
    _pipeline_job_is_repaired_stage_evidence,
    _pipeline_terminal_success_is_candidate_scoped,
    _prior_failure_reason,
    _redact_secret_manifest_for_evidence,
    _repaired_raw_manifest_downstream_retry_evidence,
    _repaired_stage_decision_state,
    _restore_top_level_source_cycle_download_blocker,
    _retry_failure_evidence,
    _shared_cycle_aggregate_has_candidate_failure,
    _shared_cycle_identity_values_match_candidate,
    _shared_cycle_row_is_candidate_scoped,
    _source_cycle_identity_matches_expected,
    _stage_cycle_run_matches_candidate,
    _state_active_jobs,
    _state_error_code,
    _state_error_message,
    _state_event_limit,
    _state_event_references_job_ids,
    _state_events,
    _state_has_failure_signal,
    _state_has_only_repaired_pipeline_failure_signal,
    _state_has_only_unsubmitted_auto_retry_placeholders,
    _state_job_limit,
    _state_jobs,
    _state_output_uri,
    _state_overflow_evidence,
    _state_retry_attempt,
    _state_retry_limit,
    _state_row_has_authoritative_candidate_proof,
    _state_row_has_m23_comparison_evidence,
    _state_row_has_m23_comparison_fields,
    _state_row_is_scoped_to_other_candidate,
    _state_row_references_job_ids,
    _state_status,
    _state_task_identity,
    _state_task_payload_failed,
    _state_truth_sequence,
    _state_truth_sort_key,
    _state_values_are_scoped_to_other_candidate,
    _state_values_have_authoritative_candidate_proof,
    _state_values_have_candidate_scoped_m23_proof,
    _state_values_have_complete_m23_identity,
    _strip_top_level_candidate_state_decision_fields,
    _strip_top_level_hydro_decision_fields,
    _strip_top_level_pipeline_decision_fields,
    _task_result_is_candidate_scoped,
    _terminal_hydro_truth_supersedes_failure,
    _top_level_source_cycle_download_blocker,
)
from workers.data_adapters.base import CycleDiscovery, cycle_id_for, format_cycle_time

_SCHEDULER_STATE_COMPAT_EXPORT_NAMES = tuple(
    name
    for name, value in globals().items()
    if name.startswith("_")
    and isinstance(value, FunctionType)
    and getattr(_scheduler_state_module, name, None) is value
)
_SCHEDULER_STATE_COMPAT_ORIGINALS = {
    name: getattr(_scheduler_state_module, name) for name in _SCHEDULER_STATE_COMPAT_EXPORT_NAMES
}
_SCHEDULER_STATE_COMPAT_WRAPPERS: dict[str, Callable[..., Any]] = {}
_SCHEDULER_STATE_COMPAT_LOCK = RLock()


def _scheduler_state_compat_override(name: str, original: Any) -> Any:
    value = globals().get(name, original)
    wrapper = _SCHEDULER_STATE_COMPAT_WRAPPERS.get(name)
    if wrapper is not None:
        return None if value is wrapper else value
    return None if value is original else value


@contextmanager
def _scheduler_state_compat_bindings() -> Any:
    """Expose old scheduler.py monkeypatches to moved scheduler_state helpers."""

    with _SCHEDULER_STATE_COMPAT_LOCK:
        previous: dict[str, Any] = {}
        for name, original in _SCHEDULER_STATE_COMPAT_ORIGINALS.items():
            override = _scheduler_state_compat_override(name, original)
            if override is None:
                continue
            previous[name] = getattr(_scheduler_state_module, name)
            setattr(_scheduler_state_module, name, override)
        try:
            yield
        finally:
            for name, value in previous.items():
                setattr(_scheduler_state_module, name, value)


def _scheduler_state_compat_wrapper(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(original)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with _scheduler_state_compat_bindings():
            return original(*args, **kwargs)

    return wrapped


for _scheduler_state_compat_name, _scheduler_state_compat_original in _SCHEDULER_STATE_COMPAT_ORIGINALS.items():
    if not isinstance(_scheduler_state_compat_original, FunctionType):
        continue
    _SCHEDULER_STATE_COMPAT_WRAPPERS[_scheduler_state_compat_name] = _scheduler_state_compat_wrapper(
        _scheduler_state_compat_name,
        _scheduler_state_compat_original,
    )
    globals()[_scheduler_state_compat_name] = _SCHEDULER_STATE_COMPAT_WRAPPERS[_scheduler_state_compat_name]
del _scheduler_state_compat_name, _scheduler_state_compat_original

# Compatibility re-exports for downstream imports and monkeypatch paths that
# still target services.orchestrator.scheduler after candidate-state extraction.
_SCHEDULER_STATE_COMPAT_WRAPPER_NAMES = _SCHEDULER_STATE_COMPAT_EXPORT_NAMES
_SCHEDULER_STATE_COMPAT_REEXPORT_NAMES = (
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
_SCHEDULER_STATE_COMPAT_REEXPORT_MISSING = tuple(
    name for name in _SCHEDULER_STATE_COMPAT_REEXPORT_NAMES if not hasattr(_scheduler_state_module, name)
)
if _SCHEDULER_STATE_COMPAT_REEXPORT_MISSING:
    raise RuntimeError(
        "scheduler state compatibility names missing from owner module: "
        f"{', '.join(_SCHEDULER_STATE_COMPAT_REEXPORT_MISSING)}"
    )
_SCHEDULER_STATE_COMPAT_OWNER_REEXPORTS = MappingProxyType(
    {name: getattr(_scheduler_state_module, name) for name in _SCHEDULER_STATE_COMPAT_REEXPORT_NAMES}
)
_SCHEDULER_STATE_COMPAT_FACADE_REEXPORTS = MappingProxyType(
    {name: globals()[name] for name in _SCHEDULER_STATE_COMPAT_REEXPORT_NAMES}
)
_SCHEDULER_STATE_COMPAT_REEXPORT_FUNCTION_NAMES = tuple(
    name
    for name in _SCHEDULER_STATE_COMPAT_REEXPORT_NAMES
    if name.startswith("_") and isinstance(_SCHEDULER_STATE_COMPAT_OWNER_REEXPORTS[name], FunctionType)
)
if set(_SCHEDULER_STATE_COMPAT_REEXPORT_FUNCTION_NAMES) != set(_SCHEDULER_STATE_COMPAT_WRAPPER_NAMES):
    raise RuntimeError("scheduler state compatibility wrapper names drifted from owner re-export names")
for _scheduler_state_direct_name, _scheduler_state_owner_value in _SCHEDULER_STATE_COMPAT_OWNER_REEXPORTS.items():
    if _scheduler_state_direct_name in _SCHEDULER_STATE_COMPAT_WRAPPER_NAMES:
        continue
    if _SCHEDULER_STATE_COMPAT_FACADE_REEXPORTS[_scheduler_state_direct_name] is not _scheduler_state_owner_value:
        raise RuntimeError(
            f"scheduler state direct re-export drifted from owner module: {_scheduler_state_direct_name}"
        )
del _scheduler_state_direct_name, _scheduler_state_owner_value
_SCHEDULER_STATE_COMPAT_EXPORTS = tuple(
    _SCHEDULER_STATE_COMPAT_FACADE_REEXPORTS[name] for name in _SCHEDULER_STATE_COMPAT_REEXPORT_NAMES
)

# Compatibility re-exports for downstream imports and monkeypatch paths that
# still target services.orchestrator.scheduler after lease extraction.
_SCHEDULER_LEASE_COMPAT_REEXPORT_NAMES = (
    "FileSchedulerLease",
    "LOCK_OWNER",
    "LOCK_SCHEMA_VERSION",
    "MAX_LOCK_PAYLOAD_BYTES",
    "PostgresSchedulerLease",
    "RECONCILE_DB_CONNECT_TIMEOUT_SECONDS",
    "UnsafeSchedulerLockError",
    "_LeaseHeartbeat",
    "_default_owner_liveness_probe",
    "_open_lock_parent_directory",
    "_open_regular_guard_file",
    "_postgres_advisory_lock_key",
    "_unlink_lock_file",
)
_SCHEDULER_LEASE_COMPAT_LOOKUP_NAMES = (
    "_default_owner_liveness_probe",
    "_unlink_lock_file",
    "_open_lock_parent_directory",
    "_open_regular_guard_file",
    "_postgres_advisory_lock_key",
)
_SCHEDULER_LEASE_COMPAT_REEXPORT_MISSING = tuple(
    name for name in _SCHEDULER_LEASE_COMPAT_REEXPORT_NAMES if not hasattr(_scheduler_lease_module, name)
)
_SCHEDULER_LEASE_COMPAT_FACADE_MISSING = tuple(
    name for name in _SCHEDULER_LEASE_COMPAT_REEXPORT_NAMES if name not in globals()
)
if _SCHEDULER_LEASE_COMPAT_REEXPORT_MISSING:
    raise RuntimeError(
        "scheduler lease compatibility names missing from owner module: "
        f"{', '.join(_SCHEDULER_LEASE_COMPAT_REEXPORT_MISSING)}"
    )
if _SCHEDULER_LEASE_COMPAT_FACADE_MISSING:
    raise RuntimeError(
        f"scheduler lease compatibility names missing from facade: {', '.join(_SCHEDULER_LEASE_COMPAT_FACADE_MISSING)}"
    )
_SCHEDULER_LEASE_COMPAT_OWNER_REEXPORTS = MappingProxyType(
    {name: getattr(_scheduler_lease_module, name) for name in _SCHEDULER_LEASE_COMPAT_REEXPORT_NAMES}
)
_SCHEDULER_LEASE_COMPAT_FACADE_REEXPORTS = MappingProxyType(
    {name: globals()[name] for name in _SCHEDULER_LEASE_COMPAT_REEXPORT_NAMES}
)
if set(getattr(_scheduler_lease_module, "__all__", ())) != set(_SCHEDULER_LEASE_COMPAT_REEXPORT_NAMES):
    raise RuntimeError("scheduler lease compatibility names drifted from owner __all__")
if not set(_SCHEDULER_LEASE_COMPAT_LOOKUP_NAMES).issubset(_SCHEDULER_LEASE_COMPAT_REEXPORT_NAMES):
    raise RuntimeError("scheduler lease compatibility lookup names are not fully re-exported")
for _scheduler_lease_direct_name, _scheduler_lease_owner_value in _SCHEDULER_LEASE_COMPAT_OWNER_REEXPORTS.items():
    if _SCHEDULER_LEASE_COMPAT_FACADE_REEXPORTS[_scheduler_lease_direct_name] is not _scheduler_lease_owner_value:
        raise RuntimeError(
            f"scheduler lease direct re-export drifted from owner module: {_scheduler_lease_direct_name}"
        )
del _scheduler_lease_direct_name, _scheduler_lease_owner_value
_SCHEDULER_LEASE_COMPAT_EXPORTS = tuple(
    _SCHEDULER_LEASE_COMPAT_FACADE_REEXPORTS[name] for name in _SCHEDULER_LEASE_COMPAT_REEXPORT_NAMES
)

DEFAULT_PRODUCTION_SOURCES = ("gfs", "IFS")
DEFAULT_ALLOWED_CYCLE_HOURS_UTC = (0, 12)
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_CYCLE_LAG_HOURS = 0
DEFAULT_MAX_CYCLES_PER_SOURCE = 1
DEFAULT_LOCK_TTL_SECONDS = 3600
# Default listen port of this service's own control API (uvicorn ... --port 8000).
# Used by the Slurm gateway self-reference check to reject a gateway URL that
# points back at the scheduler/orchestrator itself instead of a real gateway.
DEFAULT_SERVICE_PORT = 8000
MAX_LOOKBACK_HOURS = 168
MAX_SOURCES = 4
MAX_CYCLES_PER_SOURCE = 16
MAX_DISCOVERED_MODELS = 1000
MAX_DISCOVERED_CYCLES = _scheduler_discovery.MAX_DISCOVERED_CYCLES
MAX_CANDIDATES = _scheduler_candidates.MAX_CANDIDATES
MAX_REGISTRY_PAGES = 20
MAX_EVIDENCE_BYTES = _scheduler_evidence.MAX_EVIDENCE_BYTES
MAX_CONTINUOUS_JSON_PASSES = 100
MAX_MODEL_RUN_STAGE_TASK_ROWS = 16
MAX_SLURM_ENV_VALUE_LENGTH = 1024
DEFAULT_CONCURRENT_SUBMIT_BOUND = 4
# Cap a post-connect query hang in restart reconcile (mirrors
# VALIDATION_STATEMENT_TIMEOUT_MS): a reachable-but-slow DB must not stall the
# pass at reconcile time; best-effort reconcile swallows the timeout and the
# pass proceeds to the DB-host preflight.
RECONCILE_DB_STATEMENT_TIMEOUT_MS = 10_000
SCHEDULER_EVIDENCE_SCHEMA_VERSION = _scheduler_evidence.SCHEDULER_EVIDENCE_SCHEMA_VERSION
MODEL_RUN_EVIDENCE_SCHEMA_VERSION = _scheduler_evidence.MODEL_RUN_EVIDENCE_SCHEMA_VERSION
SCHEDULER_EVIDENCE_CONTRACT_ID = _scheduler_evidence.SCHEDULER_EVIDENCE_CONTRACT_ID
SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE = _scheduler_evidence.SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE
SCHEDULER_EVIDENCE_GITHUB_ISSUE = _scheduler_evidence.SCHEDULER_EVIDENCE_GITHUB_ISSUE
SLURM_ARRAY_STAGE_NAMES = {"forcing", "forecast", "parse", "state_save_qc", "frequency"}
SAFE_SLURM_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SAFE_SLURM_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:=,@+\-]*$")
SHELL_META_RE = re.compile(r"[;|&$`<>\n\r]")
PRODUCTION_SLURM_ENV_PASSTHROUGH_KEYS = (
    "GFS_NOMADS_BASE_URL",
    "GFS_FORECAST_START_HOUR",
    "GFS_FORECAST_END_HOUR",
    "GFS_FORECAST_STEP_HOURS",
    "GFS_FORECAST_RESOLUTION_SEGMENTS",
    "IFS_OPEN_DATA_SOURCE",
    "IFS_OPEN_DATA_FALLBACK_SOURCES",
    "IFS_FORECAST_START_HOUR",
    "IFS_FORECAST_END_HOUR",
    "IFS_FORECAST_STEP_HOURS",
    "IFS_FORECAST_RESOLUTION_SEGMENTS",
    "IFS_SOURCE_COOLDOWN_SECONDS",
    "IFS_DOWNLOAD_CHUNK_SIZE_BYTES",
    "IFS_MAX_FILE_SIZE_BYTES",
    "NHMS_DOWNLOAD_BBOX_SOUTH",
    "NHMS_DOWNLOAD_BBOX_NORTH",
    "NHMS_DOWNLOAD_BBOX_WEST",
    "NHMS_DOWNLOAD_BBOX_EAST",
    "NHMS_GRIB_ENV_ROOT",
    "NHMS_GRIB_SYSTEM_ECCODES",
)
LOCALHOST_NAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
    "::",
}
DATABASE_HOST_ALLOWED_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
SLURM_RESOURCE_PROFILE_DIRECTIVE_FIELDS = {
    "partition",
    "account",
    "nodes",
    "ntasks",
    "cpus_per_task",
    "memory_gb",
    "walltime",
    "max_concurrent",
    "shud_threads",
}
SLURM_RESOURCE_PROFILE_TEMPLATE_IDENTITY_FIELDS = {
    "run_id",
    "workspace_dir",
    "stage_name",
    "cycle_id",
    "object_store_root",
    "object_store_prefix",
    "manifest_index_path",
}
TASK_RESULT_CANDIDATE_IDENTITY_FIELDS = ("candidate_id", "run_id", "forcing_version_id", "model_id")
TASK_RESULT_INDEX_IDENTITY_FIELDS = ("task_id", "array_task_id", "original_task_id")
TERMINAL_PIPELINE_STATUSES = {
    "succeeded",
    "partially_failed",
    "failed",
    "cancelled",
    "submission_failed",
    "permanently_failed",
}
PIPELINE_TERMINAL_SUCCESS_STAGES = {
    "parse",
    "state_save_qc",
    "frequency",
    "publish",
    "parse_output",
    "save_state_snapshot",
    "save_state_snapshot_array",
    "publish_tiles",
}
UNKNOWN_AFTER_ATTEMPT = _scheduler_evidence.UNKNOWN_AFTER_ATTEMPT
uuid4 = _uuid4


SchedulerResourceLimitError = _scheduler_discovery.SchedulerResourceLimitError


SchedulerEvidenceWriteError = _scheduler_evidence.SchedulerEvidenceWriteError


ProductionSchedulerConfig = importlib.import_module(
    "services.orchestrator.scheduler_config"
).ProductionSchedulerConfig
ProductionSchedulerConfig.__module__ = __name__


_scheduler_types_module = importlib.import_module("services.orchestrator.scheduler_types")
SchedulerPassResult = _scheduler_types_module.SchedulerPassResult
RegisteredSchedulerModel = _scheduler_types_module.RegisteredSchedulerModel
SchedulerCandidate = _scheduler_types_module.SchedulerCandidate
_resource_profile_project_identity = _scheduler_types_module._resource_profile_project_identity
for _scheduler_type in (SchedulerPassResult, RegisteredSchedulerModel, SchedulerCandidate):
    _scheduler_type.__module__ = __name__
del _scheduler_type


SchedulerSourceCycle = _scheduler_discovery.SchedulerSourceCycle


class _BlockedModelRegistry:
    def list_models(
        self,
        *,
        basin_version_id: str | None,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> Mapping[str, Any]:
        del basin_version_id, active, limit, offset
        raise RuntimeError("blocked scheduler root preflight must not query model registry")

    def get_model(self, model_id: str) -> Mapping[str, Any]:
        del model_id
        raise RuntimeError("blocked scheduler root preflight must not query model registry")


class ProductionScheduler:
    def __init__(
        self,
        config: ProductionSchedulerConfig | None = None,
        *,
        registry: ModelRegistryReader | None = None,
        adapters: Mapping[str, CycleDiscoveryAdapter] | None = None,
        active_repository: ActiveCandidateRepository | None = None,
        canonical_readiness_provider: CanonicalReadinessProvider | None | object = (
            _CANONICAL_READINESS_PROVIDER_UNSET
        ),
        forcing_producer: ForcingProducerRunner | None = None,
        orchestrator_factory: ProductionOrchestratorFactory | None = None,
        sleep: Callable[[float], None] | None = None,
        reconcile_store: Any | None = None,
        reconcile_comment_query: Callable[[str], Any] | None = None,
        reconcile_sacct_query: Callable[[str], Any] | None = None,
    ) -> None:
        self.config = config or ProductionSchedulerConfig()
        # M24 §3A restart reconcile: injectable so tests drive it without a real
        # cluster/DB. ``None`` => build from env at pass time (production).
        self._reconcile_store = reconcile_store
        # Class name (secret-free) of the last reconcile-store build failure, so
        # _run_restart_reconcile can report a build skip without surfacing the
        # raw exception message (a malformed-URL error embeds the full DSN).
        self._reconcile_store_build_error: str | None = None
        self._reconcile_comment_query = reconcile_comment_query
        self._reconcile_sacct_query = reconcile_sacct_query
        self.registry = registry if registry is not None else PsycopgModelRegistryStore.from_env()
        self.adapters = dict(adapters if adapters is not None else _default_adapters())
        self.active_repository = active_repository
        if canonical_readiness_provider is _CANONICAL_READINESS_PROVIDER_UNSET or canonical_readiness_provider is None:
            self.canonical_readiness_provider = _UnavailableCanonicalReadinessProvider(
                reason="canonical_readiness_provider_absent",
                dependency="canonical_readiness_provider",
                retryable=True,
            )
        else:
            self.canonical_readiness_provider = canonical_readiness_provider
        self.forcing_producer = forcing_producer
        self.orchestrator_factory = orchestrator_factory
        self.sleep = sleep or _sleep
        self._source_readiness_context_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    @classmethod
    def from_env(cls, config: ProductionSchedulerConfig | None = None) -> ProductionScheduler:
        config = config or ProductionSchedulerConfig()
        if config.require_runtime_roots and _scheduler_lock_evidence_root_preflight(config)["status"] == "blocked":
            return cls(config=config, registry=_BlockedModelRegistry(), adapters={}, active_repository=None)
        if config.require_runtime_roots and _scheduler_runtime_root_preflight(config)["status"] == "blocked":
            return cls(config=config, registry=_BlockedModelRegistry(), adapters={}, active_repository=None)
        return cls(
            config=config,
            active_repository=_active_repository_from_env(),
            canonical_readiness_provider=_canonical_readiness_provider_from_env(),
            forcing_producer=_forcing_producer_from_env() if config.forcing_production_enabled else None,
        )

    def run_once(self) -> SchedulerPassResult:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime.run_once(self)

    def _build_scheduler_lease(self) -> Any:
        database_url = (self.config.database_url or "").strip()
        if self.config.scheduler_lock_backend == "postgres" and database_url:
            return PostgresSchedulerLease(
                database_url,
                lock_name=f"nhms:production-scheduler:{Path(self.config.lock_path)}",
                display_lock_path=str(self.config.lock_path),
            )
        return FileSchedulerLease(
            Path(self.config.lock_path),
            ttl_seconds=self.config.lock_ttl_seconds,
            workspace_root=Path(self.config.workspace_root),
        )

    def _run_restart_reconcile(self) -> dict[str, Any] | None:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._run_restart_reconcile(self)

    def _reset_reconcile_store_after_error(self) -> None:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._reset_reconcile_store_after_error(self)

    def _restart_reconcile_store(self) -> Any | None:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._restart_reconcile_store(self)

    def _restart_reconcile_comment_query(self) -> Callable[[str], Any]:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._restart_reconcile_comment_query(self)

    def _restart_reconcile_sacct_query(self) -> Callable[[str], Any]:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._restart_reconcile_sacct_query(self)

    def _run_retention(self, started_at: datetime) -> dict[str, Any]:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._run_retention(self, started_at)

    def _write_prelock_blocked_evidence(
        self,
        pass_id: str,
        evidence: dict[str, Any],
        root_preflight: Mapping[str, Any],
    ) -> Path | None:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._write_prelock_blocked_evidence(self, pass_id, evidence, root_preflight)

    def _reserve_pre_execution_evidence(
        self,
        pass_id: str,
        started_at: datetime,
        candidate_count: int,
    ) -> dict[str, Any]:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._reserve_pre_execution_evidence(self, pass_id, started_at, candidate_count)

    def _scheduler_evidence_write_context(self) -> _scheduler_evidence.SchedulerEvidenceWriteContext:
        from services.orchestrator import scheduler_runtime

        return scheduler_runtime._scheduler_evidence_write_context(self)

    def run_continuous(self, *, max_passes: int | None = None) -> list[SchedulerPassResult]:
        if max_passes is not None:
            max_passes = int(max_passes)
            if max_passes < 1:
                raise ValueError("production scheduler max_passes must be at least 1")
            if max_passes > MAX_CONTINUOUS_JSON_PASSES:
                raise ValueError(
                    f"production scheduler max_passes exceeds finite JSON output limit {MAX_CONTINUOUS_JSON_PASSES}"
                )
        results: list[SchedulerPassResult] = []
        completed = 0
        while max_passes is None or completed < max_passes:
            result = self.run_once()
            if max_passes is None:
                results[:] = [result]
            else:
                results.append(result)
            completed += 1
            if max_passes is not None and completed >= max_passes:
                break
            self.sleep(self.config.interval_seconds)
        return results

    def _produce_forcing_for_candidates(
        self,
        candidates: Sequence[SchedulerCandidate],
    ) -> tuple[list[SchedulerCandidate], list[SchedulerCandidate], list[dict[str, Any]]]:
        return _scheduler_execution.produce_forcing_for_candidates(self._scheduler_execution_context(), candidates)

    def _execute_candidates(self, candidates: Sequence[SchedulerCandidate]) -> list[dict[str, Any]]:
        return _scheduler_execution.execute_candidates(self._scheduler_execution_context(), candidates)

    def _execute_candidate_cohort(
        self,
        source_id: str,
        cycle_time: datetime,
        cycle_id: str,
        cycle_candidates: Sequence[SchedulerCandidate],
        *,
        orchestration_run_id: str | None,
    ) -> list[dict[str, Any]]:
        return _scheduler_execution.execute_candidate_cohort(
            self._scheduler_execution_context(),
            source_id,
            cycle_time,
            cycle_id,
            cycle_candidates,
            orchestration_run_id=orchestration_run_id,
        )

    def _scheduler_execution_context(self) -> _scheduler_execution.SchedulerExecutionContext:
        return _scheduler_execution.SchedulerExecutionContext(
            config=self.config,
            forcing_producer=self.forcing_producer,
            orchestrator_for=self._orchestrator_for,
            execute_candidate_cohort=self._execute_candidate_cohort,
            set_last_submit_overlap_receipt=self._set_last_submit_overlap_receipt,
            submit_overlap_receipt_factory=SubmitOverlapReceipt,
            timed_submission=timed_submission,
            run_concurrent_submissions=run_concurrent_submissions,
            cycle_id_for=cycle_id_for,
            restart_compatible_candidate_cohorts=_restart_compatible_candidate_cohorts,
            candidate_execution_cohorts=_candidate_execution_cohorts,
            candidate_is_fresh_full_chain=_candidate_is_fresh_full_chain,
            candidate_max_lead_hours=_candidate_max_lead_hours,
            candidate_canonical_product_id=_candidate_canonical_product_id,
            candidate_scheduler_canonical_identity=_candidate_scheduler_canonical_identity,
            candidate_forcing_blocked_evidence=_candidate_forcing_blocked_evidence,
            blocked_candidate=_blocked_candidate,
            candidate_with_forcing_result=_candidate_with_forcing_result,
            candidate_forcing_ready_evidence=_candidate_forcing_ready_evidence,
            candidate_with_state_evidence=_candidate_with_state_evidence,
            candidate_output_uri=_candidate_output_uri,
            candidate_identity_evidence=_candidate_identity_evidence,
            candidate_model_run_review_evidence=_candidate_model_run_review_evidence,
            standard_chain_shape=lambda: [stage.stage for stage in ForecastOrchestrator.stages],
            candidate_basin_manifest=_candidate_basin_manifest,
            slurm_env_check=_slurm_env_check,
            candidate_slurm_preflight_blocked_evidence=_candidate_slurm_preflight_blocked_evidence,
            secret_manifest_findings=iter_secret_manifest_findings,
            candidate_secret_manifest_blocked_evidence=_candidate_secret_manifest_blocked_evidence,
            slurm_resource_profile_blockers=_slurm_resource_profile_blockers,
            evidence_safe=_evidence_safe,
            candidate_execution_evidence=_candidate_execution_evidence,
            unknown_after_attempt=UNKNOWN_AFTER_ATTEMPT,
        )

    def _set_last_submit_overlap_receipt(self, receipt: SubmitOverlapReceipt) -> None:
        self._last_submit_overlap_receipt = receipt

    def _cancel_requested_active_slurm(self, skipped_candidates: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        from services.orchestrator import scheduler_cancellation

        return scheduler_cancellation._cancel_requested_active_slurm(self, skipped_candidates)

    def _orchestrator_for(self, source_id: str) -> ForecastOrchestrator:
        if self.orchestrator_factory is not None:
            return self.orchestrator_factory(source_id)
        return self._default_orchestrator_for(source_id, state_manager=StateManager.from_env())

    def _cancel_orchestrator_for(self, source_id: str) -> ForecastOrchestrator:
        if self.orchestrator_factory is not None:
            return self.orchestrator_factory(source_id)
        return self._default_orchestrator_for(source_id, state_manager=None)

    def _default_orchestrator_for(self, source_id: str, *, state_manager: Any | None) -> ForecastOrchestrator:
        config = OrchestratorConfig.from_env()
        if self.config.slurm_execution_enabled:
            config = OrchestratorConfig(
                workspace_root=self.config.workspace_root,
                object_store_root=self.config.object_store_root or config.object_store_root,
                object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", config.object_store_prefix),
                slurm_gateway_url=config.slurm_gateway_url,
                templates_dir=config.templates_dir,
                poll_interval_seconds=config.poll_interval_seconds,
                job_timeout_seconds=config.job_timeout_seconds,
                source_id=config.source_id,
                forecast_horizon_hours=config.forecast_horizon_hours,
                scenario_id=config.scenario_id if config.scenario_id_explicit else None,
                era5_area=config.era5_area,
                state_soft_stale_threshold_days=config.state_soft_stale_threshold_days,
                state_hard_stale_threshold_days=config.state_hard_stale_threshold_days,
                require_forecast_warm_start=config.require_forecast_warm_start,
                slurm_job_type_templates=dict(self.config.slurm_job_type_templates or {}),
                slurm_env=dict(self.config.slurm_env),
            )
        if config.source_id != source_id:
            config = OrchestratorConfig(
                workspace_root=config.workspace_root,
                object_store_root=config.object_store_root,
                object_store_prefix=config.object_store_prefix,
                slurm_gateway_url=config.slurm_gateway_url,
                templates_dir=config.templates_dir,
                poll_interval_seconds=config.poll_interval_seconds,
                job_timeout_seconds=config.job_timeout_seconds,
                source_id=source_id,
                forecast_horizon_hours=config.forecast_horizon_hours,
                scenario_id=scenario_for_source(source_id),
                era5_area=config.era5_area,
                state_soft_stale_threshold_days=config.state_soft_stale_threshold_days,
                state_hard_stale_threshold_days=config.state_hard_stale_threshold_days,
                require_forecast_warm_start=config.require_forecast_warm_start,
                slurm_job_type_templates=config.slurm_job_type_templates,
                slurm_env=config.slurm_env,
            )
        return ForecastOrchestrator(
            config=config,
            repository=_orchestrator_repository_from_env(),
            state_manager=state_manager,
            retry_service=_retry_service_from_env(),
        )

    def _base_evidence(self, pass_id: str, started_at: datetime) -> dict[str, Any]:
        return _scheduler_evidence.base_evidence(
            self.config,
            pass_id,
            started_at,
            resolved_runtime_roots=_scheduler_resolved_runtime_roots,
            runtime_config_evidence=_scheduler_runtime_config_evidence,
        )

    def _discover_models(self) -> tuple[list[RegisteredSchedulerModel], dict[str, Any]]:
        return _scheduler_models.discover_models(self)

    def _discovery_context(self) -> _scheduler_discovery.SchedulerDiscoveryContext:
        return _scheduler_discovery.SchedulerDiscoveryContext(
            config=self.config,
            adapters=self.adapters,
            active_repository=self.active_repository,
            floor_to_source_cycle_boundary=lambda value, _sources: _floor_to_source_cycle_boundary(
                value,
                _sources,
                allowed_cycle_hours_utc=self.config.allowed_cycle_hours_utc,
            ),
            source_horizon_metadata=_source_horizon_metadata,
            candidate_factory=_candidate_for,
            candidate_state_provider_caller=_call_candidate_state_provider,
            candidate_state_decider=_candidate_state_decision,
            discover_source_window_provider=self._discover_source_window,
            cycle_completion_status_provider=self._cycle_completion_status,
        )

    def _cycle_completion_status(
        self,
        discovery: CycleDiscovery,
        models: Sequence[RegisteredSchedulerModel],
        *,
        horizon: Mapping[str, Any] | None = None,
    ) -> str:
        return _scheduler_discovery.cycle_completion_status(
            self._discovery_context(),
            discovery,
            models,
            horizon=horizon,
        )

    def _discover_cycles(
        self,
        started_at: datetime,
        models: Sequence[RegisteredSchedulerModel] = (),
    ) -> tuple[list[SchedulerSourceCycle], list[dict[str, Any]]]:
        return _scheduler_discovery.discover_cycles(
            self._discovery_context(),
            started_at,
            models=models,
        )

    def _discover_source_window(
        self,
        adapter: CycleDiscoveryAdapter,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[CycleDiscovery]:
        return _scheduler_discovery.discover_source_window(
            adapter,
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
        )

    def _canonical_readiness_for_candidate(
        self,
        candidate: SchedulerCandidate,
        cycle: SchedulerSourceCycle,
    ) -> dict[str, Any] | None:
        provider = self.canonical_readiness_provider
        if provider is None:
            return None
        context = self._source_readiness_context(cycle)
        forecast_hours = list(context["forecast_hours"])
        policy_identity = dict(context["policy_identity"])
        source_object_identity = dict(context["source_object_identity"])
        try:
            readiness = provider.canonical_readiness(
                source_id=cycle.discovery.source_id,
                cycle_time=cycle.discovery.cycle_time,
                forecast_hours=forecast_hours,
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
                canonical_product_id=_candidate_canonical_product_id(candidate),
                model_id=candidate.model_id,
                basin_id=candidate.basin_id,
            )
        except Exception as error:
            readiness = _canonical_readiness_unavailable_evidence(
                cycle.discovery,
                candidate,
                forecast_hours=forecast_hours,
                policy_identity=policy_identity,
                source_object_identity=source_object_identity,
                reason="canonical_readiness_query_failed",
                dependency="canonical_readiness_provider",
                error=error,
                retryable=True,
            )
        evidence = dict(readiness)
        evidence.setdefault("source", cycle.discovery.source_id)
        evidence.setdefault("cycle_time", _format_utc(cycle.discovery.cycle_time))
        evidence.setdefault("canonical_product_id", _candidate_canonical_product_id(candidate))
        evidence.setdefault("model_id", candidate.model_id)
        evidence.setdefault("basin_id", candidate.basin_id)
        evidence.setdefault("policy_identity", policy_identity)
        evidence.setdefault("source_object_identity", source_object_identity)
        evidence.setdefault("accepted_horizon", _accepted_horizon_from_hours(forecast_hours))
        return _evidence_safe(evidence)

    def _source_readiness_context(self, cycle: SchedulerSourceCycle) -> dict[str, Any]:
        cache_key = (
            cycle.discovery.source_id,
            _ensure_utc(cycle.discovery.cycle_time).isoformat(),
            json.dumps(_evidence_safe(cycle.horizon), sort_keys=True, default=str),
        )
        cached = self._source_readiness_context_cache.get(cache_key)
        if cached is not None:
            return cached
        adapter = self.adapters.get(cycle.discovery.source_id)
        forecast_hours = _source_forecast_hours(cycle.discovery, adapter, cycle.horizon)
        context = {
            "forecast_hours": forecast_hours,
            "policy_identity": _source_policy_identity(cycle.discovery, adapter, forecast_hours),
            "source_object_identity": _source_object_identity(cycle.discovery, adapter, forecast_hours),
        }
        self._source_readiness_context_cache[cache_key] = context
        return context

    def _candidate_construction_context(self) -> _scheduler_candidates.SchedulerCandidateConstructionContext:
        return _scheduler_candidates.SchedulerCandidateConstructionContext(
            config=self.config,
            active_repository=self.active_repository,
            canonical_readiness_for_candidate=self._canonical_readiness_for_candidate,
            orchestrator_for=self._orchestrator_for,
            candidate_factory=_candidate_for,
            candidate_state_provider_caller=_call_candidate_state_provider,
            active_slurm_jobs_provider_caller=_call_active_slurm_jobs_provider,
            active_slurm_jobs_bounder=_bounded_active_slurm_jobs,
            candidate_state_decider=_candidate_state_decision,
            candidate_state_identity_mismatch_detector=_candidate_state_has_identity_mismatch,
            candidate_state_scoped_retry_detector=_candidate_state_is_candidate_scoped_retry,
            repaired_state_audit_evidence_builder=_candidate_repaired_state_audit_evidence,
            max_candidates=MAX_CANDIDATES,
        )

    def _build_candidates(
        self,
        *,
        models: Sequence[RegisteredSchedulerModel],
        cycles: Sequence[SchedulerSourceCycle],
        allow_slurm_status_sync: bool = False,
    ) -> tuple[
        list[SchedulerCandidate],
        list[SchedulerCandidate],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        return _scheduler_candidates.build_candidates(
            self._candidate_construction_context(),
            models=models,
            cycles=cycles,
            allow_slurm_status_sync=allow_slurm_status_sync,
        )

    def _write_evidence(self, pass_id: str, evidence: Mapping[str, Any]) -> Path | None:
        return _scheduler_evidence.write_evidence(
            self._scheduler_evidence_write_context(),
            pass_id,
            evidence,
        )


def _fetch_active_model_details(registry: ModelRegistryReader) -> list[Mapping[str, Any]]:
    from services.orchestrator import scheduler_models

    return scheduler_models.fetch_active_model_details(registry)


def _floor_to_source_cycle_boundary(
    value: datetime,
    sources: Sequence[str],
    *,
    allowed_cycle_hours_utc: Sequence[int] | None = None,
) -> datetime:
    normalized = _ensure_utc(value).replace(minute=0, second=0, microsecond=0)
    cycle_hours = (
        list(_normalize_allowed_cycle_hours_utc(allowed_cycle_hours_utc))
        if allowed_cycle_hours_utc is not None
        else sorted(_cycle_hours_for_sources(sources))
    )
    candidates: list[datetime] = []
    for hour in cycle_hours:
        candidate = normalized.replace(hour=hour)
        if candidate > normalized:
            candidate -= timedelta(days=1)
        candidates.append(candidate)
    return max(candidates) if candidates else normalized


def _cycle_hours_for_sources(sources: Sequence[str]) -> set[int]:
    hours: set[int] = set()
    for source in sources:
        try:
            normalized = normalize_source_id(str(source))
        except ValueError:
            normalized = str(source)
        if normalized in {"gfs", "IFS"}:
            hours.update({0, 6, 12, 18})
        elif normalized == "ERA5":
            hours.add(0)
    if not hours:
        hours.update({0, 6, 12, 18})
    return hours


def _fetch_scheduler_model_detail(registry: ModelRegistryReader, model_id: str) -> Mapping[str, Any]:
    from services.orchestrator import scheduler_models

    return scheduler_models.fetch_scheduler_model_detail(registry, model_id)


def _coerce_registered_model(row: Mapping[str, Any]) -> RegisteredSchedulerModel | dict[str, Any]:
    from services.orchestrator import scheduler_models

    return scheduler_models.coerce_registered_model(row)


def _active_model_duplicate_exclusions(rows: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    from services.orchestrator import scheduler_models

    return scheduler_models.active_model_duplicate_exclusions(rows)


def _active_model_identity_groups(
    rows: Sequence[Mapping[str, Any]],
    value_getter: Callable[[Mapping[str, Any]], str | None],
) -> dict[str, set[int]]:
    from services.orchestrator import scheduler_models

    return scheduler_models.active_model_identity_groups(rows, value_getter)


def _active_model_model_id(row: Mapping[str, Any]) -> str | None:
    from services.orchestrator import scheduler_models

    return scheduler_models.active_model_model_id(row)


def _active_model_package_uri(row: Mapping[str, Any]) -> str | None:
    from services.orchestrator import scheduler_models

    return scheduler_models.active_model_package_uri(row)


def _active_model_package_checksum(row: Mapping[str, Any]) -> str | None:
    from services.orchestrator import scheduler_models

    return scheduler_models.active_model_package_checksum(row)


def _has_package_specific_checksum_context(row: Mapping[str, Any], resource_profile: Mapping[str, Any]) -> bool:
    from services.orchestrator import scheduler_models

    return scheduler_models.has_package_specific_checksum_context(row, resource_profile)


def _model_duplicate_identity_value_for_evidence(field: str, value: str) -> str:
    from services.orchestrator import scheduler_models

    return scheduler_models.model_duplicate_identity_value_for_evidence(field, value)


def _resource_profile_summary(resource_profile: Mapping[str, Any]) -> dict[str, Any]:
    from services.orchestrator import scheduler_models

    return scheduler_models.resource_profile_summary(resource_profile)


def _coerce_output_segment_count(resource_profile: Mapping[str, Any], *, fallback: Any = None) -> int | None:
    from services.orchestrator import scheduler_models

    return scheduler_models.coerce_output_segment_count(resource_profile, fallback=fallback)


def _mapping_value(value: Any) -> Mapping[str, Any]:
    from services.orchestrator import scheduler_models

    return scheduler_models.mapping_value(value)


def _model_exclusion(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    from services.orchestrator import scheduler_models

    return scheduler_models.model_exclusion(row, reason)


def _matches_filters(
    model: RegisteredSchedulerModel,
    *,
    model_ids: Sequence[str],
    basin_ids: Sequence[str],
) -> bool:
    from services.orchestrator import scheduler_models

    return scheduler_models.matches_filters(model, model_ids=model_ids, basin_ids=basin_ids)


def _filter_expression(model_ids: Sequence[str], basin_ids: Sequence[str]) -> str | None:
    from services.orchestrator import scheduler_models

    return scheduler_models.filter_expression(model_ids, basin_ids)


_source_cycle_evidence = _scheduler_discovery._source_cycle_evidence
_source_cycle_status_candidate = _scheduler_discovery._source_cycle_status_candidate
_source_cycle_not_selected_reason = _scheduler_discovery._source_cycle_not_selected_reason


_source_blocked_evidence = _scheduler_candidates._source_blocked_evidence


SOURCE_DISCOVERY_SENSITIVE_KEY_RE = _scheduler_discovery.SOURCE_DISCOVERY_SENSITIVE_KEY_RE
SOURCE_DISCOVERY_SENSITIVE_TEXT_RE = _scheduler_discovery.SOURCE_DISCOVERY_SENSITIVE_TEXT_RE
_source_discovery_evidence_safe = _scheduler_discovery._source_discovery_evidence_safe
_source_secret_text_safe = _scheduler_discovery._source_secret_text_safe


_reason_code = _scheduler_candidates._reason_code
_canonical_readiness_unavailable_evidence = _scheduler_candidates._canonical_readiness_unavailable_evidence


_duplicate_cycle_evidence = _scheduler_discovery._duplicate_cycle_evidence
_backfill_deferred_evidence = _scheduler_discovery._backfill_deferred_evidence


def _restart_compatible_candidate_cohorts(
    candidates: Sequence[SchedulerCandidate],
) -> list[tuple[tuple[int, str], list[SchedulerCandidate]]]:
    return _scheduler_execution.restart_compatible_candidate_cohorts(
        candidates,
        candidate_restart_stage=_candidate_restart_stage,
        candidate_restart_cohort_key=_candidate_restart_cohort_key,
    )


_canonical_candidate_row_count = _scheduler_candidates._canonical_candidate_row_count
_canonical_evidence_is_fresh_zero_row = _scheduler_candidates._canonical_evidence_is_fresh_zero_row
_candidate_is_fresh_full_chain = _scheduler_candidates._candidate_is_fresh_full_chain


def _candidate_restart_stage(candidate: SchedulerCandidate) -> str | None:
    return _scheduler_execution.candidate_restart_stage(
        candidate,
        candidate_is_fresh_full_chain=_candidate_is_fresh_full_chain,
        native_shud_stage_aliases=NATIVE_SHUD_STAGE_ALIASES,
        canonical_downstream_stage=_canonical_downstream_stage,
    )


def _candidate_restart_cohort_key(restart_stage: str | None) -> tuple[int, str]:
    return _scheduler_execution.candidate_restart_cohort_key(
        restart_stage,
        downstream_restart_stages=DOWNSTREAM_RESTART_STAGES,
    )


def _candidate_execution_cohort_run_id(source_id: str, cycle_time: datetime, cohort_key: tuple[int, str]) -> str:
    return _scheduler_execution.candidate_execution_cohort_run_id(
        source_id,
        cycle_time,
        cohort_key,
        format_cycle_time=format_cycle_time,
    )


def _candidate_execution_cohorts(
    source_id: str,
    cycle_time: datetime,
    cohort_key: tuple[int, str],
    candidates: Sequence[SchedulerCandidate],
) -> list[tuple[list[SchedulerCandidate], str | None]]:
    return _scheduler_execution.candidate_execution_cohorts(
        source_id,
        cycle_time,
        cohort_key,
        candidates,
        run_id_for_candidate=_candidate_execution_cohort_run_id_for_candidate,
    )


def _candidate_execution_cohort_run_id_for_candidate(
    source_id: str,
    cycle_time: datetime,
    cohort_key: tuple[int, str],
    candidate: SchedulerCandidate,
) -> str:
    return _scheduler_execution.candidate_execution_cohort_run_id_for_candidate(
        source_id,
        cycle_time,
        cohort_key,
        candidate,
        format_cycle_time=format_cycle_time,
    )


def _candidate_for(
    *,
    discovery: CycleDiscovery,
    model: RegisteredSchedulerModel,
    horizon: Mapping[str, Any],
) -> SchedulerCandidate:
    from services.orchestrator import scheduler_candidate_manifest

    return scheduler_candidate_manifest._candidate_for(discovery=discovery, model=model, horizon=horizon)


def _candidate_max_lead_hours(candidate: SchedulerCandidate) -> int | None:
    from services.orchestrator import scheduler_candidate_manifest

    return scheduler_candidate_manifest._candidate_max_lead_hours(candidate)


_blocked_candidate = _scheduler_candidates._blocked_candidate
_candidate_with_state_evidence = _scheduler_candidates._candidate_with_state_evidence


def _candidate_with_forcing_result(candidate: SchedulerCandidate, result: Any) -> SchedulerCandidate:
    from services.orchestrator import scheduler_candidate_manifest

    return scheduler_candidate_manifest._candidate_with_forcing_result(candidate, result)


def _candidate_scheduler_canonical_identity(candidate: SchedulerCandidate) -> dict[str, Any]:
    from services.orchestrator import scheduler_candidate_manifest

    return scheduler_candidate_manifest._candidate_scheduler_canonical_identity(candidate)


_merge_state_evidence = _scheduler_candidates._merge_state_evidence


def _candidate_basin_manifest(
    candidate: SchedulerCandidate,
    *,
    output_uri: str,
    orchestration_run_id: str | None = None,
) -> dict[str, Any]:
    from services.orchestrator import scheduler_candidate_manifest

    return scheduler_candidate_manifest._candidate_basin_manifest(
        candidate,
        output_uri=output_uri,
        orchestration_run_id=orchestration_run_id,
    )


def _candidate_manual_retry_attempt(candidate: SchedulerCandidate) -> int | None:
    from services.orchestrator import scheduler_candidate_manifest

    return scheduler_candidate_manifest._candidate_manual_retry_attempt(candidate)


def _apply_candidate_warm_start_fields(manifest: dict[str, Any], candidate: SchedulerCandidate) -> None:
    from services.orchestrator import scheduler_candidate_manifest

    return scheduler_candidate_manifest._apply_candidate_warm_start_fields(manifest, candidate)


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
_candidate_evidence_write_blocked_evidence = _scheduler_candidate_execution_evidence_forwarder(
    "_candidate_evidence_write_blocked_evidence"
)
_cancel_candidate_evidence_write_blocked_evidence = _scheduler_candidate_execution_evidence_forwarder(
    "_cancel_candidate_evidence_write_blocked_evidence"
)
_sync_candidate_evidence_write_blocked_evidence = _scheduler_candidate_execution_evidence_forwarder(
    "_sync_candidate_evidence_write_blocked_evidence"
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
_config_path_relative_to_preserve_final = _scheduler_runtime_roots_forwarder(
    "_config_path_relative_to_preserve_final"
)
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
_require_safe_directory_final_component = _scheduler_runtime_roots_forwarder(
    "_require_safe_directory_final_component"
)


def _evidence_reservation_blocked_payload(
    *,
    pass_id: str,
    artifact_path: Path,
    reason: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return _scheduler_evidence.evidence_reservation_blocked_payload(
        pass_id=pass_id,
        artifact_path=artifact_path,
        reason=reason,
        details=details,
        evidence_safe=_evidence_safe,
    )


def _evidence_write_error_payload(error: OSError) -> dict[str, Any]:
    return _scheduler_evidence.evidence_write_error_payload(error)


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

_SCHEDULER_DISCOVERY_COMPAT_ALIAS_OWNER_NAMES = MappingProxyType(
    {
        "MAX_DISCOVERED_CYCLES": "MAX_DISCOVERED_CYCLES",
        "SchedulerResourceLimitError": "SchedulerResourceLimitError",
        "SchedulerSourceCycle": "SchedulerSourceCycle",
        "_source_cycle_evidence": "_source_cycle_evidence",
        "_source_cycle_status_candidate": "_source_cycle_status_candidate",
        "_source_cycle_not_selected_reason": "_source_cycle_not_selected_reason",
        "SOURCE_DISCOVERY_SENSITIVE_KEY_RE": "SOURCE_DISCOVERY_SENSITIVE_KEY_RE",
        "SOURCE_DISCOVERY_SENSITIVE_TEXT_RE": "SOURCE_DISCOVERY_SENSITIVE_TEXT_RE",
        "_source_discovery_evidence_safe": "_source_discovery_evidence_safe",
        "_source_secret_text_safe": "_source_secret_text_safe",
        "_duplicate_cycle_evidence": "_duplicate_cycle_evidence",
        "_backfill_deferred_evidence": "_backfill_deferred_evidence",
        "_source_horizon_metadata": "source_horizon_metadata",
    }
)
_SCHEDULER_DISCOVERY_COMPAT_ALIAS_NAMES = tuple(_SCHEDULER_DISCOVERY_COMPAT_ALIAS_OWNER_NAMES)
_SCHEDULER_DISCOVERY_COMPAT_FORWARDER_NAMES = (
    "_discovery_context",
    "_cycle_completion_status",
    "_discover_cycles",
    "_discover_source_window",
)
_SCHEDULER_DISCOVERY_COMPAT_OWNER_MISSING = tuple(
    owner_name
    for owner_name in _SCHEDULER_DISCOVERY_COMPAT_ALIAS_OWNER_NAMES.values()
    if not hasattr(_scheduler_discovery, owner_name)
)
_SCHEDULER_DISCOVERY_COMPAT_FACADE_MISSING = tuple(
    name for name in _SCHEDULER_DISCOVERY_COMPAT_ALIAS_NAMES if name not in globals()
)
if _SCHEDULER_DISCOVERY_COMPAT_OWNER_MISSING:
    raise RuntimeError(
        "scheduler discovery compatibility names missing from owner module: "
        f"{', '.join(_SCHEDULER_DISCOVERY_COMPAT_OWNER_MISSING)}"
    )
if _SCHEDULER_DISCOVERY_COMPAT_FACADE_MISSING:
    raise RuntimeError(
        "scheduler discovery compatibility names missing from facade: "
        f"{', '.join(_SCHEDULER_DISCOVERY_COMPAT_FACADE_MISSING)}"
    )
_SCHEDULER_DISCOVERY_COMPAT_OWNER_ALIASES = MappingProxyType(
    {
        facade_name: getattr(_scheduler_discovery, owner_name)
        for facade_name, owner_name in _SCHEDULER_DISCOVERY_COMPAT_ALIAS_OWNER_NAMES.items()
    }
)
_SCHEDULER_DISCOVERY_COMPAT_FACADE_ALIASES = MappingProxyType(
    {name: globals()[name] for name in _SCHEDULER_DISCOVERY_COMPAT_ALIAS_NAMES}
)
for (
    _scheduler_discovery_facade_name,
    _scheduler_discovery_owner_value,
) in _SCHEDULER_DISCOVERY_COMPAT_OWNER_ALIASES.items():
    if _SCHEDULER_DISCOVERY_COMPAT_FACADE_ALIASES[_scheduler_discovery_facade_name] is not (
        _scheduler_discovery_owner_value
    ):
        raise RuntimeError(
            f"scheduler discovery direct alias drifted from owner module: {_scheduler_discovery_facade_name}"
        )
del _scheduler_discovery_facade_name, _scheduler_discovery_owner_value


_source_forecast_hours = _scheduler_candidates._source_forecast_hours
_source_policy_identity = _scheduler_candidates._source_policy_identity
_source_object_identity = _scheduler_candidates._source_object_identity
_accepted_horizon_from_hours = _scheduler_candidates._accepted_horizon_from_hours

_SCHEDULER_CANDIDATE_COMPAT_ALIAS_OWNER_NAMES = MappingProxyType(
    {
        "MAX_CANDIDATES": "MAX_CANDIDATES",
        "_source_blocked_evidence": "_source_blocked_evidence",
        "_reason_code": "_reason_code",
        "_canonical_readiness_unavailable_evidence": "_canonical_readiness_unavailable_evidence",
        "_canonical_candidate_row_count": "_canonical_candidate_row_count",
        "_canonical_evidence_is_fresh_zero_row": "_canonical_evidence_is_fresh_zero_row",
        "_candidate_is_fresh_full_chain": "_candidate_is_fresh_full_chain",
        "_blocked_candidate": "_blocked_candidate",
        "_candidate_with_state_evidence": "_candidate_with_state_evidence",
        "_merge_state_evidence": "_merge_state_evidence",
        "_slurm_status_sync_failed_evidence": "_slurm_status_sync_failed_evidence",
        "_source_forecast_hours": "_source_forecast_hours",
        "_source_policy_identity": "_source_policy_identity",
        "_source_object_identity": "_source_object_identity",
        "_accepted_horizon_from_hours": "_accepted_horizon_from_hours",
    }
)
_SCHEDULER_CANDIDATE_COMPAT_ALIAS_NAMES = tuple(_SCHEDULER_CANDIDATE_COMPAT_ALIAS_OWNER_NAMES)
_SCHEDULER_CANDIDATE_COMPAT_FORWARDER_NAMES = (
    "_candidate_construction_context",
    "_build_candidates",
)
_SCHEDULER_CANDIDATE_COMPAT_OWNER_MISSING = tuple(
    owner_name
    for owner_name in _SCHEDULER_CANDIDATE_COMPAT_ALIAS_OWNER_NAMES.values()
    if not hasattr(_scheduler_candidates, owner_name)
)
_SCHEDULER_CANDIDATE_COMPAT_FACADE_MISSING = (
    *tuple(name for name in _SCHEDULER_CANDIDATE_COMPAT_ALIAS_NAMES if name not in globals()),
    *tuple(name for name in _SCHEDULER_CANDIDATE_COMPAT_FORWARDER_NAMES if not hasattr(ProductionScheduler, name)),
)
if _SCHEDULER_CANDIDATE_COMPAT_OWNER_MISSING:
    raise RuntimeError(
        "scheduler candidate compatibility names missing from owner module: "
        f"{', '.join(_SCHEDULER_CANDIDATE_COMPAT_OWNER_MISSING)}"
    )
if _SCHEDULER_CANDIDATE_COMPAT_FACADE_MISSING:
    raise RuntimeError(
        "scheduler candidate compatibility names missing from facade: "
        f"{', '.join(_SCHEDULER_CANDIDATE_COMPAT_FACADE_MISSING)}"
    )
_SCHEDULER_CANDIDATE_COMPAT_OWNER_ALIASES = MappingProxyType(
    {
        facade_name: getattr(_scheduler_candidates, owner_name)
        for facade_name, owner_name in _SCHEDULER_CANDIDATE_COMPAT_ALIAS_OWNER_NAMES.items()
    }
)
_SCHEDULER_CANDIDATE_COMPAT_FACADE_ALIASES = MappingProxyType(
    {name: globals()[name] for name in _SCHEDULER_CANDIDATE_COMPAT_ALIAS_NAMES}
)
for (
    _scheduler_candidate_facade_name,
    _scheduler_candidate_owner_value,
) in _SCHEDULER_CANDIDATE_COMPAT_OWNER_ALIASES.items():
    if _SCHEDULER_CANDIDATE_COMPAT_FACADE_ALIASES[_scheduler_candidate_facade_name] is not (
        _scheduler_candidate_owner_value
    ):
        raise RuntimeError(
            f"scheduler candidate direct alias drifted from owner module: {_scheduler_candidate_facade_name}"
        )
del _scheduler_candidate_facade_name, _scheduler_candidate_owner_value


_SCHEDULER_EXECUTION_COMPAT_WRAPPER_OWNER_NAMES = MappingProxyType(
    {
        "_restart_compatible_candidate_cohorts": "restart_compatible_candidate_cohorts",
        "_candidate_restart_stage": "candidate_restart_stage",
        "_candidate_restart_cohort_key": "candidate_restart_cohort_key",
        "_candidate_execution_cohort_run_id": "candidate_execution_cohort_run_id",
        "_candidate_execution_cohorts": "candidate_execution_cohorts",
        "_candidate_execution_cohort_run_id_for_candidate": "candidate_execution_cohort_run_id_for_candidate",
    }
)
_SCHEDULER_EXECUTION_COMPAT_WRAPPER_NAMES = tuple(_SCHEDULER_EXECUTION_COMPAT_WRAPPER_OWNER_NAMES)
_SCHEDULER_EXECUTION_COMPAT_FORWARDER_OWNER_NAMES = MappingProxyType(
    {
        "_produce_forcing_for_candidates": "produce_forcing_for_candidates",
        "_execute_candidates": "execute_candidates",
        "_execute_candidate_cohort": "execute_candidate_cohort",
        "_scheduler_execution_context": "SchedulerExecutionContext",
    }
)
_SCHEDULER_EXECUTION_COMPAT_FORWARDER_NAMES = tuple(_SCHEDULER_EXECUTION_COMPAT_FORWARDER_OWNER_NAMES)
_SCHEDULER_EXECUTION_COMPAT_OWNER_MISSING = tuple(
    owner_name
    for owner_name in (
        *_SCHEDULER_EXECUTION_COMPAT_WRAPPER_OWNER_NAMES.values(),
        *_SCHEDULER_EXECUTION_COMPAT_FORWARDER_OWNER_NAMES.values(),
    )
    if not hasattr(_scheduler_execution, owner_name)
)
_SCHEDULER_EXECUTION_COMPAT_FACADE_MISSING = (
    *tuple(name for name in _SCHEDULER_EXECUTION_COMPAT_WRAPPER_NAMES if name not in globals()),
    *tuple(name for name in _SCHEDULER_EXECUTION_COMPAT_FORWARDER_NAMES if not hasattr(ProductionScheduler, name)),
)
if _SCHEDULER_EXECUTION_COMPAT_OWNER_MISSING:
    raise RuntimeError(
        "scheduler execution compatibility names missing from owner module: "
        f"{', '.join(_SCHEDULER_EXECUTION_COMPAT_OWNER_MISSING)}"
    )
if _SCHEDULER_EXECUTION_COMPAT_FACADE_MISSING:
    raise RuntimeError(
        "scheduler execution compatibility names missing from facade: "
        f"{', '.join(_SCHEDULER_EXECUTION_COMPAT_FACADE_MISSING)}"
    )
_SCHEDULER_EXECUTION_COMPAT_OWNER_WRAPPERS = MappingProxyType(
    {
        facade_name: getattr(_scheduler_execution, owner_name)
        for facade_name, owner_name in _SCHEDULER_EXECUTION_COMPAT_WRAPPER_OWNER_NAMES.items()
    }
)
_SCHEDULER_EXECUTION_COMPAT_FACADE_WRAPPERS = MappingProxyType(
    {name: globals()[name] for name in _SCHEDULER_EXECUTION_COMPAT_WRAPPER_NAMES}
)


def _bounded_evidence_payload(
    payload: Mapping[str, Any],
    *,
    reason: str,
    max_evidence_bytes: int | None = None,
) -> dict[str, Any]:
    return _scheduler_evidence.bounded_evidence_payload(
        payload,
        reason=reason,
        max_evidence_bytes=MAX_EVIDENCE_BYTES if max_evidence_bytes is None else max_evidence_bytes,
    )


def _evidence_status(evidence: Mapping[str, Any], fallback: str) -> str:
    return _scheduler_evidence.evidence_status(evidence, fallback)


_SCHEDULER_EVIDENCE_COMPAT_DIRECT_OWNER_NAMES = MappingProxyType(
    {
        "MAX_EVIDENCE_BYTES": "MAX_EVIDENCE_BYTES",
        "SCHEDULER_EVIDENCE_SCHEMA_VERSION": "SCHEDULER_EVIDENCE_SCHEMA_VERSION",
        "SCHEDULER_EVIDENCE_CONTRACT_ID": "SCHEDULER_EVIDENCE_CONTRACT_ID",
        "SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE": "SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE",
        "SCHEDULER_EVIDENCE_GITHUB_ISSUE": "SCHEDULER_EVIDENCE_GITHUB_ISSUE",
        "MODEL_RUN_EVIDENCE_SCHEMA_VERSION": "MODEL_RUN_EVIDENCE_SCHEMA_VERSION",
        "UNKNOWN_AFTER_ATTEMPT": "UNKNOWN_AFTER_ATTEMPT",
        "SchedulerEvidenceWriteError": "SchedulerEvidenceWriteError",
    }
)
_SCHEDULER_EVIDENCE_COMPAT_DIRECT_NAMES = tuple(_SCHEDULER_EVIDENCE_COMPAT_DIRECT_OWNER_NAMES)
_SCHEDULER_EVIDENCE_COMPAT_FORWARDER_OWNER_NAMES = MappingProxyType(
    {
        "_write_prelock_blocked_evidence": "write_prelock_blocked_evidence",
        "_reserve_pre_execution_evidence": "reserve_pre_execution_evidence",
        "_scheduler_evidence_write_context": "SchedulerEvidenceWriteContext",
        "_base_evidence": "base_evidence",
        "_write_evidence": "write_evidence",
    }
)
_SCHEDULER_EVIDENCE_COMPAT_FORWARDER_NAMES = tuple(_SCHEDULER_EVIDENCE_COMPAT_FORWARDER_OWNER_NAMES)
_SCHEDULER_EVIDENCE_COMPAT_WRAPPER_OWNER_NAMES = MappingProxyType(
    {
        "_candidate_evidence_write_blocked_evidence": "candidate_evidence_write_blocked_evidence",
        "_cancel_candidate_evidence_write_blocked_evidence": "cancel_candidate_evidence_write_blocked_evidence",
        "_sync_candidate_evidence_write_blocked_evidence": "sync_candidate_evidence_write_blocked_evidence",
        "_evidence_reservation_blocked_payload": "evidence_reservation_blocked_payload",
        "_evidence_write_error_payload": "evidence_write_error_payload",
        "_scheduler_resolved_runtime_roots": "scheduler_resolved_runtime_roots",
        "_root_evidence_item": "root_evidence_item",
        "_scheduler_runtime_config_evidence": "scheduler_runtime_config_evidence",
        "_open_evidence_directory": "open_evidence_directory",
        "_write_new_regular_file": "write_new_regular_file",
        "_require_evidence_artifact_available": "require_evidence_artifact_available",
        "_bounded_evidence_payload": "bounded_evidence_payload",
        "_evidence_status": "evidence_status",
        "_execution_write_proof": "execution_write_proof",
        "_execution_write_proof_from_evidence": "execution_write_proof_from_evidence",
        "_no_mutation_proof": "no_mutation_proof",
    }
)
_SCHEDULER_EVIDENCE_COMPAT_WRAPPER_NAMES = tuple(_SCHEDULER_EVIDENCE_COMPAT_WRAPPER_OWNER_NAMES)
_SCHEDULER_EVIDENCE_COMPAT_OWNER_MISSING = tuple(
    owner_name
    for owner_name in (
        *_SCHEDULER_EVIDENCE_COMPAT_DIRECT_OWNER_NAMES.values(),
        *_SCHEDULER_EVIDENCE_COMPAT_FORWARDER_OWNER_NAMES.values(),
        *_SCHEDULER_EVIDENCE_COMPAT_WRAPPER_OWNER_NAMES.values(),
    )
    if not hasattr(_scheduler_evidence, owner_name)
)
_SCHEDULER_EVIDENCE_COMPAT_FACADE_MISSING = (
    *tuple(name for name in _SCHEDULER_EVIDENCE_COMPAT_DIRECT_NAMES if name not in globals()),
    *tuple(name for name in _SCHEDULER_EVIDENCE_COMPAT_FORWARDER_NAMES if not hasattr(ProductionScheduler, name)),
    *tuple(name for name in _SCHEDULER_EVIDENCE_COMPAT_WRAPPER_NAMES if name not in globals()),
)
if _SCHEDULER_EVIDENCE_COMPAT_OWNER_MISSING:
    raise RuntimeError(
        "scheduler evidence compatibility names missing from owner module: "
        f"{', '.join(_SCHEDULER_EVIDENCE_COMPAT_OWNER_MISSING)}"
    )
if _SCHEDULER_EVIDENCE_COMPAT_FACADE_MISSING:
    raise RuntimeError(
        "scheduler evidence compatibility names missing from facade: "
        f"{', '.join(_SCHEDULER_EVIDENCE_COMPAT_FACADE_MISSING)}"
    )
_SCHEDULER_EVIDENCE_COMPAT_OWNER_DIRECTS = MappingProxyType(
    {
        facade_name: getattr(_scheduler_evidence, owner_name)
        for facade_name, owner_name in _SCHEDULER_EVIDENCE_COMPAT_DIRECT_OWNER_NAMES.items()
    }
)
_SCHEDULER_EVIDENCE_COMPAT_FACADE_DIRECTS = MappingProxyType(
    {name: globals()[name] for name in _SCHEDULER_EVIDENCE_COMPAT_DIRECT_NAMES}
)
for (
    _scheduler_evidence_direct_name,
    _scheduler_evidence_owner_value,
) in _SCHEDULER_EVIDENCE_COMPAT_OWNER_DIRECTS.items():
    if _SCHEDULER_EVIDENCE_COMPAT_FACADE_DIRECTS[_scheduler_evidence_direct_name] is not (
        _scheduler_evidence_owner_value
    ):
        raise RuntimeError(
            f"scheduler evidence direct alias drifted from owner module: {_scheduler_evidence_direct_name}"
        )
del _scheduler_evidence_direct_name, _scheduler_evidence_owner_value
_SCHEDULER_EVIDENCE_COMPAT_OWNER_WRAPPERS = MappingProxyType(
    {
        facade_name: getattr(_scheduler_evidence, owner_name)
        for facade_name, owner_name in _SCHEDULER_EVIDENCE_COMPAT_WRAPPER_OWNER_NAMES.items()
    }
)
_SCHEDULER_EVIDENCE_COMPAT_FACADE_WRAPPERS = MappingProxyType(
    {name: globals()[name] for name in _SCHEDULER_EVIDENCE_COMPAT_WRAPPER_NAMES}
)


def _now(config: ProductionSchedulerConfig) -> datetime:
    return config.now or datetime.now(UTC)


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
