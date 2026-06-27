from __future__ import annotations

import json
import os
import re
import stat
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from errno import EACCES, ELOOP, ENOTDIR, EPERM
from functools import wraps
from pathlib import Path
from threading import RLock
from types import FunctionType, MappingProxyType
from typing import Any, Protocol
from uuid import uuid4

from packages.common.model_registry import PsycopgModelRegistryStore
from packages.common.redaction import redact_payload
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
from services.orchestrator import scheduler_preflight as _scheduler_preflight
from services.orchestrator import scheduler_state as _scheduler_state_module
from services.orchestrator.chain import (
    ForecastOrchestrator,
    OrchestratorConfig,
    PipelineResult,
    _retry_service_from_env,
    scenario_for_source,
)
from services.orchestrator.production_contract import (
    production_identity_contract_evidence,
    production_stage_for,
    production_status_for,
)
from services.orchestrator.reservation import (
    SubmitOverlapReceipt,
    run_concurrent_submissions,
    timed_submission,
)
from services.orchestrator.retention import RetentionConfig, run_retention
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
from services.slurm_gateway.config import DEFAULT_JOB_TYPE_TEMPLATES
from services.slurm_gateway.gateway import ConfigurationError
from services.slurm_gateway.resource_validation import ResourceProfileValidationError, validate_resource_profile
from workers.canonical_converter.converter import evaluate_canonical_readiness
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


SchedulerResourceLimitError = _scheduler_discovery.SchedulerResourceLimitError


SchedulerEvidenceWriteError = _scheduler_evidence.SchedulerEvidenceWriteError


class ModelRegistryReader(Protocol):
    def list_models(
        self,
        *,
        basin_version_id: str | None,
        active: bool | None,
        limit: int,
        offset: int,
    ) -> Mapping[str, Any]:
        raise NotImplementedError

    def get_model(self, model_id: str) -> Mapping[str, Any]:
        raise NotImplementedError

    def get_model_internal(self, model_id: str) -> Mapping[str, Any]:
        raise NotImplementedError


class CycleDiscoveryAdapter(Protocol):
    def discover_cycles(
        self,
        cycle_date: str | date | datetime,
        end_date: str | date | datetime | None = None,
    ) -> list[CycleDiscovery]:
        raise NotImplementedError


class ActiveCandidateRepository(Protocol):
    def has_active_orchestration(self, *, source_id: str, cycle_time: datetime) -> bool:
        raise NotImplementedError

    def has_active_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        raise NotImplementedError

    def has_completed_pipeline(self, *, source_id: str, cycle_time: datetime, model_id: str) -> bool:
        raise NotImplementedError

    def active_slurm_jobs(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
    ) -> Sequence[Mapping[str, Any]]:
        raise NotImplementedError

    def candidate_state(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
        run_id: str,
        forcing_version_id: str,
        candidate_id: str,
    ) -> Mapping[str, Any] | None:
        raise NotImplementedError


class CanonicalReadinessProvider(Protocol):
    def canonical_readiness(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        forecast_hours: Sequence[int],
        policy_identity: Mapping[str, Any],
        source_object_identity: Mapping[str, Any],
        canonical_product_id: str,
        model_id: str,
        basin_id: str,
    ) -> Mapping[str, Any]:
        raise NotImplementedError


class ForcingProducerRunner(Protocol):
    def produce(
        self,
        *,
        source_id: str | None = None,
        cycle_time: str | datetime,
        model_id: str,
        max_lead_hours: int | None = None,
        basin_id: str | None = None,
        basin_version_id: str | None = None,
        river_network_version_id: str | None = None,
        canonical_product_id: str | None = None,
        canonical_identity: Mapping[str, Any] | None = None,
    ) -> Any:
        raise NotImplementedError


class ProductionOrchestratorFactory(Protocol):
    def __call__(self, source_id: str) -> ForecastOrchestrator:
        raise NotImplementedError


_CANONICAL_READINESS_PROVIDER_UNSET = object()


class _UnavailableCanonicalReadinessProvider:
    def __init__(self, *, reason: str, dependency: str, retryable: bool) -> None:
        self.reason = reason
        self.dependency = dependency
        self.retryable = retryable

    def canonical_readiness(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        forecast_hours: Sequence[int],
        policy_identity: Mapping[str, Any],
        source_object_identity: Mapping[str, Any],
        canonical_product_id: str,
        model_id: str,
        basin_id: str,
    ) -> Mapping[str, Any]:
        discovery = CycleDiscovery(
            cycle_id=cycle_id_for(source_id, cycle_time),
            source_id=source_id,
            cycle_time=cycle_time,
            cycle_hour=cycle_time.hour,
            available=True,
            status="discovered",
        )
        candidate = SchedulerCandidate(
            candidate_id=f"{source_id}:{_format_utc(cycle_time)}:{model_id}:canonical_readiness",
            source_id=source_id,
            cycle_id=cycle_id_for(source_id, cycle_time),
            cycle_time_utc=cycle_time,
            model_id=model_id,
            basin_id=basin_id,
            basin_version_id=None,
            river_network_version_id=None,
            segment_count=None,
            output_segment_count=None,
            model_package_uri=None,
            resource_profile={},
            display_capabilities={},
            frequency_capabilities={},
            horizon={},
            scenario_id="canonical_readiness",
            run_id="",
            forcing_version_id="",
            status="blocked",
        )
        return _canonical_readiness_unavailable_evidence(
            discovery,
            candidate,
            forecast_hours=forecast_hours,
            policy_identity=policy_identity,
            source_object_identity=source_object_identity,
            reason=self.reason,
            dependency=self.dependency,
            retryable=self.retryable,
        )


@dataclass(frozen=True)
class ProductionSchedulerConfig:
    workspace_root: Path | str = field(default_factory=lambda: os.getenv("WORKSPACE_ROOT", ".nhms-workspace"))
    object_store_root: Path | str | None = field(default_factory=lambda: os.getenv("OBJECT_STORE_ROOT"))
    published_artifact_root: Path | str | None = field(
        default_factory=lambda: os.getenv("NHMS_PUBLISHED_ARTIFACT_ROOT")
    )
    log_root: Path | str | None = field(
        default_factory=lambda: os.getenv("SLURM_SHARED_LOG_ROOT") or os.getenv("LOG_ROOT")
    )
    runtime_root: Path | str | None = field(
        default_factory=lambda: (
            os.getenv("NHMS_SCHEDULER_RUNTIME_ROOT")
            or os.getenv("NHMS_RUNTIME_ROOT")
            or os.getenv("RUN_WORKSPACE_ROOT")
            or os.getenv("SHUD_RUNTIME_ROOT")
        )
    )
    temp_root: Path | str | None = field(
        default_factory=lambda: (
            os.getenv("NHMS_SCHEDULER_TEMP_ROOT") or os.getenv("NHMS_TEMP_ROOT") or os.getenv("TMPDIR")
        )
    )
    scheduler_lock_root: Path | str | None = field(default_factory=lambda: os.getenv("NHMS_SCHEDULER_LOCK_ROOT"))
    scheduler_evidence_root: Path | str | None = field(
        default_factory=lambda: os.getenv("NHMS_SCHEDULER_EVIDENCE_ROOT")
    )
    service_role: str | None = field(default_factory=lambda: os.getenv("NHMS_SERVICE_ROLE"))
    require_runtime_roots: bool = field(default_factory=lambda: _env_flag("NHMS_SCHEDULER_REQUIRE_ROOTS"))
    database_url: str | None = field(default_factory=lambda: os.getenv("DATABASE_URL"))
    slurm_execution_enabled: bool = field(
        default_factory=lambda: _env_flag("NHMS_PRODUCTION_SLURM_ENABLED") or _env_flag("SLURM_EXECUTION_ENABLED")
    )
    slurm_gateway_url: str = field(default_factory=lambda: os.getenv("SLURM_GATEWAY_URL", "http://localhost:8000"))
    service_port: int = field(default_factory=lambda: _env_int("NHMS_SERVICE_PORT", DEFAULT_SERVICE_PORT))
    forcing_production_enabled: bool = field(default_factory=lambda: _env_flag("NHMS_PRODUCTION_FORCING_ENABLED"))
    allowed_storage_roots: tuple[Path | str, ...] = field(
        default_factory=lambda: _env_path_list("NHMS_SCHEDULER_ALLOWED_ROOTS")
    )
    slurm_job_type_templates: Mapping[str, str] | None = None
    slurm_env: Mapping[str, str] = field(default_factory=dict)
    cancel_active_slurm: bool = False
    sources: tuple[str, ...] = DEFAULT_PRODUCTION_SOURCES
    allowed_cycle_hours_utc: tuple[int, ...] = field(
        default_factory=lambda: _env_allowed_cycle_hours_utc(
            "NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC",
            DEFAULT_ALLOWED_CYCLE_HOURS_UTC,
        )
    )
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS
    cycle_lag_hours: int = DEFAULT_CYCLE_LAG_HOURS
    max_cycles_per_source: int = DEFAULT_MAX_CYCLES_PER_SOURCE
    backfill_enabled: bool = field(default_factory=lambda: _env_flag("NHMS_SCHEDULER_BACKFILL_ENABLED"))
    model_ids: tuple[str, ...] = ()
    basin_ids: tuple[str, ...] = ()
    dry_run: bool = True
    continuous: bool = False
    interval_seconds: float = 300.0
    retry_limit: int = field(default_factory=lambda: _env_int("NHMS_SCHEDULER_RETRY_LIMIT", DEFAULT_RETRY_LIMIT))
    concurrent_submit_bound: int = field(
        default_factory=lambda: _env_int("NHMS_SCHEDULER_CONCURRENT_SUBMIT_BOUND", DEFAULT_CONCURRENT_SUBMIT_BOUND)
    )
    # M24 §3A: recover reserved-unbound (submit-crash window) and in-flight jobs
    # at the start of each executing pass. Defaults on so a restarted scheduler
    # always reconciles before submitting; disable only for planning-only runs.
    restart_reconcile_enabled: bool = field(
        default_factory=lambda: _env_flag("NHMS_SCHEDULER_RESTART_RECONCILE", default=True)
    )
    candidate_state_job_limit: int = field(
        default_factory=lambda: _env_int("NHMS_CANDIDATE_STATE_JOB_LIMIT", DEFAULT_CANDIDATE_STATE_JOB_LIMIT)
    )
    candidate_state_event_limit: int = field(
        default_factory=lambda: _env_int("NHMS_CANDIDATE_STATE_EVENT_LIMIT", DEFAULT_CANDIDATE_STATE_EVENT_LIMIT)
    )
    scheduler_lock_backend: str = field(default_factory=lambda: os.getenv("NHMS_SCHEDULER_LOCK_BACKEND", "file"))
    lock_path: Path | str | None = None
    evidence_dir: Path | str | None = None
    lock_ttl_seconds: int = DEFAULT_LOCK_TTL_SECONDS
    now: datetime | None = None
    source_exclusions: tuple[dict[str, Any], ...] = field(init=False, default=())
    _workspace_root_preflight_path: Path = field(init=False, repr=False, compare=False)
    _object_store_root_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _published_artifact_root_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _runtime_root_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _temp_root_preflight_path: Path | None = field(init=False, repr=False, compare=False)
    _lock_root_preflight_path: Path = field(init=False, repr=False, compare=False)
    _evidence_root_preflight_path: Path = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        _reject_blank_config_path(self.workspace_root, "workspace_root")
        _reject_blank_config_path(self.lock_path, "lock_path")
        _reject_blank_config_path(self.evidence_dir, "evidence_dir")
        workspace_root_preflight_path = _config_path_preserve_final_component(self.workspace_root)
        workspace_root = workspace_root_preflight_path.resolve()
        object.__setattr__(self, "_workspace_root_preflight_path", workspace_root_preflight_path)
        object.__setattr__(self, "workspace_root", workspace_root)
        object_store_root_preflight_path = _optional_config_path_relative_to_preserve_final(
            self.object_store_root,
            workspace_root,
        )
        object.__setattr__(
            self,
            "object_store_root",
            _resolve_optional_config_path(object_store_root_preflight_path),
        )
        object.__setattr__(
            self,
            "_object_store_root_preflight_path",
            object_store_root_preflight_path,
        )
        published_artifact_root_preflight_path = _optional_config_path_relative_to_preserve_final(
            self.published_artifact_root,
            workspace_root,
        )
        object.__setattr__(
            self,
            "published_artifact_root",
            _resolve_optional_config_path(published_artifact_root_preflight_path),
        )
        object.__setattr__(
            self,
            "_published_artifact_root_preflight_path",
            published_artifact_root_preflight_path,
        )
        log_root_preflight_path = _optional_config_path_relative_to_preserve_final(self.log_root, workspace_root)
        object.__setattr__(self, "log_root", _resolve_optional_config_path(log_root_preflight_path))
        runtime_root_preflight_path = _optional_config_path_relative_to_preserve_final(
            self.runtime_root,
            workspace_root,
        )
        object.__setattr__(self, "runtime_root", _resolve_optional_config_path(runtime_root_preflight_path))
        object.__setattr__(self, "_runtime_root_preflight_path", runtime_root_preflight_path)
        temp_root_preflight_path = _optional_config_path_relative_to_preserve_final(self.temp_root, workspace_root)
        object.__setattr__(self, "temp_root", _resolve_optional_config_path(temp_root_preflight_path))
        object.__setattr__(self, "_temp_root_preflight_path", temp_root_preflight_path)
        scheduler_lock_root_preflight_path = _optional_config_path_relative_to_preserve_final(
            self.scheduler_lock_root,
            workspace_root,
        )
        object.__setattr__(
            self,
            "scheduler_lock_root",
            _resolve_optional_config_path(scheduler_lock_root_preflight_path),
        )
        scheduler_evidence_root_preflight_path = _optional_config_path_relative_to_preserve_final(
            self.scheduler_evidence_root,
            workspace_root,
        )
        object.__setattr__(
            self,
            "scheduler_evidence_root",
            _resolve_optional_config_path(scheduler_evidence_root_preflight_path),
        )
        object.__setattr__(self, "service_role", str(self.service_role).strip() if self.service_role else None)
        object.__setattr__(self, "database_url", str(self.database_url).strip() if self.database_url else None)
        allowed_roots = tuple(_optional_config_path(root) for root in self.allowed_storage_roots if root)
        object.__setattr__(self, "allowed_storage_roots", allowed_roots)
        templates = dict(self.slurm_job_type_templates or DEFAULT_JOB_TYPE_TEMPLATES)
        object.__setattr__(self, "slurm_job_type_templates", templates)
        object.__setattr__(self, "slurm_env", _production_slurm_env(dict(self.slurm_env)))
        object.__setattr__(self, "slurm_gateway_url", str(self.slurm_gateway_url or "").strip())
        object.__setattr__(self, "service_port", int(self.service_port))
        if len(self.sources) > MAX_SOURCES:
            raise ValueError(f"production scheduler source count exceeds limit {MAX_SOURCES}")
        sources, source_exclusions = _normalize_sources(self.sources)
        if len(sources) > MAX_SOURCES:
            raise ValueError(f"production scheduler source count exceeds limit {MAX_SOURCES}")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "source_exclusions", tuple(source_exclusions))
        object.__setattr__(
            self,
            "allowed_cycle_hours_utc",
            _normalize_allowed_cycle_hours_utc(self.allowed_cycle_hours_utc),
        )
        lookback_hours = max(int(self.lookback_hours), 0)
        if lookback_hours > MAX_LOOKBACK_HOURS:
            raise ValueError(f"production scheduler lookback_hours exceeds limit {MAX_LOOKBACK_HOURS}")
        object.__setattr__(self, "lookback_hours", lookback_hours)
        object.__setattr__(self, "cycle_lag_hours", max(int(self.cycle_lag_hours), 0))
        max_cycles_per_source = int(self.max_cycles_per_source)
        if max_cycles_per_source < 1:
            raise ValueError("production scheduler max_cycles_per_source must be at least 1")
        if max_cycles_per_source > MAX_CYCLES_PER_SOURCE:
            raise ValueError(f"production scheduler max_cycles_per_source exceeds limit {MAX_CYCLES_PER_SOURCE}")
        object.__setattr__(self, "max_cycles_per_source", max_cycles_per_source)
        object.__setattr__(self, "model_ids", tuple(str(model_id) for model_id in self.model_ids if model_id))
        object.__setattr__(self, "basin_ids", tuple(str(basin_id) for basin_id in self.basin_ids if basin_id))
        object.__setattr__(self, "interval_seconds", max(float(self.interval_seconds), 1.0))
        object.__setattr__(self, "retry_limit", max(int(self.retry_limit), 0))
        object.__setattr__(self, "concurrent_submit_bound", max(int(self.concurrent_submit_bound), 1))
        object.__setattr__(self, "candidate_state_job_limit", max(int(self.candidate_state_job_limit), 1))
        object.__setattr__(self, "candidate_state_event_limit", max(int(self.candidate_state_event_limit), 1))
        object.__setattr__(self, "lock_ttl_seconds", max(int(self.lock_ttl_seconds), 1))
        lock_backend = str(self.scheduler_lock_backend or "file").strip().lower()
        if lock_backend not in {"file", "postgres"}:
            raise ValueError("production scheduler scheduler_lock_backend must be 'file' or 'postgres'")
        object.__setattr__(self, "scheduler_lock_backend", lock_backend)
        if self.lock_path is None:
            lock_root = (
                Path(self.scheduler_lock_root) if self.scheduler_lock_root is not None else workspace_root / "scheduler"
            )
            lock_root_preflight_path = (
                scheduler_lock_root_preflight_path
                if scheduler_lock_root_preflight_path is not None
                else workspace_root / "scheduler"
            )
            lock_path = _confined_path(
                lock_root / "production-scheduler.lock",
                workspace_root,
                "lock_path",
            )
            object.__setattr__(self, "_lock_root_preflight_path", lock_root_preflight_path)
            object.__setattr__(self, "lock_path", lock_path)
        else:
            lock_path_preflight_path = _config_path_relative_to_preserve_final(self.lock_path, workspace_root)
            lock_path = _confined_path(self.lock_path, workspace_root, "lock_path")
            _require_under_workspace(lock_path, workspace_root, "lock_path")
            object.__setattr__(self, "_lock_root_preflight_path", lock_path_preflight_path.parent)
            object.__setattr__(self, "lock_path", lock_path)
        if self.evidence_dir is None:
            evidence_root = (
                Path(self.scheduler_evidence_root)
                if self.scheduler_evidence_root is not None
                else workspace_root / "scheduler" / "evidence"
            )
            evidence_root_preflight_path = (
                scheduler_evidence_root_preflight_path
                if scheduler_evidence_root_preflight_path is not None
                else workspace_root / "scheduler" / "evidence"
            )
            evidence_dir = _confined_path(
                evidence_root,
                workspace_root,
                "evidence_dir",
            )
            _require_safe_directory_final_component(evidence_dir, workspace_root, "evidence_dir")
            object.__setattr__(self, "_evidence_root_preflight_path", evidence_root_preflight_path)
            object.__setattr__(self, "evidence_dir", evidence_dir)
        else:
            evidence_dir_preflight_path = _config_path_relative_to_preserve_final(self.evidence_dir, workspace_root)
            evidence_dir = _confined_path(self.evidence_dir, workspace_root, "evidence_dir")
            _require_under_workspace(evidence_dir, workspace_root, "evidence_dir")
            _require_safe_directory_final_component(evidence_dir, workspace_root, "evidence_dir")
            object.__setattr__(self, "_evidence_root_preflight_path", evidence_dir_preflight_path)
            object.__setattr__(self, "evidence_dir", evidence_dir)
        if self.now is not None:
            object.__setattr__(self, "now", _ensure_utc(self.now))


@dataclass(frozen=True)
class SchedulerPassResult:
    pass_id: str
    status: str
    evidence: dict[str, Any]
    artifact_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.evidence)
        if self.artifact_path is not None:
            payload.setdefault("artifact_path", str(self.artifact_path))
        return payload


@dataclass(frozen=True)
class RegisteredSchedulerModel:
    model_id: str
    basin_id: str
    basin_version_id: str
    river_network_version_id: str
    segment_count: int | None
    output_segment_count: int | None
    model_package_uri: str
    shud_code_version: str
    resource_profile: Mapping[str, Any]
    resource_profile_summary: Mapping[str, Any]
    display_capabilities: Mapping[str, Any]
    frequency_capabilities: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "model_id": self.model_id,
            "basin_id": self.basin_id,
            "basin_version_id": self.basin_version_id,
            "river_network_version_id": self.river_network_version_id,
            "segment_count": self.segment_count,
            "output_segment_count": self.output_segment_count,
            "model_package_uri": _redact_secret_manifest_for_evidence(self.model_package_uri, "model_package_uri"),
            "shud_code_version": self.shud_code_version,
            "resource_profile": _resource_profile_evidence(self.resource_profile_summary),
            "display_capabilities": dict(self.display_capabilities),
            "frequency_capabilities": dict(self.frequency_capabilities),
        }
        project_identity = _resource_profile_project_identity(self.resource_profile)
        if project_identity is not None:
            payload.update(project_identity)
        return payload


def _resource_profile_project_identity(resource_profile: Mapping[str, Any]) -> dict[str, str] | None:
    project_name = resource_profile.get("project_name")
    shud_input_name = resource_profile.get("shud_input_name")
    project = str(project_name) if project_name not in (None, "") else None
    shud_input = str(shud_input_name) if shud_input_name not in (None, "") else None
    if project is None and shud_input is None:
        return None
    return {"project_name": project or shud_input or "", "shud_input_name": shud_input or project or ""}


@dataclass(frozen=True)
class SchedulerCandidate:
    candidate_id: str
    source_id: str
    cycle_id: str
    cycle_time_utc: datetime
    model_id: str
    basin_id: str
    basin_version_id: str
    river_network_version_id: str
    segment_count: int | None
    output_segment_count: int | None
    model_package_uri: str
    resource_profile: Mapping[str, Any]
    display_capabilities: Mapping[str, Any]
    frequency_capabilities: Mapping[str, Any]
    horizon: Mapping[str, Any]
    scenario_id: str
    run_id: str
    forcing_version_id: str
    status: str
    reason: str | None = None
    state_evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        contract_identity = _candidate_production_identity(self)
        payload = {
            "production_identity_contract": production_identity_contract_evidence(contract_identity),
            "candidate_id": self.candidate_id,
            "source_id": self.source_id,
            "source": self.source_id,
            "cycle_id": self.cycle_id,
            "cycle_time_utc": _format_utc(self.cycle_time_utc),
            "cycle_time": _format_utc(self.cycle_time_utc),
            "model_id": self.model_id,
            "basin_id": self.basin_id,
            "basin_version_id": self.basin_version_id,
            "river_network_version_id": self.river_network_version_id,
            "segment_count": self.segment_count,
            "output_segment_count": self.output_segment_count,
            "model_package_uri": _redact_secret_manifest_for_evidence(self.model_package_uri, "model_package_uri"),
            "resource_profile": _resource_profile_evidence(self.resource_profile),
            "display_capabilities": dict(self.display_capabilities),
            "frequency_capabilities": dict(self.frequency_capabilities),
            "horizon": dict(self.horizon),
            "scenario_id": self.scenario_id,
            "run_id": self.run_id,
            "canonical_product_id": contract_identity["canonical_product_id"],
            "forcing_version_id": self.forcing_version_id,
            "hydro_run_id": contract_identity["hydro_run_id"],
            "published_manifest_id": contract_identity["published_manifest_id"],
            "status": self.status,
            "reason": self.reason,
        }
        if contract_identity.get("pipeline_job_id") not in (None, ""):
            payload["pipeline_job_id"] = contract_identity["pipeline_job_id"]
        if self.state_evidence:
            payload["state_evidence"] = _evidence_safe(self.state_evidence)
        return payload


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
        self._source_readiness_context_cache.clear()
        started_at = _now(self.config)
        pass_id = f"scheduler_{format_cycle_time(started_at)}_{uuid4().hex[:12]}"
        root_preflight = _scheduler_lock_evidence_root_preflight(self.config)
        if root_preflight["status"] == "blocked":
            evidence = self._base_evidence(pass_id, started_at)
            evidence.update(
                {
                    "status": "preflight_blocked",
                    "finished_at": _format_utc(_now(self.config)),
                    "lock": {
                        "acquired": False,
                        "contention": False,
                        "lock_path": str(self.config.lock_path),
                        "reason": "scheduler_root_preflight_blocked",
                    },
                    "root_preflight": root_preflight,
                    "counts": _empty_counts(),
                    "candidates": [],
                    "blocked_candidates": [],
                    "skipped_candidates": [],
                    "duplicate_exclusions": list(self.config.source_exclusions),
                    "model_discovery": _empty_model_discovery(),
                    "source_cycles": [],
                    "model_run_evidence": [],
                    "slurm_cancellation_evidence": [],
                    "no_mutation_proof": _no_mutation_proof(),
                    "execution_boundary": "scheduler_root_preflight_blocked",
                }
            )
            artifact_path = self._write_prelock_blocked_evidence(pass_id, evidence, root_preflight)
            return SchedulerPassResult(
                pass_id=pass_id,
                status="preflight_blocked",
                evidence=evidence,
                artifact_path=artifact_path,
            )
        lock = self._build_scheduler_lease()
        lock_result = lock.acquire(pass_id=pass_id, started_at=started_at)
        if not lock_result["acquired"]:
            evidence = self._base_evidence(pass_id, started_at)
            evidence.update(
                {
                    "status": "lock_contended",
                    "finished_at": _format_utc(_now(self.config)),
                    "lock": lock_result,
                    "counts": _empty_counts(),
                    "candidates": [],
                    "blocked_candidates": [],
                    "model_discovery": _empty_model_discovery(),
                    "source_cycles": [],
                }
            )
            artifact_path = self._write_evidence(pass_id, evidence)
            status = _evidence_status(evidence, "lock_contended")
            return SchedulerPassResult(
                pass_id=pass_id,
                status=status,
                evidence=evidence,
                artifact_path=artifact_path,
            )

        heartbeat = _LeaseHeartbeat(lock, pass_id, max(1, self.config.lock_ttl_seconds // 3))
        heartbeat.start()
        try:
            root_preflight = _scheduler_runtime_root_preflight(self.config)
            if root_preflight["status"] == "blocked":
                finished_at = _now(self.config)
                evidence = self._base_evidence(pass_id, started_at)
                evidence.update(
                    {
                        "status": "preflight_blocked",
                        "finished_at": _format_utc(finished_at),
                        "lock": lock_result,
                        "root_preflight": root_preflight,
                        "counts": _empty_counts(),
                        "candidates": [],
                        "blocked_candidates": [],
                        "skipped_candidates": [],
                        "duplicate_exclusions": list(self.config.source_exclusions),
                        "model_discovery": _empty_model_discovery(),
                        "source_cycles": [],
                        "model_run_evidence": [],
                        "slurm_cancellation_evidence": [],
                        "no_mutation_proof": _no_mutation_proof(),
                        "execution_boundary": "scheduler_root_preflight_blocked",
                    }
                )
                artifact_path = self._write_evidence(pass_id, evidence)
                status = _evidence_status(evidence, "preflight_blocked")
                return SchedulerPassResult(
                    pass_id=pass_id,
                    status=status,
                    evidence=evidence,
                    artifact_path=artifact_path,
                )
            # M24 §3A: before planning/submitting this pass, recover any jobs
            # stuck in the submit-crash window (reserved-unbound) and refresh
            # in-flight statuses from accounting. Comment-reconcile finds back a
            # crashed cohort's slurm_job_id so we never re-submit an already
            # in-flight cohort.
            restart_reconcile_evidence = self._run_restart_reconcile()
            models, model_evidence = self._discover_models()
            cycles, source_cycle_evidence = self._discover_cycles(started_at, models=models)
            (
                candidates,
                blocked_candidates,
                skipped_candidates,
                candidate_duplicate_exclusions,
                slurm_status_sync_evidence,
            ) = self._build_candidates(models=models, cycles=cycles)
            cancellation_evidence: list[dict[str, Any]] = []
            pending_cancel_candidates = [
                candidate
                for candidate in skipped_candidates
                if candidate.get("reason") == "cancel_requested_active_slurm"
            ]
            cancel_active_slurm_requested = (
                self.config.cancel_active_slurm and not self.config.dry_run and bool(pending_cancel_candidates)
            )
            execution_evidence: list[dict[str, Any]] = []
            submitted_count = 0
            failed_count = 0
            partial_count = 0
            execution_boundary = "planning_only"
            pass_status = "planned"
            no_mutation_proof = _no_mutation_proof()
            execution_write_proof = _execution_write_proof()
            slurm_preflight_evidence: dict[str, Any] | None = None
            evidence_reservation: dict[str, Any] = {"status": "not_required"}
            pending_status_sync_candidates = [
                candidate
                for candidate in skipped_candidates
                if candidate.get("reason") == "active_slurm_status_sync_deferred"
            ]
            slurm_status_sync_proof = _slurm_status_sync_proof(sync_required=bool(pending_status_sync_candidates))
            slurm_cancellation_proof = _slurm_cancellation_proof()
            mutation_candidate_count = (
                len(candidates) + len(pending_cancel_candidates) + len(pending_status_sync_candidates)
            )
            # §4.2 lease: if the heartbeat reports our lease was taken over
            # mid-pass, short-circuit BEFORE any submission/cancellation so we
            # never race the new holder at the DB layer. The #290 DB reservation
            # would still prevent a real double-submit, but executing a doomed
            # pass wastes work and muddies evidence. Fall through to finally,
            # which stops the heartbeat and token-CAS releases the lock (a no-op
            # if it was already reclaimed).
            if heartbeat.lost:
                finished_at = _now(self.config)
                evidence = self._base_evidence(pass_id, started_at)
                evidence.update(
                    {
                        "status": "lease_lost",
                        "finished_at": _format_utc(finished_at),
                        "lock": lock_result,
                        "counts": _empty_counts(),
                        "candidates": [],
                        "blocked_candidates": [],
                        "skipped_candidates": [],
                        "duplicate_exclusions": list(self.config.source_exclusions),
                        "model_discovery": _empty_model_discovery(),
                        "source_cycles": [],
                        "no_mutation_proof": _no_mutation_proof(),
                        "execution_boundary": "lease_lost",
                    }
                )
                if root_preflight["status"] != "not_required":
                    evidence["root_preflight"] = root_preflight
                artifact_path = self._write_evidence(pass_id, evidence)
                status = _evidence_status(evidence, "lease_lost")
                return SchedulerPassResult(
                    pass_id=pass_id,
                    status=status,
                    evidence=evidence,
                    artifact_path=artifact_path,
                )
            if not self.config.dry_run and mutation_candidate_count:
                evidence_reservation = self._reserve_pre_execution_evidence(
                    pass_id,
                    started_at,
                    mutation_candidate_count,
                )
                if evidence_reservation["status"] == "blocked":
                    execution_evidence = [
                        _candidate_evidence_write_blocked_evidence(candidate, evidence_reservation)
                        for candidate in candidates
                    ]
                    execution_write_proof = _execution_write_proof_from_evidence(
                        execution_evidence,
                        reservation=evidence_reservation,
                    )
                    execution_evidence.extend(
                        _sync_candidate_evidence_write_blocked_evidence(candidate, evidence_reservation)
                        for candidate in pending_status_sync_candidates
                    )
                    cancellation_evidence = [
                        _cancel_candidate_evidence_write_blocked_evidence(candidate, evidence_reservation)
                        for candidate in pending_cancel_candidates
                    ]
                    execution_boundary = "evidence_preflight_blocked"
                    pass_status = "preflight_blocked"
                    slurm_status_sync_proof = _slurm_status_sync_proof(
                        sync_required=bool(pending_status_sync_candidates),
                        reservation=evidence_reservation,
                        blocked=True,
                    )
                    slurm_cancellation_proof = _slurm_cancellation_proof(
                        cancellation_required=bool(pending_cancel_candidates),
                        reservation=evidence_reservation,
                        blocked=True,
                    )
                else:
                    if pending_status_sync_candidates:
                        (
                            candidates,
                            blocked_candidates,
                            skipped_candidates,
                            candidate_duplicate_exclusions,
                            slurm_status_sync_evidence,
                        ) = self._build_candidates(
                            models=models,
                            cycles=cycles,
                            allow_slurm_status_sync=True,
                        )
                        pending_cancel_candidates = [
                            candidate
                            for candidate in skipped_candidates
                            if candidate.get("reason") == "cancel_requested_active_slurm"
                        ]
                        cancel_active_slurm_requested = (
                            self.config.cancel_active_slurm
                            and not self.config.dry_run
                            and bool(pending_cancel_candidates)
                        )
                    slurm_status_sync_proof = _slurm_status_sync_proof_from_candidates(
                        slurm_status_sync_evidence,
                        reservation=evidence_reservation,
                    )
                    if _slurm_status_sync_failed(slurm_status_sync_proof):
                        pass_status = "slurm_status_sync_failed"
                        execution_boundary = "slurm_status_sync"
                    else:
                        if cancel_active_slurm_requested:
                            cancellation_evidence = self._cancel_requested_active_slurm(skipped_candidates)
                            slurm_cancellation_proof = _slurm_cancellation_proof_from_evidence(
                                cancellation_evidence,
                                reservation=evidence_reservation,
                            )
                        if candidates and self.forcing_producer is not None:
                            (
                                candidates,
                                forcing_blocked_candidates,
                                forcing_evidence,
                            ) = self._produce_forcing_for_candidates(candidates)
                            blocked_candidates.extend(forcing_blocked_candidates)
                            execution_evidence.extend(forcing_evidence)
                        if candidates:
                            slurm_preflight = _slurm_preflight(self.config)
                            if slurm_preflight["status"] != "not_required":
                                slurm_preflight_evidence = redact_payload(slurm_preflight)
                            if slurm_preflight["status"] == "blocked":
                                execution_evidence.extend(
                                    [
                                        _candidate_slurm_preflight_blocked_evidence(candidate, slurm_preflight)
                                        for candidate in candidates
                                    ]
                                )
                                execution_write_proof = _execution_write_proof_from_evidence(
                                    execution_evidence,
                                    reservation=evidence_reservation,
                                )
                                execution_boundary = "slurm_preflight_blocked"
                                pass_status = "preflight_blocked"
                            elif self.orchestrator_factory is None and not self.config.slurm_execution_enabled:
                                execution_evidence.extend(
                                    [
                                        _candidate_preflight_blocked_evidence(candidate, config=self.config)
                                        for candidate in candidates
                                    ]
                                )
                                execution_write_proof = _execution_write_proof_from_evidence(
                                    execution_evidence,
                                    reservation=evidence_reservation,
                                )
                                execution_boundary = "preflight_blocked"
                                pass_status = "preflight_blocked"
                                no_mutation_proof = _no_mutation_proof()
                            else:
                                execution_evidence.extend(self._execute_candidates(candidates))
                                execution_write_proof = _execution_write_proof_from_evidence(
                                    execution_evidence,
                                    reservation=evidence_reservation,
                                )
                                submitted_count = sum(1 for item in execution_evidence if item.get("submitted") is True)
                                execution_boundary = (
                                    "slurm_gateway_orchestration"
                                    if self.config.slurm_execution_enabled
                                    else "production_orchestration"
                                )
                if execution_evidence:
                    pass_status = _scheduler_pass_status_from_execution(execution_evidence)
                if cancellation_evidence and not execution_evidence:
                    pass_status = _scheduler_pass_status_from_cancellation(cancellation_evidence)
                    execution_boundary = _scheduler_execution_boundary_from_cancellation(cancellation_evidence)
                elif cancellation_evidence and pass_status == "planned":
                    pass_status = _scheduler_pass_status_from_cancellation(cancellation_evidence)
                    execution_boundary = _scheduler_execution_boundary_from_cancellation(cancellation_evidence)
                if (
                    pass_status == "planned"
                    and execution_boundary == "planning_only"
                    and _slurm_status_sync_mutated(slurm_status_sync_proof)
                ):
                    pass_status = "slurm_status_synced"
                    execution_boundary = "slurm_status_sync"
                if pass_status == "planned" and not candidates and blocked_candidates:
                    pass_status = _blocked_pass_status(blocked_candidates)
                scheduler_mutation_proof = _scheduler_mutation_proof(
                    execution_write_proof=execution_write_proof,
                    slurm_status_sync_proof=slurm_status_sync_proof,
                    slurm_cancellation_proof=slurm_cancellation_proof,
                )
                no_mutation_proof = {
                    "adapter_download_called": False,
                    "slurm_submit_called": scheduler_mutation_proof["slurm_submit_called"],
                    "slurm_status_sync_called": slurm_status_sync_proof.get("sync_called") is True,
                    "slurm_cancellation_called": slurm_cancellation_proof.get("cancel_called") is True,
                    "shud_runtime_called": False,
                    "hydro_result_table_writes": scheduler_mutation_proof["hydro_result_table_writes"],
                    "met_result_table_writes": scheduler_mutation_proof["met_result_table_writes"],
                    "pipeline_status_writes": scheduler_mutation_proof["pipeline_status_writes"],
                    "pipeline_event_writes": scheduler_mutation_proof["pipeline_event_writes"],
                }
                failed_count = _scheduler_failed_count_from_execution(execution_evidence)
                partial_count = _scheduler_partial_count_from_execution(execution_evidence)
            finished_at = _now(self.config)
            evidence = self._base_evidence(pass_id, started_at)
            evidence["operator_filters"].update(model_evidence["operator_filters"])
            evidence["filters"] = dict(evidence["operator_filters"])
            duplicate_exclusions = [
                *self.config.source_exclusions,
                *[item for item in source_cycle_evidence if item.get("status") == "excluded"],
                *candidate_duplicate_exclusions,
            ]
            total_candidate_count = len(candidates) + len(blocked_candidates) + len(skipped_candidates)
            evidence.update(
                {
                    "status": pass_status,
                    "finished_at": _format_utc(finished_at),
                    "lock": lock_result,
                    "model_discovery": model_evidence,
                    "source_cycles": source_cycle_evidence,
                    "candidates": [candidate.to_dict() for candidate in candidates],
                    "blocked_candidates": [candidate.to_dict() for candidate in blocked_candidates],
                    "skipped_candidates": skipped_candidates,
                    "duplicate_exclusions": duplicate_exclusions,
                    "counts": {
                        "candidate_count": total_candidate_count,
                        "blocked_candidate_count": len(blocked_candidates),
                        "skipped_candidate_count": len(skipped_candidates),
                        "selected_model_count": len(models),
                        "source_cycle_count": len(cycles),
                        "submitted_count": submitted_count,
                        "failed_count": failed_count,
                        "partial_count": partial_count,
                        "slurm_status_sync_count": _slurm_status_sync_count(slurm_status_sync_proof),
                        "slurm_status_sync_unknown_count": _slurm_status_sync_unknown_count(
                            slurm_status_sync_proof,
                        ),
                        "slurm_cancelled_count": _slurm_cancelled_count(cancellation_evidence),
                        "slurm_cancellation_blocked_count": _slurm_cancellation_blocked_count(
                            cancellation_evidence,
                        ),
                        "slurm_cancellation_unknown_count": _slurm_cancellation_unknown_count(
                            slurm_cancellation_proof,
                        ),
                    },
                    "model_run_evidence": execution_evidence,
                    "execution_write_proof": execution_write_proof,
                    "slurm_cancellation_evidence": cancellation_evidence,
                    "slurm_status_sync_proof": slurm_status_sync_proof,
                    "slurm_cancellation_proof": slurm_cancellation_proof,
                    "no_mutation_proof": no_mutation_proof,
                    "execution_boundary": execution_boundary,
                }
            )
            if restart_reconcile_evidence is not None:
                evidence["restart_reconcile"] = restart_reconcile_evidence
            overlap_receipt = getattr(self, "_last_submit_overlap_receipt", None)
            if overlap_receipt is not None:
                # M24 §3A Evidence Floor: archive the overlapping-submit receipt
                # into the durable pass artifact (not just memory) so
                # "receipt shows overlapping submits" has on-disk proof.
                evidence["submit_overlap_receipt"] = overlap_receipt.to_dict()
            if slurm_preflight_evidence is not None:
                evidence["slurm_preflight"] = slurm_preflight_evidence
            if (
                not self.config.dry_run
                and mutation_candidate_count
                and evidence_reservation["status"] != "not_required"
            ):
                evidence["evidence_pre_execution"] = evidence_reservation
            if root_preflight["status"] != "not_required":
                evidence["root_preflight"] = root_preflight
            if self.config.backfill_enabled:
                evidence["backfill"] = {
                    "enabled": True,
                    "lookback_hours": self.config.lookback_hours,
                    "audit": [item for item in source_cycle_evidence if item.get("type") == "backfill_audit"],
                }
            else:
                evidence["backfill"] = {"enabled": False}
            evidence["retention"] = self._run_retention(started_at)
            try:
                artifact_path = self._write_evidence(pass_id, evidence)
            except (OSError, SchedulerEvidenceWriteError) as error:
                if evidence_reservation.get("status") != "blocked":
                    raise
                evidence["evidence_write_error"] = _evidence_write_error_payload(error)
                artifact_path = None
            status = _evidence_status(evidence, pass_status)
            return SchedulerPassResult(
                pass_id=pass_id,
                status=status,
                evidence=evidence,
                artifact_path=artifact_path,
            )
        except SchedulerResourceLimitError as error:
            finished_at = _now(self.config)
            evidence = self._base_evidence(pass_id, started_at)
            evidence.update(
                {
                    "status": "resource_limit_blocked",
                    "finished_at": _format_utc(finished_at),
                    "lock": lock_result,
                    "limit": {"reason": error.reason, **error.details},
                    "counts": _empty_counts(),
                    "candidates": [],
                    "blocked_candidates": [],
                    "skipped_candidates": [],
                    "duplicate_exclusions": list(self.config.source_exclusions),
                    "model_discovery": _empty_model_discovery(),
                    "source_cycles": [],
                    "no_mutation_proof": _no_mutation_proof(),
                    "execution_boundary": "planning_only",
                }
            )
            if root_preflight["status"] != "not_required":
                evidence["root_preflight"] = root_preflight
            artifact_path = self._write_evidence(pass_id, evidence)
            status = _evidence_status(evidence, "resource_limit_blocked")
            return SchedulerPassResult(
                pass_id=pass_id,
                status=status,
                evidence=evidence,
                artifact_path=artifact_path,
            )
        finally:
            try:
                heartbeat.stop()
            except Exception:
                pass
            lock.release(pass_id=pass_id)

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
        """Recover submit-crash and in-flight jobs at the start of an exec pass.

        Reconcile is read-only w.r.t. submission: it binds reserved-unbound rows
        back to their real slurm_job_id via the idempotency ``--comment`` and
        refreshes in-flight statuses from accounting. It NEVER re-submits, so an
        already in-flight cohort is recovered, not duplicated. Best-effort:
        failures are recorded but never abort the pass.
        """

        if self.config.dry_run or not self.config.restart_reconcile_enabled:
            return None
        store = self._restart_reconcile_store()
        if store is None:
            build_error = getattr(self, "_reconcile_store_build_error", None)
            if build_error is not None:
                return {
                    "status": "skipped",
                    "reason": "reconcile_store_build_failed",
                    "error_type": build_error,
                }
            return {"status": "skipped", "reason": "reconcile_store_unavailable"}

        from services.orchestrator.reconcile import (
            reconcile_inflight_jobs,
            reconcile_reserved_unbound_jobs,
        )

        evidence: dict[str, Any] = {"status": "completed"}
        try:
            comment_query = self._restart_reconcile_comment_query()
            reserved = reconcile_reserved_unbound_jobs(store, comment_query=comment_query)
            evidence["reserved_unbound"] = {
                "count": len(reserved),
                "outcomes": [
                    {
                        "job_id": o.job_id,
                        "idempotency_key": o.idempotency_key,
                        "action": o.action,
                        "status": o.status,
                        "slurm_job_id": o.slurm_job_id,
                    }
                    for o in reserved
                ],
            }
        except Exception as error:  # noqa: BLE001 - recovery must never abort the pass.
            evidence["status"] = "error"
            evidence["reserved_unbound_error"] = str(error)
            self._reset_reconcile_store_after_error()

        try:
            sacct_query = self._restart_reconcile_sacct_query()
            inflight = reconcile_inflight_jobs(store, sacct_query=sacct_query)
            evidence["inflight"] = {
                "count": len(inflight),
                "outcomes": [
                    {
                        "job_id": o.job_id,
                        "slurm_job_id": o.slurm_job_id,
                        "action": o.action,
                        "status": o.status,
                    }
                    for o in inflight
                ],
            }
        except Exception as error:  # noqa: BLE001 - recovery must never abort the pass.
            evidence["status"] = "error"
            evidence["inflight_error"] = str(error)
            self._reset_reconcile_store_after_error()
        return evidence

    def _reset_reconcile_store_after_error(self) -> None:
        """Recover the cached reconcile session after a write/commit failure.

        persistence commits with no rollback, so a failed commit leaves the
        cached session in pending-rollback state; reusing it next pass (or in
        the same pass's inflight segment) raises PendingRollbackError and
        silently kills crash recovery for the daemon's lifetime. Roll the
        session back to keep its connection reusable; only if rollback itself
        fails (the connection is truly dead) dispose the engine pool and drop
        the cache so the next pass rebuilds a clean store via
        _restart_reconcile_store.
        """

        store = self._reconcile_store
        if store is None:
            return
        try:
            store.session.rollback()
        except Exception:  # noqa: BLE001 - poisoned/dead session: dispose + drop so
            # the next pass rebuilds a clean one via _restart_reconcile_store.
            try:
                bind = store.session.get_bind()
                store.session.close()
                if hasattr(bind, "dispose"):
                    bind.dispose()
            except Exception:  # noqa: BLE001 - cleanup is best-effort; never abort the pass.
                pass
            self._reconcile_store = None

    def _restart_reconcile_store(self) -> Any | None:
        if self._reconcile_store is not None:
            return self._reconcile_store
        database_url = (self.config.database_url or "").strip()
        if not database_url:
            return None
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session

        from services.orchestrator.persistence import PipelineStore

        # Best-effort: a malformed/unbuildable database_url must never abort the
        # pass. make_url() raises synchronously inside create_engine for a bad
        # DSN, so wrap the whole build. ZERO-LEAK: record only the exception
        # class name (provably secret-free); the raw message embeds the DSN
        # incl. password. The submit-path DB-host preflight still runs.
        try:
            engine = create_engine(
                database_url,
                future=True,
                connect_args={
                    "connect_timeout": RECONCILE_DB_CONNECT_TIMEOUT_SECONDS,
                    "options": f"-c statement_timeout={RECONCILE_DB_STATEMENT_TIMEOUT_MS}",
                },
            )
            self._reconcile_store = PipelineStore(Session(engine))
        except Exception as error:  # noqa: BLE001 - build must never abort the pass.
            self._reconcile_store_build_error = type(error).__name__
            return None
        self._reconcile_store_build_error = None
        return self._reconcile_store

    def _restart_reconcile_comment_query(self) -> Callable[[str], Any]:
        if self._reconcile_comment_query is not None:
            return self._reconcile_comment_query
        from services.orchestrator.reconcile import default_comment_sacct_querier

        return default_comment_sacct_querier()

    def _restart_reconcile_sacct_query(self) -> Callable[[str], Any]:
        if self._reconcile_sacct_query is not None:
            return self._reconcile_sacct_query
        from services.orchestrator.reconcile import default_sacct_querier

        return default_sacct_querier()

    def _run_retention(self, started_at: datetime) -> dict[str, Any]:
        """Run forecast-data retention cleanup; never break the scheduling pass.

        Scheduler ``dry_run`` is the master switch: when the pass runs in
        dry-run (planning-only, no side effects), retention is forced into
        dry-run too, regardless of NHMS_RETENTION_DRY_RUN. This preserves the
        "dry_run => no side effects" contract so a planning pass never deletes
        aged artifacts even when the env enables real deletion.
        """
        retention_config = RetentionConfig.from_env()
        if not retention_config.enabled:
            return {"status": "disabled", "enabled": False}
        forced_dry_run = False
        if self.config.dry_run and not retention_config.dry_run:
            retention_config = replace(retention_config, dry_run=True)
            forced_dry_run = True
        try:
            result = run_retention(
                object_store_root=self.config.object_store_root,
                now=started_at,
                config=retention_config,
                published_artifact_root=self.config.published_artifact_root,
            )
        except Exception as error:  # noqa: BLE001 - cleanup must never abort scheduling
            return {"status": "error", "enabled": True, "error": str(error)}
        payload = result.to_dict()
        payload["status"] = "completed"
        if forced_dry_run:
            payload["forced_dry_run_by_scheduler"] = True
        return payload

    def _write_prelock_blocked_evidence(
        self,
        pass_id: str,
        evidence: dict[str, Any],
        root_preflight: Mapping[str, Any],
    ) -> Path | None:
        return _scheduler_evidence.write_prelock_blocked_evidence(
            self._scheduler_evidence_write_context(),
            pass_id,
            evidence,
            root_preflight,
            write_evidence_callback=self._write_evidence,
        )

    def _reserve_pre_execution_evidence(
        self,
        pass_id: str,
        started_at: datetime,
        candidate_count: int,
    ) -> dict[str, Any]:
        return _scheduler_evidence.reserve_pre_execution_evidence(
            self._scheduler_evidence_write_context(),
            pass_id,
            started_at,
            candidate_count,
            now=_now(self.config),
        )

    def _scheduler_evidence_write_context(self) -> _scheduler_evidence.SchedulerEvidenceWriteContext:
        return _scheduler_evidence.SchedulerEvidenceWriteContext(
            config=self.config,
            require_safe_directory_final_component=_require_safe_directory_final_component,
            require_under_workspace=_require_under_workspace,
            evidence_safe=_evidence_safe,
            max_evidence_bytes=MAX_EVIDENCE_BYTES,
            bounded_evidence_payload=_bounded_evidence_payload,
            open_evidence_directory=_open_evidence_directory,
            write_new_regular_file=lambda artifact_name, serialized, dir_fd, artifact_path: _write_new_regular_file(
                artifact_name,
                serialized,
                dir_fd=dir_fd,
                artifact_path=artifact_path,
            ),
            require_evidence_artifact_available=lambda artifact_name, dir_fd, artifact_path: (
                _require_evidence_artifact_available(
                    artifact_name,
                    dir_fd=dir_fd,
                    artifact_path=artifact_path,
                )
            ),
            reservation_blocked_payload=lambda pass_id, artifact_path, reason, details, evidence_safe: (
                _evidence_reservation_blocked_payload(
                    pass_id=pass_id,
                    artifact_path=artifact_path,
                    reason=reason,
                    details=details,
                )
            ),
            evidence_write_error_payload=_evidence_write_error_payload,
        )

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
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        for candidate in skipped_candidates:
            if candidate.get("reason") != "cancel_requested_active_slurm":
                continue
            source_id = str(candidate.get("source_id") or "")
            cycle_time_text = candidate.get("cycle_time_utc")
            if not source_id or not cycle_time_text:
                continue
            grouped.setdefault((source_id, str(cycle_time_text)), candidate)

        evidence: list[dict[str, Any]] = []
        for (source_id, cycle_time_text), skipped in sorted(grouped.items()):
            cycle_time = _ensure_utc(datetime.fromisoformat(cycle_time_text.replace("Z", "+00:00")))
            cycle_id = cycle_id_for(source_id, cycle_time)
            orchestrator = self._cancel_orchestrator_for(source_id)
            cancel = getattr(orchestrator, "cancel_active_cycle_jobs", None)
            if not callable(cancel):
                evidence.append(
                    {
                        "source_id": source_id,
                        "cycle_id": cycle_id,
                        "cycle_time_utc": cycle_time_text,
                        "status": "blocked",
                        "error_code": "SLURM_CANCEL_UNSUPPORTED",
                        "cancel_attempted": False,
                        "mutation_occurred": False,
                        "replacement_submitted": False,
                    }
                )
                continue
            try:
                cancelled = _bounded_active_slurm_jobs(
                    [dict(item) for item in cancel(cycle_id, reason="scheduler_cancel_requested")],
                    max_jobs=self.config.candidate_state_job_limit,
                )
            except Exception as error:
                evidence.append(
                    {
                        "source_id": source_id,
                        "cycle_id": cycle_id,
                        "cycle_time_utc": cycle_time_text,
                        "status": "failed",
                        "error_code": getattr(error, "error_code", "SLURM_CANCEL_FAILED"),
                        "error_message": _evidence_safe(getattr(error, "message", str(error))),
                        "cancel_attempted": True,
                        "mutation_outcome": UNKNOWN_AFTER_ATTEMPT,
                        "replacement_submitted": False,
                        "active_slurm_jobs": _evidence_safe(skipped.get("active_slurm_jobs", [])),
                        "residual_blockers": [
                            {
                                "code": getattr(error, "error_code", "SLURM_CANCEL_FAILED"),
                                "state": "blocked",
                                "quality_flag": "slurm_cancellation_failed",
                                "residual_risk": (
                                    "Slurm cancellation raised after the downstream cancellation method was called; "
                                    "mutation outcome is unknown."
                                ),
                            }
                        ],
                    }
                )
                continue
            cancellation_status = _scheduler_cancellation_status(cancelled)
            cancellation_item: dict[str, Any] = {
                "source_id": source_id,
                "cycle_id": cycle_id,
                "cycle_time_utc": cycle_time_text,
                "status": cancellation_status,
                "cancelled_jobs": _evidence_safe(cancelled),
                "cancel_attempted": True,
                "mutation_occurred": cancellation_status in {"cancelled", "partially_cancelled"},
                "replacement_submitted": False,
                "active_slurm_jobs": _evidence_safe(skipped.get("active_slurm_jobs", [])),
            }
            pipeline_status_write = any(_cancelled_job_pipeline_status_write(item) for item in cancelled)
            pipeline_event_write = any(_cancelled_job_pipeline_event_write(item) for item in cancelled)
            if pipeline_status_write:
                cancellation_item["pipeline_status_write"] = True
            if pipeline_event_write:
                cancellation_item["pipeline_event_write"] = True
            if cancellation_status != "cancelled":
                cancellation_item["error_code"] = "SLURM_CANCELLATION_GAP"
                cancellation_item["cancellation_proven"] = False
            evidence.append(cancellation_item)
        return evidence

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
        rows = _fetch_active_model_details(self.registry)
        exclusions: list[dict[str, Any]] = []
        runnable: list[RegisteredSchedulerModel] = []
        duplicate_exclusions = _active_model_duplicate_exclusions(rows)

        for index, row in enumerate(rows):
            duplicate_exclusion = duplicate_exclusions.get(index)
            if duplicate_exclusion is not None:
                exclusions.append(duplicate_exclusion)
                continue
            model = _coerce_registered_model(row)
            if isinstance(model, RegisteredSchedulerModel):
                runnable.append(model)
            else:
                exclusions.append(model)

        runnable.sort(key=lambda item: item.model_id)
        selected: list[RegisteredSchedulerModel] = []
        filter_excluded = 0
        for model in runnable:
            if not _matches_filters(model, model_ids=self.config.model_ids, basin_ids=self.config.basin_ids):
                filter_excluded += 1
                exclusions.append(
                    {
                        "model_id": model.model_id,
                        "basin_id": model.basin_id,
                        "basin_version_id": model.basin_version_id,
                        "reason": "operator_filter_excluded",
                    }
                )
                continue
            selected.append(model)

        evidence = {
            "active_model_count": len(rows),
            "runnable_model_count": len(runnable),
            "selected_model_count": len(selected),
            "excluded_model_count": len(exclusions),
            "models": [model.to_dict() for model in selected],
            "exclusions": exclusions,
        }
        evidence["operator_filters"] = {
            "expression": _filter_expression(self.config.model_ids, self.config.basin_ids),
            "excluded_runnable_count": filter_excluded,
        }
        return selected, evidence

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
    rows: list[Mapping[str, Any]] = []
    offset = 0
    limit = 500
    pages = 0
    while True:
        pages += 1
        if pages > MAX_REGISTRY_PAGES:
            raise SchedulerResourceLimitError(
                "registry_page_limit_exceeded",
                {"max_registry_pages": MAX_REGISTRY_PAGES, "model_count": len(rows)},
            )
        page = registry.list_models(basin_version_id=None, active=True, limit=limit, offset=offset)
        items = list(page.get("items") or [])
        for item in items:
            if len(rows) >= MAX_DISCOVERED_MODELS:
                raise SchedulerResourceLimitError(
                    "model_limit_exceeded",
                    {"max_discovered_models": MAX_DISCOVERED_MODELS, "model_count": len(rows)},
                )
            model_id = str(item.get("model_id") or "")
            rows.append(_fetch_scheduler_model_detail(registry, model_id) if model_id else item)
        total = int(page.get("total") or len(rows))
        offset += len(items)
        if len(items) == 0 or offset >= total:
            break
    return rows


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
    internal_getter = getattr(registry, "get_model_internal", None)
    if callable(internal_getter):
        return internal_getter(model_id)
    return registry.get_model(model_id)


def _coerce_registered_model(row: Mapping[str, Any]) -> RegisteredSchedulerModel | dict[str, Any]:
    resource_profile = row.get("resource_profile")
    if not isinstance(resource_profile, Mapping):
        resource_profile = {}
    lifecycle_state = str(row.get("lifecycle_state") or ("active" if row.get("active_flag") else "inactive"))
    required = {
        "model_id": row.get("model_id"),
        "basin_id": row.get("basin_id") or resource_profile.get("basin_id"),
        "basin_version_id": row.get("basin_version_id"),
        "river_network_version_id": row.get("river_network_version_id"),
        "model_package_uri": row.get("model_package_uri"),
        "shud_code_version": row.get("shud_code_version"),
    }
    if row.get("active_flag") is False or lifecycle_state != "active":
        return _model_exclusion(row, "inactive_model")
    if resource_profile.get("runnable") is False:
        return _model_exclusion(row, "not_runnable")
    if not required["shud_code_version"]:
        return _model_exclusion(row, "not_shud_model")
    missing = sorted(key for key, value in required.items() if value in (None, ""))
    if missing:
        return {**_model_exclusion(row, "incomplete_model_metadata"), "missing_fields": missing}

    segment_count = row.get("segment_count")
    output_segment_count = _coerce_output_segment_count(resource_profile, fallback=segment_count)
    return RegisteredSchedulerModel(
        model_id=str(required["model_id"]),
        basin_id=str(required["basin_id"]),
        basin_version_id=str(required["basin_version_id"]),
        river_network_version_id=str(required["river_network_version_id"]),
        segment_count=int(segment_count) if segment_count not in (None, "") else None,
        output_segment_count=output_segment_count,
        model_package_uri=str(required["model_package_uri"]),
        shud_code_version=str(required["shud_code_version"]),
        resource_profile=dict(resource_profile),
        resource_profile_summary=_resource_profile_summary(resource_profile),
        display_capabilities=_mapping_value(resource_profile.get("display_capabilities")),
        frequency_capabilities=_mapping_value(resource_profile.get("frequency_capabilities")),
    )


def _active_model_duplicate_exclusions(rows: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    duplicate_groups: list[tuple[str, str, set[int]]] = []
    for identity_field, groups in (
        ("model_id", _active_model_identity_groups(rows, _active_model_model_id)),
        ("model_package_uri", _active_model_identity_groups(rows, _active_model_package_uri)),
        ("package_checksum", _active_model_identity_groups(rows, _active_model_package_checksum)),
    ):
        for value, indexes in groups.items():
            if value and len(indexes) > 1:
                duplicate_groups.append((identity_field, value, indexes))

    exclusions: dict[int, dict[str, Any]] = {}
    for identity_field, value, indexes in duplicate_groups:
        duplicate_model_ids = sorted(
            str(rows[index].get("model_id") or "") for index in indexes if rows[index].get("model_id") not in (None, "")
        )
        for index in indexes:
            if index in exclusions:
                continue
            exclusions[index] = {
                **_model_exclusion(rows[index], "duplicate_active_model_identity"),
                "duplicate_identity_field": identity_field,
                "duplicate_identity_value": _model_duplicate_identity_value_for_evidence(identity_field, value),
                "duplicate_model_ids": duplicate_model_ids,
                "duplicate_active_model_count": len(indexes),
            }
    return exclusions


def _active_model_identity_groups(
    rows: Sequence[Mapping[str, Any]],
    value_getter: Callable[[Mapping[str, Any]], str | None],
) -> dict[str, set[int]]:
    groups: dict[str, set[int]] = {}
    for index, row in enumerate(rows):
        value = value_getter(row)
        if value:
            groups.setdefault(value, set()).add(index)
    return groups


def _active_model_model_id(row: Mapping[str, Any]) -> str | None:
    value = row.get("model_id")
    return str(value) if value not in (None, "") else None


def _active_model_package_uri(row: Mapping[str, Any]) -> str | None:
    value = row.get("model_package_uri")
    return str(value) if value not in (None, "") else None


def _active_model_package_checksum(row: Mapping[str, Any]) -> str | None:
    resource_profile = row.get("resource_profile")
    if not isinstance(resource_profile, Mapping):
        return None
    if not _has_package_specific_checksum_context(row, resource_profile):
        return None
    value = resource_profile.get("package_checksum")
    return str(value) if value not in (None, "") else None


def _has_package_specific_checksum_context(row: Mapping[str, Any], resource_profile: Mapping[str, Any]) -> bool:
    if row.get("model_package_uri") not in (None, ""):
        return True
    for identity_field in ("model_package_uri", "manifest_uri", "model_package_manifest_uri", "package_uri"):
        if resource_profile.get(identity_field) not in (None, ""):
            return True
    lineage = str(resource_profile.get("lineage") or "")
    return lineage in {"basins_registry_import", "qhh_production_bootstrap"}


def _model_duplicate_identity_value_for_evidence(field: str, value: str) -> str:
    if field == "model_package_uri":
        redacted = _redact_secret_manifest_for_evidence(value, "model_package_uri")
        return str(redacted)
    if field == "package_checksum":
        return "[redacted]"
    return value


def _resource_profile_summary(resource_profile: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "resource_profile_id",
        "cpu",
        "memory_gb",
        "walltime",
        "max_concurrent",
        "shud_threads",
        "station_count",
        "station_ids",
        "forcing_station_metadata",
        "manifest_uri",
        "output_segment_count",
        "output_uri",
        "display_capabilities",
        "frequency_capabilities",
    )
    return {key: resource_profile[key] for key in keys if key in resource_profile}


def _coerce_output_segment_count(resource_profile: Mapping[str, Any], *, fallback: Any = None) -> int | None:
    output_river = resource_profile.get("output_river")
    candidates: list[Any] = [
        resource_profile.get("output_segment_count"),
        resource_profile.get("shud_output_segment_count"),
        resource_profile.get("shud_output_river_count"),
    ]
    if isinstance(output_river, Mapping):
        candidates.extend(
            [
                output_river.get("output_segment_count"),
                output_river.get("segment_count"),
            ]
        )
    candidates.append(fallback)
    for value in candidates:
        if value in (None, ""):
            continue
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        if count >= 0:
            return count
    return None


def _mapping_value(value: Any) -> Mapping[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _model_exclusion(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "model_id": row.get("model_id"),
        "basin_id": row.get("basin_id"),
        "basin_version_id": row.get("basin_version_id"),
        "reason": reason,
    }


def _matches_filters(
    model: RegisteredSchedulerModel,
    *,
    model_ids: Sequence[str],
    basin_ids: Sequence[str],
) -> bool:
    if model_ids and model.model_id not in set(model_ids):
        return False
    return not (basin_ids and model.basin_id not in set(basin_ids) and model.basin_version_id not in set(basin_ids))


def _filter_expression(model_ids: Sequence[str], basin_ids: Sequence[str]) -> str | None:
    parts: list[str] = []
    if model_ids:
        parts.append("model_id in [" + ",".join(model_ids) + "]")
    if basin_ids:
        parts.append("basin_id in [" + ",".join(basin_ids) + "]")
    return " and ".join(parts) if parts else None


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
    source_id = normalize_source_id(discovery.source_id)
    cycle_time = _ensure_utc(discovery.cycle_time)
    compact_cycle = format_cycle_time(cycle_time)
    scenario_id = scenario_for_source(source_id)
    candidate_id = f"{source_id}:{_format_utc(cycle_time)}:{model.model_id}:{scenario_id}"
    return SchedulerCandidate(
        candidate_id=candidate_id,
        source_id=source_id,
        cycle_id=cycle_id_for(source_id, cycle_time),
        cycle_time_utc=cycle_time,
        model_id=model.model_id,
        basin_id=model.basin_id,
        basin_version_id=model.basin_version_id,
        river_network_version_id=model.river_network_version_id,
        segment_count=model.segment_count,
        output_segment_count=model.output_segment_count,
        model_package_uri=model.model_package_uri,
        resource_profile=model.resource_profile,
        display_capabilities=model.display_capabilities,
        frequency_capabilities=model.frequency_capabilities,
        horizon=horizon,
        scenario_id=scenario_id,
        run_id=f"fcst_{source_id.lower()}_{compact_cycle}_{model.model_id}",
        forcing_version_id=f"forc_{source_id.lower()}_{compact_cycle}_{model.model_id}",
        status="selected",
    )


def _candidate_max_lead_hours(candidate: SchedulerCandidate) -> int | None:
    for key in ("max_lead_hours", "forecast_horizon_hours"):
        value = candidate.horizon.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


_blocked_candidate = _scheduler_candidates._blocked_candidate
_candidate_with_state_evidence = _scheduler_candidates._candidate_with_state_evidence


def _candidate_with_forcing_result(candidate: SchedulerCandidate, result: Any) -> SchedulerCandidate:
    forcing_version_id = str(getattr(result, "forcing_version_id", candidate.forcing_version_id))
    file_uris = dict(getattr(result, "file_uris", {}) or {})
    forcing_package_uri = getattr(result, "forcing_package_uri", None)
    checksum = getattr(result, "checksum", None)
    resource_profile = dict(candidate.resource_profile)
    resource_profile.update(
        {
            "forcing_version_id": forcing_version_id,
            "forcing_package_uri": forcing_package_uri,
            "forcing_package_manifest_uri": file_uris.get("package_manifest"),
            "forcing_manifest_checksum": checksum,
            "forcing_uri": file_uris.get("tsd_forc") or forcing_package_uri,
            "forcing_file_uris": file_uris,
            "forcing_variable_count": getattr(result, "variable_count", None),
            "forcing_time_range": dict(getattr(result, "time_range", {}) or {}),
            "forcing_units": dict(getattr(result, "units", {}) or {}),
        }
    )
    if getattr(result, "station_count", None) not in (None, ""):
        resource_profile["station_count"] = getattr(result, "station_count")
    return replace(candidate, forcing_version_id=forcing_version_id, resource_profile=resource_profile)


def _candidate_scheduler_canonical_identity(candidate: SchedulerCandidate) -> dict[str, Any]:
    readiness = candidate.state_evidence.get("canonical_readiness")
    identity = {"canonical_product_id": _candidate_canonical_product_id(candidate)}
    if isinstance(readiness, Mapping):
        for key in ("policy_identity", "source_object_identity", "accepted_horizon", "expected_leads"):
            value = readiness.get(key)
            if value not in (None, ""):
                identity[key] = _evidence_safe(value)
    return _evidence_safe(identity)


_merge_state_evidence = _scheduler_candidates._merge_state_evidence


def _candidate_basin_manifest(
    candidate: SchedulerCandidate,
    *,
    output_uri: str,
    orchestration_run_id: str | None = None,
) -> dict[str, Any]:
    resource_profile = dict(candidate.resource_profile)
    manifest = {
        "candidate_id": candidate.candidate_id,
        "source_id": candidate.source_id,
        "cycle_id": candidate.cycle_id,
        "cycle_time": format_cycle_time(candidate.cycle_time_utc),
        "model_id": candidate.model_id,
        "basin_id": candidate.basin_id,
        "basin_version_id": candidate.basin_version_id,
        "river_network_version_id": candidate.river_network_version_id,
        "segment_count": candidate.segment_count,
        "output_segment_count": candidate.output_segment_count,
        "model_package_uri": candidate.model_package_uri,
        "model_package_manifest_uri": _model_package_manifest_uri(candidate),
        "resource_profile": dict(candidate.resource_profile),
        "display_capabilities": dict(candidate.display_capabilities),
        "frequency_capabilities": dict(candidate.frequency_capabilities),
        "scenario_id": candidate.scenario_id,
        "run_id": candidate.run_id,
        "canonical_product_id": _candidate_canonical_product_id(candidate),
        "forcing_version_id": candidate.forcing_version_id,
        "forcing_package_uri": resource_profile.get("forcing_package_uri"),
        "forcing_package_manifest_uri": resource_profile.get("forcing_package_manifest_uri"),
        "forcing_manifest_checksum": resource_profile.get("forcing_manifest_checksum"),
        "forcing_uri": resource_profile.get("forcing_uri"),
        "forcing_file_uris": dict(resource_profile.get("forcing_file_uris") or {}),
        "hydro_run_id": candidate.run_id,
        "published_manifest_id": _candidate_published_manifest_id(candidate),
        "forecast_horizon_hours": candidate.horizon.get("forecast_horizon_hours")
        or candidate.horizon.get("max_lead_hours"),
        "max_lead_hours": candidate.horizon.get("max_lead_hours"),
        "station_count": _candidate_station_count(candidate),
        "station_ids": _candidate_station_ids(candidate),
        "frequency_curves_available": _nested_bool(
            candidate.frequency_capabilities,
            "curves_available",
            fallback=_nested_bool(candidate.frequency_capabilities, "return_periods"),
        ),
        "warning_thresholds_available": _nested_bool(candidate.frequency_capabilities, "warning_thresholds_available"),
        "optional_weather_available": _nested_bool(candidate.display_capabilities, "optional_weather_available"),
        "output_river": _candidate_output_river_manifest(candidate),
        "output_key": _candidate_output_key(candidate),
        "output_uri": output_uri,
    }
    if orchestration_run_id not in (None, ""):
        manifest["orchestration_run_id"] = orchestration_run_id
    pipeline_job_id = _candidate_contract_pipeline_job_id(candidate)
    if pipeline_job_id not in (None, ""):
        manifest["pipeline_job_id"] = pipeline_job_id
    _apply_candidate_warm_start_fields(manifest, candidate)
    if candidate.state_evidence:
        state_evidence = _evidence_safe(candidate.state_evidence)
        manifest["state_evidence"] = state_evidence
        restart_stage = state_evidence.get("restart_stage") if isinstance(state_evidence, Mapping) else None
        # Defense in depth: fresh full-chain ingestion never carries a basin
        # restart_stage even if a residual marker survived upstream merges.
        if restart_stage and not _candidate_is_fresh_full_chain(candidate):
            manifest["restart_stage"] = restart_stage
        if state_evidence.get("durable_shud_output_reused") is True:
            manifest["durable_shud_output_reused"] = True
            manifest["native_shud_resubmitted"] = False
        retry_attempt = _candidate_manual_retry_attempt(candidate)
        if retry_attempt is not None:
            manifest["manual_retry_attempt"] = retry_attempt
            manifest["retry_attempt"] = retry_attempt
    forcing_metadata = resource_profile.get("forcing_station_metadata")
    if isinstance(forcing_metadata, Mapping):
        manifest["forcing_station_metadata"] = dict(forcing_metadata)
    slurm_env = resource_profile.get("slurm_env")
    if isinstance(slurm_env, Mapping):
        manifest["slurm_env"] = {str(key): str(value) for key, value in slurm_env.items()}
    project_identity = _resource_profile_project_identity(resource_profile)
    if project_identity is not None:
        manifest["project_name"] = project_identity["project_name"]
        manifest["shud_input_name"] = project_identity["shud_input_name"]
    package_checksum = resource_profile.get("package_checksum")
    if package_checksum not in (None, ""):
        manifest["package_checksum"] = str(package_checksum)
        manifest.setdefault("model_package_checksum", str(package_checksum))
    source_inventory_checksum = resource_profile.get("source_inventory_checksum")
    if source_inventory_checksum not in (None, ""):
        manifest["source_inventory_checksum"] = str(source_inventory_checksum)
    return manifest


def _candidate_manual_retry_attempt(candidate: SchedulerCandidate) -> int | None:
    state_evidence = candidate.state_evidence
    if not isinstance(state_evidence, Mapping):
        return None
    manual_retry = state_evidence.get("manual_retry")
    if not isinstance(manual_retry, Mapping):
        return None
    if manual_retry.get("allowed") is False:
        return None
    for key in ("new_attempt", "attempt", "retry_count"):
        try:
            value = int(manual_retry.get(key))
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _apply_candidate_warm_start_fields(manifest: dict[str, Any], candidate: SchedulerCandidate) -> None:
    """Carry warm-start init-state fields onto the scheduler basin record.

    When the candidate's ``state_evidence`` already names a selected warm-start state
    (``candidate_state``), copy ``init_state_*`` + lineage onto the basin manifest so
    the scheduler basin record agrees with the cycle-stage and forecast runtime
    manifests (M24 §2 Lane 2). When absent, the orchestrator's ``_apply_cohort_warm_start``
    is authoritative and fills these fields; this is a no-op passthrough.
    """

    evidence = candidate.state_evidence
    if not isinstance(evidence, Mapping):
        return
    selected = evidence.get("candidate_state")
    if not isinstance(selected, Mapping):
        return
    uri = selected.get("init_state_uri") or selected.get("state_uri") or selected.get("ic_file_uri")
    if uri in (None, ""):
        return
    manifest["init_state_uri"] = str(uri)
    for key in ("init_state_id", "init_state_checksum", "init_state_valid_time", "init_state_quality"):
        value = selected.get(key) or selected.get(key.removeprefix("init_state_"))
        if value not in (None, ""):
            manifest[key] = value
    lineage = selected.get("init_state_lineage") or selected.get("lineage")
    if isinstance(lineage, Mapping):
        manifest["init_state_lineage"] = dict(lineage)


def _resource_profile_project_identity(resource_profile: Mapping[str, Any]) -> dict[str, str] | None:
    project_name = resource_profile.get("project_name")
    shud_input_name = resource_profile.get("shud_input_name")
    project = str(project_name) if project_name not in (None, "") else None
    shud_input = str(shud_input_name) if shud_input_name not in (None, "") else None
    if project is None and shud_input is None:
        return None
    return {"project_name": project or shud_input or "", "shud_input_name": shud_input or project or ""}


def _candidate_execution_attempted(outcome: Mapping[str, Any] | None, submitted: bool) -> bool:
    if submitted and outcome is None:
        return True
    if not outcome:
        return False
    return any(
        outcome.get(field) not in (None, "")
        for field in ("slurm_job_id", "exit_code", "log_uri", "accounting", "task_id", "original_task_id")
    )


def _pipeline_result_slurm_submit_called(result: PipelineResult) -> bool:
    for stage in result.stages:
        if _nonempty_evidence_value(getattr(stage, "slurm_job_id", None)):
            return True
        task_results = getattr(stage, "task_results", ()) or ()
        for task in task_results:
            if isinstance(task, Mapping) and _nonempty_evidence_value(task.get("slurm_job_id")):
                return True
    return any(
        _nonempty_evidence_value(outcome.get("slurm_job_id"))
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if isinstance(outcome, Mapping)
    )


def _pipeline_result_pipeline_status_write(result: PipelineResult) -> bool | str:
    return _pipeline_result_pipeline_write_value(
        result,
        write_field="pipeline_status_write",
        absent_field="pipeline_status_writes_proven_absent",
    )


def _pipeline_result_pipeline_event_write(result: PipelineResult) -> bool | str:
    return _pipeline_result_pipeline_write_value(
        result,
        write_field="pipeline_event_write",
        absent_field="pipeline_event_writes_proven_absent",
    )


def _pipeline_result_pipeline_write_value(
    result: PipelineResult,
    *,
    write_field: str,
    absent_field: str,
) -> bool | str:
    outcome_values = [
        _candidate_pipeline_write_value(outcome, write_field, fallback=None)
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if isinstance(outcome, Mapping)
    ]
    if any(value is True for value in outcome_values):
        return True
    if any(value == UNKNOWN_AFTER_ATTEMPT for value in outcome_values):
        return UNKNOWN_AFTER_ATTEMPT
    if _pipeline_result_has_pipeline_job_evidence(result):
        return True
    if outcome_values and all(value is False for value in outcome_values):
        return False
    if _pipeline_result_write_absence_proven(result, absent_field):
        return False
    return UNKNOWN_AFTER_ATTEMPT


def _pipeline_result_has_pipeline_job_evidence(result: PipelineResult) -> bool:
    for stage in result.stages:
        if _nonempty_evidence_value(getattr(stage, "pipeline_job_id", None)):
            return True
        task_results = getattr(stage, "task_results", ()) or ()
        for task in task_results:
            if isinstance(task, Mapping) and _nonempty_evidence_value(task.get("pipeline_job_id")):
                return True
    return any(
        _nonempty_evidence_value(outcome.get("pipeline_job_id"))
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if isinstance(outcome, Mapping)
    )


def _pipeline_result_write_absence_proven(result: PipelineResult, absent_field: str) -> bool:
    outcomes = [outcome for outcome in getattr(result, "candidate_outcomes", ()) or () if isinstance(outcome, Mapping)]
    if outcomes and all(outcome.get(absent_field) is True for outcome in outcomes):
        return True
    return False


def _candidate_slurm_submit_called(outcome: Mapping[str, Any] | None, fallback: bool) -> bool:
    if outcome and _nonempty_evidence_value(outcome.get("slurm_job_id")):
        return True
    return fallback


def _candidate_pipeline_write_value(
    outcome: Mapping[str, Any] | None,
    write_field: str,
    *,
    fallback: bool | str | None,
) -> bool | str | None:
    if outcome:
        value = outcome.get(write_field)
        if value == UNKNOWN_AFTER_ATTEMPT:
            return UNKNOWN_AFTER_ATTEMPT
        coerced = _nested_bool(outcome, write_field)
        if coerced is True:
            return True
        absent_field = f"{write_field}s_proven_absent"
        if outcome.get(absent_field) is True:
            return False
    return fallback


def _execution_mutation_value(*values: bool | str | None) -> bool | str:
    if any(value == UNKNOWN_AFTER_ATTEMPT for value in values):
        return UNKNOWN_AFTER_ATTEMPT
    return any(value is True for value in values)


def _nonempty_evidence_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _candidate_identity_evidence(candidate: SchedulerCandidate, *, output_uri: str | None = None) -> dict[str, Any]:
    contract_identity = _candidate_production_identity(candidate)
    evidence = {
        "production_identity_contract": production_identity_contract_evidence(contract_identity),
        "candidate_id": candidate.candidate_id,
        "source_id": candidate.source_id,
        "source": candidate.source_id,
        "cycle_id": candidate.cycle_id,
        "cycle_time_utc": _format_utc(candidate.cycle_time_utc),
        "cycle_time": _format_utc(candidate.cycle_time_utc),
        "model_id": candidate.model_id,
        "scenario_id": candidate.scenario_id,
        "run_id": candidate.run_id,
        "canonical_product_id": contract_identity["canonical_product_id"],
        "forcing_version_id": candidate.forcing_version_id,
        "hydro_run_id": contract_identity["hydro_run_id"],
        "published_manifest_id": contract_identity["published_manifest_id"],
        "model_package_uri": _redact_secret_manifest_for_evidence(candidate.model_package_uri, "model_package_uri"),
        "model_package_manifest_uri": _redact_secret_manifest_for_evidence(
            _model_package_manifest_uri(candidate),
            "model_package_manifest_uri",
        ),
        "basin_version_id": candidate.basin_version_id,
        "river_network_version_id": candidate.river_network_version_id,
        "segment_count": candidate.segment_count,
        "output_segment_count": candidate.output_segment_count,
        "output_key": _candidate_output_key(candidate),
    }
    if contract_identity.get("pipeline_job_id") not in (None, ""):
        evidence["pipeline_job_id"] = contract_identity["pipeline_job_id"]
    resolved_output_uri = output_uri or _candidate_output_uri(candidate)
    if resolved_output_uri is not None:
        evidence["output_uri"] = _redact_secret_manifest_for_evidence(resolved_output_uri, "output_uri")
    if candidate.state_evidence:
        evidence["state_evidence"] = _evidence_safe(candidate.state_evidence)
    return evidence


def _candidate_preflight_blocked_evidence(
    candidate: SchedulerCandidate,
    *,
    config: ProductionSchedulerConfig | None = None,
) -> dict[str, Any]:
    if config is not None and config.slurm_execution_enabled:
        preflight = _slurm_preflight(config)
        return _candidate_slurm_preflight_blocked_evidence(candidate, preflight)
    return {
        **_candidate_model_run_review_evidence(
            candidate,
            output_uri=None,
            outcome=None,
            status="preflight_blocked",
            stage_statuses=[],
        ),
        **_candidate_identity_evidence(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "unsupported_without_safe_preflight",
        "error_code": "PRODUCTION_PREFLIGHT_UNSUPPORTED",
        "error_message": (
            "Default non-dry-run production scheduling is blocked until the Slurm/database preflight "
            "from issue #194 is available or a deterministic orchestrator_factory is injected."
        ),
        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
        "qhh_script_invoked": False,
        "residual_blockers": [
            {
                "code": "PRODUCTION_PREFLIGHT_UNSUPPORTED",
                "state": "blocked",
                "quality_flag": "preflight_required",
                "residual_risk": "No scheduler mutation was attempted.",
            }
        ],
    }


def _candidate_slurm_preflight_blocked_evidence(
    candidate: SchedulerCandidate,
    preflight: Mapping[str, Any],
) -> dict[str, Any]:
    blockers = list(preflight.get("blockers") or [])
    primary = (
        blockers[0]
        if blockers
        else {
            "code": "SLURM_PREFLIGHT_BLOCKED",
            "message": "Slurm preflight blocked submission.",
        }
    )
    return {
        **_candidate_model_run_review_evidence(
            candidate,
            output_uri=None,
            outcome=None,
            status="preflight_blocked",
            stage_statuses=[],
        ),
        **_candidate_identity_evidence(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "slurm_preflight",
        "slurm_preflight": redact_payload(preflight),
        "error_code": str(primary.get("code") or "SLURM_PREFLIGHT_BLOCKED"),
        "error_message": str(primary.get("message") or "Slurm preflight blocked submission."),
        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
        "qhh_script_invoked": False,
        "residual_blockers": [
            {
                "code": str(blocker.get("code") or "SLURM_PREFLIGHT_BLOCKED"),
                "field": blocker.get("field"),
                "state": "blocked",
                "quality_flag": "slurm_preflight_blocked",
                "residual_risk": blocker.get("message"),
            }
            for blocker in blockers
        ],
    }


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


def _candidate_secret_manifest_blocked_evidence(
    candidate: SchedulerCandidate,
    *,
    findings: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    return {
        **_candidate_model_run_review_evidence(
            candidate,
            output_uri=None,
            outcome=None,
            status="preflight_blocked",
            stage_statuses=[],
        ),
        **_candidate_identity_evidence(candidate),
        "status": "preflight_blocked",
        "submitted": False,
        "mutation_occurred": False,
        "execution_mode": "slurm_preflight",
        "error_code": "SLURM_PREFLIGHT_SECRET_MANIFEST_REJECTED",
        "error_message": "Slurm submission manifests reject secret-bearing fields and URL values.",
        "standard_chain_shape": [stage.stage for stage in ForecastOrchestrator.stages],
        "qhh_script_invoked": False,
        "residual_blockers": [
            {
                "code": "SLURM_PREFLIGHT_SECRET_MANIFEST_REJECTED",
                "field": finding.get("field"),
                "state": "blocked",
                "quality_flag": "slurm_preflight_blocked",
                "residual_risk": "Secret-bearing manifest field or URL value was rejected before submission.",
            }
            for finding in findings
        ],
    }


def _slurm_resource_profile_blockers(resource_profile: Mapping[str, Any]) -> list[dict[str, Any]]:
    collision_fields = sorted(SLURM_RESOURCE_PROFILE_TEMPLATE_IDENTITY_FIELDS.intersection(resource_profile))
    if collision_fields:
        return [
            {
                "code": "SLURM_PREFLIGHT_RESOURCE_PROFILE_INVALID",
                "field": f"resource_profile.{field}",
                "message": "Slurm resource profile cannot override manifest or template identity fields.",
                "reason": "manifest_identity_collision",
            }
            for field in collision_fields
        ]
    directive_fields = {
        key: resource_profile[key] for key in SLURM_RESOURCE_PROFILE_DIRECTIVE_FIELDS if key in resource_profile
    }
    if not directive_fields:
        return []
    try:
        validate_resource_profile(directive_fields, require_required=False)
    except ResourceProfileValidationError as exc:
        return [
            {
                "code": "SLURM_PREFLIGHT_RESOURCE_PROFILE_INVALID",
                "field": exc.details.get("field"),
                "message": "Slurm resource profile contains invalid directive values.",
                "reason": exc.details.get("reason") or exc.details.get("type"),
            }
        ]
    except ConfigurationError as exc:
        return [
            {
                "code": "SLURM_PREFLIGHT_RESOURCE_PROFILE_INVALID",
                "field": (exc.details or {}).get("field"),
                "message": "Slurm resource profile contains invalid directive values.",
            }
        ]
    return []


def _resource_profile_evidence(resource_profile: Mapping[str, Any]) -> dict[str, Any]:
    redacted = _redact_secret_manifest_for_evidence(dict(resource_profile), "resource_profile")
    redacted = _redact_digest_keys_for_evidence(redacted)
    if not isinstance(redacted, Mapping):
        return {}
    evidence = dict(redacted)
    invalid_fields = {
        str(blocker.get("field", "")).removeprefix("resource_profile.")
        for blocker in _slurm_resource_profile_blockers(resource_profile)
        if blocker.get("field")
    }
    for field_name in invalid_fields:
        if field_name in evidence:
            evidence[field_name] = "[unsafe]"
    return evidence


def _redact_digest_keys_for_evidence(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            if _is_digest_evidence_key(key_text):
                redacted[key_text] = "[redacted]"
            else:
                redacted[key_text] = _redact_digest_keys_for_evidence(nested)
        return redacted
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_redact_digest_keys_for_evidence(item) for item in value]
    return value


def _is_digest_evidence_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if any(token in normalized for token in ("checksum", "digest", "hash")):
        return True
    return any(token == normalized or normalized.endswith(f"_{token}") for token in ("sha", "sha1", "sha256", "sha512"))


def _candidate_execution_evidence(
    result: PipelineResult,
    candidates: Sequence[SchedulerCandidate],
    *,
    output_uris: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    stage_names = [stage.stage for stage in result.stages]
    stage_statuses = [_stage_run_evidence(stage) for stage in result.stages]
    slurm_submit_called = _pipeline_result_slurm_submit_called(result)
    pipeline_status_write = _pipeline_result_pipeline_status_write(result)
    pipeline_event_write = _pipeline_result_pipeline_event_write(result)
    outcomes_by_candidate = {
        str(outcome.get("candidate_id")): dict(outcome)
        for outcome in getattr(result, "candidate_outcomes", ()) or ()
        if outcome.get("candidate_id")
    }
    return [
        _candidate_execution_evidence_item(
            result,
            candidate,
            output_uri=(output_uris or {}).get(candidate.candidate_id),
            outcome=outcomes_by_candidate.get(candidate.candidate_id),
            slurm_submit_called=slurm_submit_called,
            pipeline_status_write=pipeline_status_write,
            pipeline_event_write=pipeline_event_write,
            stage_names=stage_names,
            stage_statuses=stage_statuses,
        )
        for candidate in candidates
    ]


def _candidate_forcing_ready_evidence(candidate: SchedulerCandidate, result: Any) -> dict[str, Any]:
    status = str(getattr(result, "status", "forcing_ready") or "forcing_ready")
    met_write = status != "already_done"
    payload = {
        **_candidate_identity_evidence(candidate, output_uri=None),
        "stage": "forcing",
        "production_stage": production_stage_for("forcing"),
        "status": status,
        "production_status": production_status_for(status),
        "submitted": False,
        "slurm_submit_called": False,
        "execution_attempted": True,
        "forcing_producer_called": True,
        "mutation_occurred": met_write,
        "met_result_table_write": met_write,
        "hydro_result_table_write": False,
        "pipeline_status_writes_proven_absent": True,
        "pipeline_event_writes_proven_absent": True,
        "qhh_script_invoked": False,
        "rshud_runtime_called": False,
        "forcing": {
            "forcing_version_id": getattr(result, "forcing_version_id", candidate.forcing_version_id),
            "forcing_package_uri": getattr(result, "forcing_package_uri", None),
            "package_uri": getattr(result, "forcing_package_uri", None),
            "checksum": getattr(result, "checksum", None),
            "manifest_checksum": getattr(result, "checksum", None),
            "station_count": getattr(result, "station_count", None),
            "timestep_count": getattr(result, "timestep_count", None),
            "variable_count": getattr(result, "variable_count", None),
            "time_range": dict(getattr(result, "time_range", {}) or {}),
            "units": dict(getattr(result, "units", {}) or {}),
            "file_uris": dict(getattr(result, "file_uris", {}) or {}),
        },
    }
    return _evidence_safe(payload)


def _candidate_forcing_blocked_evidence(candidate: SchedulerCandidate, error: Exception) -> dict[str, Any]:
    error_code = str(getattr(error, "error_code", "FORCING_PRODUCTION_BLOCKED"))
    payload = {
        **_candidate_identity_evidence(candidate, output_uri=None),
        "stage": "forcing",
        "production_stage": production_stage_for("forcing"),
        "status": "blocked",
        "production_status": production_status_for("blocked"),
        "submitted": False,
        "slurm_submit_called": False,
        "execution_attempted": True,
        "forcing_producer_called": True,
        "mutation_outcome": UNKNOWN_AFTER_ATTEMPT,
        "mutation_occurred": UNKNOWN_AFTER_ATTEMPT,
        "met_result_table_write": UNKNOWN_AFTER_ATTEMPT,
        "hydro_result_table_write": False,
        "pipeline_status_writes_proven_absent": True,
        "pipeline_event_writes_proven_absent": True,
        "qhh_script_invoked": False,
        "rshud_runtime_called": False,
        "error_code": error_code,
        "error_message": _evidence_safe(getattr(error, "message", str(error))),
        "residual_blockers": [
            {
                "code": error_code,
                "stage": "forcing",
                "state": "blocked",
                "quality_flag": "forcing_production_blocked",
                "residual_risk": "Station forcing production did not complete; SHUD submission is blocked.",
            }
        ],
    }
    return _evidence_safe(payload)


def _candidate_execution_evidence_item(
    result: PipelineResult,
    candidate: SchedulerCandidate,
    *,
    output_uri: str | None,
    outcome: Mapping[str, Any] | None,
    slurm_submit_called: bool,
    pipeline_status_write: bool | str,
    pipeline_event_write: bool | str,
    stage_names: Sequence[str],
    stage_statuses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    if outcome is None:
        status = result.status
        candidate_submitted = slurm_submit_called
        candidate_outcome: dict[str, Any] | None = None
        execution_attempted = True
    else:
        outcome_status = str(outcome.get("status") or "")
        status = _candidate_status_from_outcome(result.status, outcome_status)
        execution_attempted = True
        candidate_slurm_submit_called = _candidate_slurm_submit_called(outcome, slurm_submit_called)
        candidate_submitted = candidate_slurm_submit_called and (outcome_status == "active" or execution_attempted)
        candidate_outcome = dict(outcome)
    candidate_pipeline_status_write = _candidate_pipeline_write_value(
        outcome,
        "pipeline_status_write",
        fallback=pipeline_status_write,
    )
    candidate_pipeline_event_write = _candidate_pipeline_write_value(
        outcome,
        "pipeline_event_write",
        fallback=pipeline_event_write,
    )
    mutation_occurred = _execution_mutation_value(
        candidate_submitted,
        candidate_pipeline_status_write,
        candidate_pipeline_event_write,
    )
    review_evidence = _candidate_model_run_review_evidence(
        candidate,
        output_uri=output_uri,
        outcome=outcome,
        status=status,
        stage_statuses=stage_statuses,
    )
    item = {
        **review_evidence,
        "status": status,
        "submitted": candidate_submitted,
        "slurm_submit_called": candidate_submitted,
        "execution_attempted": execution_attempted,
        "final_candidate_success": (
            status == result.status and not _is_non_submitted_terminal_or_unavailable_status(status)
        ),
        "mutation_occurred": mutation_occurred,
        "pipeline_run_id": result.run_id,
        "standard_chain_shape": stage_names,
        "qhh_script_invoked": False,
    }
    if candidate_pipeline_status_write is True:
        item["pipeline_status_write"] = True
        item["pipeline_status_writes_proven_absent"] = False
    elif candidate_pipeline_status_write == UNKNOWN_AFTER_ATTEMPT:
        item["pipeline_status_write"] = UNKNOWN_AFTER_ATTEMPT
        item["pipeline_status_writes_proven_absent"] = False
    else:
        item["pipeline_status_writes_proven_absent"] = True
    if candidate_pipeline_event_write is True:
        item["pipeline_event_write"] = True
        item["pipeline_event_writes_proven_absent"] = False
    elif candidate_pipeline_event_write == UNKNOWN_AFTER_ATTEMPT:
        item["pipeline_event_write"] = UNKNOWN_AFTER_ATTEMPT
        item["pipeline_event_writes_proven_absent"] = False
    else:
        item["pipeline_event_writes_proven_absent"] = True
    if mutation_occurred == UNKNOWN_AFTER_ATTEMPT:
        item["mutation_outcome"] = UNKNOWN_AFTER_ATTEMPT
    if candidate_outcome is not None:
        candidate_outcome = _evidence_safe(candidate_outcome)
        item["candidate_outcome"] = candidate_outcome
        if _is_partial_candidate_evidence(item):
            item["error_code"] = str(candidate_outcome.get("reason") or f"CANDIDATE_{status}").upper()
            item["error_message"] = f"Candidate {candidate.candidate_id} was {status} in the partial multi-basin cycle."
            if not any(blocker.get("code") == item["error_code"] for blocker in item["residual_blockers"]):
                item["residual_blockers"].append(
                    {
                        "code": item["error_code"],
                        "stage": candidate_outcome.get("stage") or candidate_outcome.get("failed_stage"),
                        "state": "blocked",
                        "quality_flag": "partial_candidate",
                        "residual_risk": item["error_message"],
                    }
                )
    return item


def _candidate_status_from_outcome(result_status: str, outcome_status: str) -> str:
    if outcome_status == "active":
        return result_status
    if _is_non_submitted_terminal_or_unavailable_status(outcome_status):
        return outcome_status
    return "unavailable"


def _candidate_model_run_review_evidence(
    candidate: SchedulerCandidate,
    *,
    output_uri: str | None,
    outcome: Mapping[str, Any] | None,
    status: str,
    stage_statuses: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    stage_status_payload = _candidate_stage_evidence(candidate, stage_statuses, outcome=outcome)
    quality_states = _candidate_quality_states(candidate, outcome=outcome, status=status)
    artifact_refs = _candidate_artifact_refs(candidate, output_uri=output_uri)
    return {
        "schema_version": MODEL_RUN_EVIDENCE_SCHEMA_VERSION,
        "review_contract": {
            "contract_id": SCHEDULER_EVIDENCE_CONTRACT_ID,
            "github_issue": SCHEDULER_EVIDENCE_GITHUB_ISSUE,
            "openspec_change": SCHEDULER_EVIDENCE_OPEN_SPEC_CHANGE,
            "scope": "model_run_evidence",
        },
        **_candidate_identity_evidence(candidate, output_uri=output_uri),
        "stage_statuses": stage_status_payload,
        "stage_evidence": stage_status_payload,
        "artifact_refs": artifact_refs,
        "artifact_locations": dict(artifact_refs),
        "resource_profile": _resource_profile_evidence(candidate.resource_profile),
        "resource_summary": _candidate_resource_summary(
            candidate,
            stage_statuses=stage_status_payload,
            outcome=outcome,
        ),
        "forcing": _candidate_forcing_evidence(candidate),
        "outputs": _candidate_output_evidence(candidate, output_uri=output_uri, outcome=outcome),
        "display": _candidate_display_evidence(candidate),
        "quality_states": quality_states,
        "residual_blockers": _candidate_residual_blockers(
            candidate,
            outcome=outcome,
            status=status,
            quality_states=quality_states,
        ),
    }


def _candidate_stage_evidence(
    candidate: SchedulerCandidate,
    stage_statuses: Sequence[Mapping[str, Any]],
    *,
    outcome: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    return [
        _candidate_stage_evidence_item(candidate, _evidence_safe(dict(stage)), outcome=outcome)
        for stage in stage_statuses
    ]


def _candidate_stage_evidence_item(
    candidate: SchedulerCandidate,
    stage: Mapping[str, Any],
    *,
    outcome: Mapping[str, Any] | None,
) -> dict[str, Any]:
    stage_payload = dict(stage)
    stage_payload["production_stage"] = production_stage_for(
        stage_payload.get("stage") or stage_payload.get("job_type")
    )
    stage_payload["production_status"] = production_status_for(stage_payload.get("status"))
    task_results = _stage_task_results(stage_payload)
    total_count = len(task_results)
    status_counts = Counter(str(task.get("status") or task.get("state") or "unknown") for task in task_results)
    matched_tasks = [task for task in task_results if _task_result_matches_candidate(task, candidate, outcome=outcome)]
    exact_match_available = _task_candidate_matching_available(task_results, outcome=outcome)
    if exact_match_available:
        selected_tasks = matched_tasks[:MAX_MODEL_RUN_STAGE_TASK_ROWS]
    else:
        selected_tasks = task_results[:MAX_MODEL_RUN_STAGE_TASK_ROWS]
    selected_count = len(selected_tasks)
    stage_payload["task_results"] = [_evidence_safe(dict(task)) for task in selected_tasks]
    stage_payload["task_results_summary"] = _evidence_safe(
        {
            "total_count": total_count,
            "included_count": selected_count,
            "omitted_count": max(total_count - selected_count, 0),
            "matched_count": len(matched_tasks),
            "matching": "candidate_identity" if exact_match_available else "bounded_sample",
            "limit": MAX_MODEL_RUN_STAGE_TASK_ROWS,
            "status_counts": dict(sorted(status_counts.items())),
        }
    )
    return _evidence_safe(stage_payload)


def _stage_run_evidence(stage: Any) -> dict[str, Any]:
    task_results = [
        _task_result_evidence(task)
        for task in tuple(getattr(stage, "task_results", ()) or ())
        if isinstance(task, Mapping)
    ]
    payload = {
        "stage": getattr(stage, "stage", None),
        "production_stage": production_stage_for(getattr(stage, "stage", None) or getattr(stage, "job_type", None)),
        "job_type": getattr(stage, "job_type", None),
        "pipeline_job_id": getattr(stage, "pipeline_job_id", None),
        "slurm_job_id": getattr(stage, "slurm_job_id", None),
        "status": getattr(stage, "status", None),
        "production_status": production_status_for(getattr(stage, "status", None)),
        "exit_code": getattr(stage, "exit_code", None),
        "error_code": getattr(stage, "error_code", None),
        "error_message": getattr(stage, "error_message", None),
        "log_uri": getattr(stage, "log_uri", None),
        "accounting": getattr(stage, "accounting", {}) or {},
        "resource_metrics": _resource_metrics_from_mapping(getattr(stage, "accounting", {}) or {}),
        "task_results": task_results,
    }
    if not payload["accounting"]:
        payload["accounting_gap"] = {
            "available": False,
            "reason": "accounting_unavailable",
            "fabricated_metrics": False,
        }
    return _evidence_safe(payload)


def _stage_task_results(stage: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    task_results = stage.get("task_results") or []
    if not isinstance(task_results, Sequence) or isinstance(task_results, str | bytes | bytearray):
        return []
    return [task for task in task_results if isinstance(task, Mapping)]


def _task_result_matches_candidate(
    task: Mapping[str, Any],
    candidate: SchedulerCandidate,
    *,
    outcome: Mapping[str, Any] | None,
) -> bool:
    identity_fields = {
        "candidate_id": candidate.candidate_id,
        "run_id": candidate.run_id,
        "forcing_version_id": candidate.forcing_version_id,
        "model_id": candidate.model_id,
    }
    for field_name, expected in identity_fields.items():
        if _normalized_identity(task.get(field_name)) == _normalized_identity(expected):
            return True
    identity = task.get("identity")
    if isinstance(identity, Mapping):
        for field_name, expected in identity_fields.items():
            if _normalized_identity(identity.get(field_name)) == _normalized_identity(expected):
                return True
    if outcome is None:
        return False
    for field_name in TASK_RESULT_CANDIDATE_IDENTITY_FIELDS:
        task_value = _normalized_identity(task.get(field_name))
        outcome_value = _normalized_identity(outcome.get(field_name))
        if task_value is not None and task_value == outcome_value:
            return True
    outcome_task_ids = {
        _normalized_identity(outcome.get(field_name)) for field_name in TASK_RESULT_INDEX_IDENTITY_FIELDS
    }
    outcome_task_ids.discard(None)
    task_ids = {_normalized_identity(task.get(field_name)) for field_name in TASK_RESULT_INDEX_IDENTITY_FIELDS}
    task_ids.discard(None)
    return bool(task_ids.intersection(outcome_task_ids))


def _task_candidate_matching_available(
    tasks: Sequence[Mapping[str, Any]],
    *,
    outcome: Mapping[str, Any] | None,
) -> bool:
    for task in tasks:
        if any(task.get(field_name) not in (None, "") for field_name in TASK_RESULT_CANDIDATE_IDENTITY_FIELDS):
            return True
        identity = task.get("identity")
        if isinstance(identity, Mapping) and any(
            identity.get(field_name) not in (None, "") for field_name in TASK_RESULT_CANDIDATE_IDENTITY_FIELDS
        ):
            return True
    if outcome is None:
        return False
    outcome_has_task_identity = any(
        outcome.get(field_name) not in (None, "") for field_name in TASK_RESULT_INDEX_IDENTITY_FIELDS
    )
    if not outcome_has_task_identity:
        return False
    return any(
        any(task.get(field_name) not in (None, "") for field_name in TASK_RESULT_INDEX_IDENTITY_FIELDS)
        for task in tasks
    )


def _normalized_identity(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _task_result_evidence(task: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(task)
    payload["accounting"] = dict(_mapping_value(payload.get("accounting")))
    metrics = _resource_metrics_from_mapping(payload.get("resource_metrics") or payload["accounting"])
    if metrics:
        payload["resource_metrics"] = metrics
    elif "resource_metrics" not in payload:
        payload["resource_metrics"] = {}
    return _evidence_safe(payload)


def _resource_metrics_from_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    aliases = {
        "elapsed": ("elapsed", "elapsed_time"),
        "max_rss": ("max_rss", "MaxRSS", "maxrss"),
        "ave_rss": ("ave_rss", "AveRSS", "averss"),
        "alloc_tres": ("alloc_tres", "AllocTRES", "tres"),
        "max_disk_read": ("max_disk_read", "MaxDiskRead"),
        "max_disk_write": ("max_disk_write", "MaxDiskWrite"),
    }
    metrics: dict[str, Any] = {}
    for normalized, keys in aliases.items():
        for key in keys:
            if key in value and value[key] not in (None, ""):
                metrics[normalized] = value[key]
                break
    return _evidence_safe(metrics)


def _candidate_artifact_refs(candidate: SchedulerCandidate, *, output_uri: str | None) -> dict[str, Any]:
    refs = {
        "model_package_uri": _redact_secret_manifest_for_evidence(candidate.model_package_uri, "model_package_uri"),
        "model_package_manifest_uri": _redact_secret_manifest_for_evidence(
            _model_package_manifest_uri(candidate),
            "model_package_manifest_uri",
        ),
        "output_key": _candidate_output_key(candidate),
    }
    resolved_output_uri = output_uri or _candidate_output_uri(candidate)
    if resolved_output_uri is not None:
        refs["output_uri"] = _redact_secret_manifest_for_evidence(resolved_output_uri, "output_uri")
    manifest_uri = candidate.resource_profile.get("manifest_uri")
    if manifest_uri not in (None, ""):
        refs["resource_manifest_uri"] = _redact_secret_manifest_for_evidence(
            str(manifest_uri),
            "resource_manifest_uri",
        )
    return _evidence_safe(refs)


def _candidate_resource_summary(
    candidate: SchedulerCandidate,
    *,
    stage_statuses: Sequence[Mapping[str, Any]],
    outcome: Mapping[str, Any] | None,
) -> dict[str, Any]:
    resource_profile = _resource_profile_evidence(candidate.resource_profile)
    stage_accounting = [
        {
            "stage": stage.get("stage"),
            "slurm_job_id": stage.get("slurm_job_id"),
            "accounting": stage.get("accounting") or {},
            "resource_metrics": stage.get("resource_metrics") or {},
            "accounting_gap": stage.get("accounting_gap"),
        }
        for stage in stage_statuses
    ]
    task_accounting: list[dict[str, Any]] = []
    for stage in stage_statuses:
        for task in stage.get("task_results") or []:
            if not isinstance(task, Mapping):
                continue
            task_accounting.append(
                {
                    "stage": stage.get("stage"),
                    "task_id": task.get("task_id"),
                    "array_task_id": task.get("array_task_id"),
                    "slurm_job_id": task.get("slurm_job_id"),
                    "status": task.get("status"),
                    "accounting": task.get("accounting") or {},
                    "resource_metrics": task.get("resource_metrics") or {},
                }
            )
    payload = {
        "resource_profile": resource_profile,
        "requested": {
            "memory_gb": resource_profile.get("memory_gb"),
            "cpu": resource_profile.get("cpu"),
            "cpus_per_task": resource_profile.get("cpus_per_task"),
            "walltime": resource_profile.get("walltime"),
            "max_concurrent": resource_profile.get("max_concurrent"),
            "shud_threads": resource_profile.get("shud_threads"),
        },
        "stage_accounting": stage_accounting,
        "task_accounting": task_accounting,
        "candidate_accounting": dict(_mapping_value(outcome.get("accounting") if outcome is not None else None)),
        "candidate_resource_metrics": _resource_metrics_from_mapping(
            (outcome.get("resource_metrics") or outcome.get("accounting")) if outcome is not None else {}
        ),
    }
    return _evidence_safe(payload)


def _candidate_forcing_evidence(candidate: SchedulerCandidate) -> dict[str, Any]:
    metadata = candidate.resource_profile.get("forcing_station_metadata")
    station_count = _candidate_station_count(candidate)
    station_ids = _candidate_station_ids(candidate)
    payload = {
        "station_count": station_count,
        "station_ids": station_ids,
        "state": "ready" if station_count and station_count > 0 else "unavailable",
        "quality_flag": "ok" if station_count and station_count > 0 else "station_forcing_unavailable",
    }
    if isinstance(metadata, Mapping):
        payload["station_metadata"] = dict(metadata)
        if metadata.get("quality_flag") not in (None, ""):
            payload["quality_flag"] = metadata.get("quality_flag")
    return _evidence_safe(payload)


def _candidate_output_evidence(
    candidate: SchedulerCandidate,
    *,
    output_uri: str | None,
    outcome: Mapping[str, Any] | None,
) -> dict[str, Any]:
    resolved_output_uri = output_uri or _candidate_output_uri(candidate)
    parsed_row_count = _first_present_int(
        outcome,
        candidate.resource_profile,
        "parsed_row_count",
        "canonical_product_count",
        "output_row_count",
    )
    output_segment_count = _first_present_int(
        outcome,
        candidate.resource_profile,
        "output_segment_count",
        "shud_output_segment_count",
        "shud_output_river_count",
    )
    if output_segment_count is None:
        output_segment_count = candidate.output_segment_count
    payload = {
        "output_uri": _redact_secret_manifest_for_evidence(
            resolved_output_uri,
            "output_uri",
        )
        if resolved_output_uri
        else None,
        "output_key": _candidate_output_key(candidate),
        "shud_output_uri": _redact_secret_manifest_for_evidence(
            _first_present_value(outcome, candidate.resource_profile, "shud_output_uri", "output_uri"),
            "shud_output_uri",
        ),
        "parsed_row_count": parsed_row_count,
        "segment_count": output_segment_count,
        "output_segment_count": output_segment_count,
        "gis_segment_count": candidate.segment_count,
        "canonical_product_counts": _candidate_product_counts(candidate, outcome=outcome),
    }
    return _evidence_safe(payload)


def _candidate_display_evidence(candidate: SchedulerCandidate) -> dict[str, Any]:
    tiles = _nested_bool(candidate.display_capabilities, "tiles", fallback=False)
    optional_weather_available = _nested_bool(candidate.display_capabilities, "optional_weather_available")
    unavailable_products: list[str] = []
    if tiles is False:
        unavailable_products.append("tiles")
    if optional_weather_available is False:
        unavailable_products.append("optional_weather_products")
    payload = {
        "state": "ready" if tiles else "unavailable",
        "tiles": tiles,
        "optional_weather_available": optional_weather_available,
        "unavailable_products": unavailable_products,
        "quality_flag": "ok" if not unavailable_products else "display_inputs_unavailable",
    }
    return _evidence_safe(payload)


def _candidate_quality_states(
    candidate: SchedulerCandidate,
    *,
    outcome: Mapping[str, Any] | None,
    status: str,
) -> dict[str, Any]:
    forcing = _candidate_forcing_evidence(candidate)
    display = _candidate_display_evidence(candidate)
    frequency = _candidate_frequency_evidence(candidate)
    output = _candidate_output_evidence(candidate, output_uri=None, outcome=outcome)
    payload = {
        "candidate": {
            "state": status,
            "quality_flag": "ok" if not _is_non_submitted_terminal_or_unavailable_status(status) else "blocked",
        },
        "station_forcing": {
            "state": forcing.get("state"),
            "quality_flag": forcing.get("quality_flag"),
            "station_count": forcing.get("station_count"),
        },
        "output_river": {
            "state": "ready" if (output.get("segment_count") or 0) > 0 else "unavailable",
            "quality_flag": "ok" if (output.get("segment_count") or 0) > 0 else "output_river_unavailable",
            "segment_count": output.get("segment_count"),
        },
        "frequency": frequency,
        "display": display,
    }
    return _evidence_safe(payload)


def _candidate_frequency_evidence(candidate: SchedulerCandidate) -> dict[str, Any]:
    return_periods = _nested_bool(candidate.frequency_capabilities, "return_periods", fallback=False)
    curves_available = _nested_bool(candidate.frequency_capabilities, "curves_available", fallback=return_periods)
    warning_thresholds_available = _nested_bool(candidate.frequency_capabilities, "warning_thresholds_available")
    unavailable_products: list[str] = []
    if curves_available is False:
        unavailable_products.append("return_period_curves")
    if warning_thresholds_available is False:
        unavailable_products.append("warning_thresholds")
    return _evidence_safe(
        {
            "state": "ready" if not unavailable_products and return_periods else "unavailable",
            "return_periods": return_periods,
            "curves_available": curves_available,
            "warning_thresholds_available": warning_thresholds_available,
            "unavailable_products": unavailable_products,
            "quality_flag": "ok" if not unavailable_products else "frequency_inputs_unavailable",
        }
    )


def _candidate_residual_blockers(
    candidate: SchedulerCandidate,
    *,
    outcome: Mapping[str, Any] | None,
    status: str,
    quality_states: Mapping[str, Any],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for key, state in quality_states.items():
        if not isinstance(state, Mapping):
            continue
        state_value = str(state.get("state") or "")
        if state_value not in {"blocked", "failed", "unavailable"}:
            continue
        blockers.append(
            {
                "code": str(state.get("quality_flag") or f"{key}_unavailable").upper(),
                "field": key,
                "state": state_value,
                "quality_flag": state.get("quality_flag"),
                "residual_risk": f"{key} is {state_value}; downstream readiness must keep this non-final.",
            }
        )
    if _is_non_submitted_terminal_or_unavailable_status(status):
        code = (
            str(outcome.get("reason") or outcome.get("error_code") or f"CANDIDATE_{status}").upper()
            if outcome is not None
            else f"CANDIDATE_{status}".upper()
        )
        blockers.append(
            {
                "code": code,
                "stage": (outcome.get("stage") or outcome.get("failed_stage")) if outcome is not None else None,
                "state": "blocked",
                "quality_flag": "candidate_not_successful",
                "residual_risk": f"Candidate {candidate.candidate_id} ended with status {status}.",
            }
        )
    return _evidence_safe(blockers)


def _candidate_product_counts(candidate: SchedulerCandidate, *, outcome: Mapping[str, Any] | None) -> dict[str, Any]:
    explicit = _first_present_value(outcome, candidate.resource_profile, "canonical_product_counts", "product_counts")
    if isinstance(explicit, Mapping):
        return _evidence_safe(dict(explicit))
    parsed = _first_present_int(outcome, candidate.resource_profile, "parsed_row_count", "output_row_count")
    counts: dict[str, Any] = {}
    if parsed is not None:
        counts["parsed_rows"] = parsed
    if candidate.segment_count is not None:
        counts["gis_river_segments"] = candidate.segment_count
    if candidate.output_segment_count is not None:
        counts["river_segments"] = candidate.output_segment_count
        counts["shud_output_segments"] = candidate.output_segment_count
    station_count = _candidate_station_count(candidate)
    if station_count is not None:
        counts["forcing_stations"] = station_count
    return counts


def _candidate_output_river_manifest(candidate: SchedulerCandidate) -> dict[str, Any]:
    explicit = candidate.resource_profile.get("output_river")
    if isinstance(explicit, Mapping):
        payload = dict(explicit)
    else:
        payload = {}
    output_segment_count = _coerce_output_segment_count(
        candidate.resource_profile,
        fallback=candidate.output_segment_count,
    )
    if output_segment_count is None:
        output_segment_count = candidate.segment_count
    payload.setdefault("state", "ready" if output_segment_count and output_segment_count > 0 else "unavailable")
    payload.setdefault("river_network_version_id", candidate.river_network_version_id)
    payload.setdefault("segment_count", output_segment_count)
    payload.setdefault("output_segment_count", output_segment_count)
    payload.setdefault("gis_segment_count", candidate.segment_count)
    payload.setdefault("identity_source", "resource_profile.output_segment_count")
    payload.setdefault(
        "quality_flag",
        "ok" if output_segment_count and output_segment_count > 0 else "output_river_unavailable",
    )
    return _evidence_safe(payload)


def _first_present_value(
    outcome: Mapping[str, Any] | None,
    profile: Mapping[str, Any],
    *keys: str,
) -> Any:
    for source in (outcome, profile):
        if not isinstance(source, Mapping):
            continue
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _first_present_int(
    outcome: Mapping[str, Any] | None,
    profile: Mapping[str, Any],
    *keys: str,
) -> int | None:
    value = _first_present_value(outcome, profile, *keys)
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _model_package_manifest_uri(candidate: SchedulerCandidate) -> str:
    resource_profile = dict(candidate.resource_profile)
    explicit = resource_profile.get("manifest_uri")
    if explicit not in (None, ""):
        return str(explicit)
    package_uri = candidate.model_package_uri.rstrip("/")
    if package_uri.endswith("/package"):
        return f"{package_uri.removesuffix('/package')}/manifest.json"
    return f"{package_uri}/manifest.json"


def _candidate_output_key(candidate: SchedulerCandidate) -> str:
    return f"runs/{candidate.run_id}/output/"


def _candidate_output_uri(candidate: SchedulerCandidate, object_store: Any | None = None) -> str | None:
    explicit = candidate.resource_profile.get("output_uri")
    if explicit not in (None, "") and _has_uri_scheme(str(explicit)):
        return str(explicit).rstrip("/") + "/"
    if object_store is not None:
        uri_for_key = getattr(object_store, "uri_for_key", None)
        if callable(uri_for_key):
            return str(uri_for_key(_candidate_output_key(candidate))).rstrip("/") + "/"
    return None


def _has_uri_scheme(value: str) -> bool:
    return re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value.strip()) is not None


def _candidate_station_count(candidate: SchedulerCandidate) -> int | None:
    value = candidate.resource_profile.get("station_count")
    if value in (None, ""):
        forcing = candidate.resource_profile.get("forcing_station_metadata")
        if isinstance(forcing, Mapping):
            value = forcing.get("station_count")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _candidate_station_ids(candidate: SchedulerCandidate) -> list[str]:
    value = candidate.resource_profile.get("station_ids")
    if value in (None, ""):
        forcing = candidate.resource_profile.get("forcing_station_metadata")
        if isinstance(forcing, Mapping):
            value = forcing.get("station_ids")
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [str(item) for item in value]
    return []


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


def _slurm_preflight(config: ProductionSchedulerConfig) -> dict[str, Any]:
    if not config.slurm_execution_enabled:
        return {
            "status": "not_required",
            "enabled": False,
            "blockers": [],
            "checks": {},
        }

    blockers: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}

    database_url = config.database_url
    db_blocker = _database_url_blocker(database_url)
    checks["database"] = {
        "configured": bool(database_url),
        "host": _database_host(database_url),
        "compute_node_reachable": db_blocker is None,
    }
    if db_blocker is not None:
        blockers.append(db_blocker)

    roots = {
        "workspace_root": config.workspace_root,
        "object_store_root": config.object_store_root,
        "log_root": config.log_root,
        "runtime_root": config.runtime_root,
    }
    allowed_roots = _preflight_allowed_roots(config)
    root_checks: dict[str, Any] = {}
    for field_name, value in roots.items():
        root_check, blocker = _storage_root_check(field_name, value, allowed_roots)
        root_checks[field_name] = root_check
        if blocker is not None:
            blockers.append(blocker)
    checks["storage_roots"] = root_checks
    checks["allowed_roots"] = [str(root) for root in allowed_roots]

    template_check, template_blockers = _slurm_template_allowlist_check(config)
    checks["templates"] = template_check
    blockers.extend(template_blockers)

    env_check, env_blockers = _slurm_env_check(config.slurm_env)
    checks["environment"] = env_check
    blockers.extend(env_blockers)

    shud_check, shud_blockers = _slurm_shud_executable_check(config)
    checks["shud_executable"] = shud_check
    blockers.extend(shud_blockers)

    gateway_check, gateway_blockers = _slurm_gateway_check(config)
    checks["gateway"] = gateway_check
    blockers.extend(gateway_blockers)

    grib_check, grib_blockers = _slurm_grib_env_check(config)
    checks["grib_env"] = grib_check
    blockers.extend(grib_blockers)

    return {
        "status": "blocked" if blockers else "ready",
        "enabled": True,
        "blockers": blockers,
        "checks": checks,
    }


# Hosts that, when a gateway URL points at them together with this service's own
# listen port, mean the gateway is pointing back at the scheduler/orchestrator
# itself rather than a real Slurm gateway.
_GATEWAY_SELF_HOSTS = frozenset(
    {"localhost", "localhost.localdomain", "127.0.0.1", "::1", "0.0.0.0", "::", "ip6-localhost", "ip6-loopback"}
)


def _slurm_gateway_backend() -> str:
    """Resolve the configured Slurm gateway backend without touching the network."""

    from services.slurm_gateway.config import SlurmGatewaySettings

    try:
        return str(SlurmGatewaySettings().backend or "").strip().lower()
    except Exception:  # noqa: BLE001 - config read must not break the pass.
        return ""


_GATEWAY_HEALTH_PATH = "/api/v1/slurm/health"
_GATEWAY_REQUIRED_BINARIES = ("sbatch", "squeue", "sacct", "scancel")
_GATEWAY_PROBE_TIMEOUT_SECONDS = 10.0


def _default_gateway_probe(config: ProductionSchedulerConfig) -> dict[str, Any]:
    """Bounded, fail-safe gateway health probe.

    For a **real** node-22 gateway, HTTP GETs
    ``${SLURM_GATEWAY_URL}/api/v1/slurm/health`` (the configured URL, not an
    in-process ``create_gateway().health()``) with a bounded timeout, and
    interprets the Lane 1 health structure (top-level ``healthy`` + per-binary
    ``executable`` flags). Any non-2xx response, unreachable host, malformed
    body, or exception (missing Slurm CLI on the gateway, network failure) is
    converted into ``healthy=False`` with a redacted reason rather than raised,
    so the scheduler records a BLOCKED state instead of crashing or faking PASS.

    For the **mock** dev backend (co-located, in-process, no HTTP server), reads
    in-process health instead, so a submittable dev/test run is never fenced.
    """

    from services.slurm_gateway.config import SlurmGatewaySettings

    try:
        mode = str(SlurmGatewaySettings().backend or "")
    except Exception:  # noqa: BLE001 - config read must not break the probe.
        mode = ""

    if mode not in {"real", "slurm"}:
        return _in_process_gateway_probe(mode)

    base_url = str(config.slurm_gateway_url or "").strip()
    if not base_url:
        return {
            "mode": mode,
            "healthy": False,
            "submit_capable": False,
            "accounting_available": False,
            "reason": "SLURM_GATEWAY_URL is not configured.",
        }

    url = base_url.rstrip("/") + _GATEWAY_HEALTH_PATH
    try:
        import httpx

        with httpx.Client(timeout=_GATEWAY_PROBE_TIMEOUT_SECONDS) as client:
            response = client.get(url)
        if response.status_code // 100 != 2:
            return {
                "mode": mode,
                "healthy": False,
                "submit_capable": False,
                "accounting_available": False,
                "reason": f"gateway health returned HTTP {response.status_code}",
            }
        payload = response.json()
        if not isinstance(payload, Mapping):
            return {
                "mode": mode,
                "healthy": False,
                "submit_capable": False,
                "accounting_available": False,
                "reason": "gateway health returned a non-object body",
            }
        return _interpret_gateway_health(payload, mode=mode)
    except Exception as error:  # noqa: BLE001 - probe must be fail-safe, never raise.
        return {
            "mode": mode,
            "healthy": False,
            "submit_capable": False,
            "accounting_available": False,
            "reason": str(redact_payload(str(error))),
        }


def _slurm_gateway_check(
    config: ProductionSchedulerConfig,
    *,
    probe: Callable[[ProductionSchedulerConfig], Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate the node-22 Slurm gateway before any submission.

    Two independent gates, both required by the spec:

    1. **Self-reference (deterministic, no network):** reject a gateway URL that
       points back at this service's own listen address.
    2. **Health/availability (bounded, injectable, fail-safe):** probe gateway
       health, submit capability, and accounting availability. ``probe`` is
       injectable for tests (mirrors ``check_shud_executable``). An explicitly
       unavailable / unhealthy gateway -> ``SLURM_GATEWAY_UNAVAILABLE``; a probe
       that cannot determine state fails safe as BLOCKED rather than faking PASS.

    Evidence records the gateway ``mode`` and ``host:port`` but never any
    credential (userinfo is stripped, payload is redacted). When the gateway is
    genuinely healthy and reachable this adds NO blocker, so it never wrongly
    fences a submittable run (never-break-userspace).
    """

    checks: dict[str, Any] = {}
    blockers: list[dict[str, Any]] = []

    backend = _slurm_gateway_backend()
    self_blocker, endpoint = _gateway_self_reference_blocker(
        config.slurm_gateway_url, config.service_port, backend=backend
    )
    checks["endpoint"] = endpoint
    checks["self_reference"] = self_blocker is not None
    if self_blocker is not None:
        # Self-reference is decisive: do not also probe a bogus endpoint.
        blockers.append(self_blocker)
        return redact_payload(checks), redact_payload(blockers)

    probe_fn = probe or _default_gateway_probe
    try:
        result = dict(probe_fn(config))
    except Exception as error:  # noqa: BLE001 - injected probe must not break the pass.
        result = {
            "healthy": False,
            "submit_capable": False,
            "accounting_available": False,
            "reason": str(redact_payload(str(error))),
        }

    checks["mode"] = result.get("mode")
    if result.get("backend") is not None:
        checks["backend"] = result.get("backend")
    if result.get("version") is not None:
        checks["version"] = result.get("version")
    healthy = bool(result.get("healthy"))
    submit_capable = bool(result.get("submit_capable", healthy))
    accounting_available = bool(result.get("accounting_available", healthy))
    checks["healthy"] = healthy
    checks["submit_capable"] = submit_capable
    checks["accounting_available"] = accounting_available

    if not (healthy and submit_capable and accounting_available):
        blockers.append(
            {
                "code": "SLURM_GATEWAY_UNAVAILABLE",
                "field": "SLURM_GATEWAY_URL",
                "message": (
                    "Slurm gateway is unavailable, unhealthy, or cannot confirm "
                    "submit/accounting capability before submission."
                ),
                "host": endpoint.get("host"),
                "port": endpoint.get("port"),
                **({"reason": str(result["reason"])} if result.get("reason") else {}),
            }
        )

    return redact_payload(checks), redact_payload(blockers)


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


def _scheduler_execution_boundary_from_cancellation(cancellation_evidence: Sequence[Mapping[str, Any]]) -> str:
    return _scheduler_evidence.scheduler_execution_boundary_from_cancellation(cancellation_evidence)


def _slurm_status_sync_proof(
    *,
    sync_required: bool = False,
    reservation: Mapping[str, Any] | None = None,
    blocked: bool = False,
) -> dict[str, Any]:
    return _scheduler_evidence.slurm_status_sync_proof(
        sync_required=sync_required,
        reservation=reservation,
        blocked=blocked,
    )


def _slurm_status_sync_proof_from_candidates(
    slurm_status_sync_evidence: Sequence[Mapping[str, Any]],
    *,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    return _scheduler_evidence.slurm_status_sync_proof_from_candidates(
        slurm_status_sync_evidence,
        reservation=reservation,
    )


def _execution_write_proof(
    *,
    reservation: Mapping[str, Any] | None = None,
    execution_required: bool = False,
    blocked: bool = False,
) -> dict[str, Any]:
    return _scheduler_evidence.execution_write_proof(
        reservation=reservation,
        execution_required=execution_required,
        blocked=blocked,
    )


def _execution_write_proof_from_evidence(
    execution_evidence: Sequence[Mapping[str, Any]],
    *,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    return _scheduler_evidence.execution_write_proof_from_evidence(
        execution_evidence,
        reservation=reservation,
    )


def _slurm_cancellation_proof(
    *,
    cancellation_required: bool = False,
    reservation: Mapping[str, Any] | None = None,
    blocked: bool = False,
) -> dict[str, Any]:
    return _scheduler_evidence.slurm_cancellation_proof(
        cancellation_required=cancellation_required,
        reservation=reservation,
        blocked=blocked,
    )


def _slurm_cancellation_proof_from_evidence(
    cancellation_evidence: Sequence[Mapping[str, Any]],
    *,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    return _scheduler_evidence.slurm_cancellation_proof_from_evidence(
        cancellation_evidence,
        reservation=reservation,
    )


def _slurm_status_sync_count(proof: Mapping[str, Any]) -> int:
    return _scheduler_evidence.slurm_status_sync_count(proof)


def _slurm_status_sync_unknown_count(proof: Mapping[str, Any]) -> int:
    return _scheduler_evidence.slurm_status_sync_unknown_count(proof)


def _slurm_status_sync_mutated(proof: Mapping[str, Any]) -> bool:
    return _scheduler_evidence.slurm_status_sync_mutated(proof)


def _slurm_status_sync_failed(proof: Mapping[str, Any]) -> bool:
    return _scheduler_evidence.slurm_status_sync_failed(proof)


def _slurm_cancelled_count(cancellation_evidence: Sequence[Mapping[str, Any]]) -> int:
    return _scheduler_evidence.slurm_cancelled_count(cancellation_evidence)


def _slurm_cancellation_blocked_count(cancellation_evidence: Sequence[Mapping[str, Any]]) -> int:
    return _scheduler_evidence.slurm_cancellation_blocked_count(cancellation_evidence)


def _slurm_cancellation_unknown_count(proof: Mapping[str, Any]) -> int:
    return _scheduler_evidence.slurm_cancellation_unknown_count(proof)


def _scheduler_mutation_proof(
    *,
    execution_write_proof: Mapping[str, Any],
    slurm_status_sync_proof: Mapping[str, Any],
    slurm_cancellation_proof: Mapping[str, Any],
) -> dict[str, bool | str]:
    return _scheduler_evidence.scheduler_mutation_proof(
        execution_write_proof=execution_write_proof,
        slurm_status_sync_proof=slurm_status_sync_proof,
        slurm_cancellation_proof=slurm_cancellation_proof,
    )


def _proof_mutation_value(proof: Mapping[str, Any]) -> bool | str:
    return _scheduler_evidence.proof_mutation_value(proof)


def _named_proof_value(proof: Mapping[str, Any], write_field: str, absent_field: str) -> bool | str:
    return _scheduler_evidence.named_proof_value(proof, write_field, absent_field)


def _slurm_submit_proof_value(proof: Mapping[str, Any]) -> bool | str:
    return _scheduler_evidence.slurm_submit_proof_value(proof)


def _pipeline_status_write_proof_value(proof: Mapping[str, Any]) -> bool | str:
    return _scheduler_evidence.pipeline_status_write_proof_value(proof)


def _pipeline_event_write_proof_value(proof: Mapping[str, Any]) -> bool | str:
    return _scheduler_evidence.pipeline_event_write_proof_value(proof)


def _merge_proof_values(*values: bool | str) -> bool | str:
    return _scheduler_evidence.merge_proof_values(*values)


def _positive_count(value: Any) -> bool:
    return _scheduler_evidence.positive_count(value)


def _nested_bool(mapping: Mapping[str, Any], key: str, *, fallback: bool | None = None) -> bool | None:
    value = mapping.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "available", "ready", "yes", "1"}:
            return True
        if normalized in {"false", "unavailable", "missing", "blocked", "no", "0"}:
            return False
    return fallback


def _default_adapters() -> Mapping[str, CycleDiscoveryAdapter]:
    from workers.data_adapters.gfs_adapter import GFSAdapter, GFSAdapterConfig
    from workers.data_adapters.ifs_adapter import IFSAdapter, IFSAdapterConfig

    return {
        "gfs": GFSAdapter(config=GFSAdapterConfig(), repository=None),
        "IFS": IFSAdapter(config=IFSAdapterConfig(), repository=None),
    }


class _MetStoreCanonicalReadinessProvider:
    def __init__(self, store: Any) -> None:
        self.store = store

    def canonical_readiness(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        forecast_hours: Sequence[int],
        policy_identity: Mapping[str, Any],
        source_object_identity: Mapping[str, Any],
        canonical_product_id: str,
        model_id: str,
        basin_id: str,
    ) -> Mapping[str, Any]:
        products = self.store.list_canonical_products(source_id=source_id, cycle_time=cycle_time)
        return evaluate_canonical_readiness(
            source_id=source_id,
            cycle_time=cycle_time,
            products=products,
            forecast_hours=forecast_hours,
            policy_identity=policy_identity,
            source_object_identity=source_object_identity,
            canonical_product_id=canonical_product_id,
            model_id=model_id,
            basin_id=basin_id,
        ).evidence


def _canonical_readiness_provider_from_env() -> CanonicalReadinessProvider:
    try:
        from packages.common.met_store import PsycopgMetStore

        return _MetStoreCanonicalReadinessProvider(PsycopgMetStore.from_env())
    except ImportError:
        return _UnavailableCanonicalReadinessProvider(
            reason="canonical_readiness_dependency_unavailable",
            dependency="canonical_readiness_provider",
            retryable=True,
        )
    except Exception:
        return _UnavailableCanonicalReadinessProvider(
            reason="canonical_readiness_provider_unavailable",
            dependency="canonical_readiness_provider",
            retryable=True,
        )


def _forcing_producer_from_env() -> ForcingProducerRunner:
    from workers.forcing_producer import ForcingProducer

    return ForcingProducer.from_env()


def _active_repository_from_env() -> ActiveCandidateRepository:
    from services.orchestrator.chain import PsycopgOrchestratorRepository

    return PsycopgOrchestratorRepository.from_env()


def _orchestrator_repository_from_env() -> Any:
    from services.orchestrator.chain import PsycopgOrchestratorRepository

    return PsycopgOrchestratorRepository.from_env()


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


def _scheduler_lock_evidence_root_preflight(config: ProductionSchedulerConfig) -> dict[str, Any]:
    if not config.require_runtime_roots:
        return _scheduler_root_preflight_not_required(config)
    allowed_roots = _scheduler_allowed_roots(config)
    allowed_roots_check, allowed_roots_blocker = _scheduler_allowed_roots_policy_check(config, allowed_roots)
    enforce_approved_roots = allowed_roots_blocker is None
    checks: dict[str, Any] = {}
    checks["allowed_roots_policy"] = allowed_roots_check
    blockers: list[dict[str, Any]] = []
    if allowed_roots_blocker is not None:
        blockers.append(allowed_roots_blocker)
    for field_name, path in (
        ("workspace_root", config._workspace_root_preflight_path),
        ("lock_root", config._lock_root_preflight_path),
        ("evidence_root", config._evidence_root_preflight_path),
    ):
        check, blocker = _scheduler_root_check(
            field_name,
            path,
            allowed_roots,
            required=True,
            must_exist=True,
            allow_create=False,
            require_approved_root=enforce_approved_roots and field_name == "workspace_root",
            require_under_workspace=field_name in {"lock_root", "evidence_root"},
            workspace_root=config._workspace_root_preflight_path.resolve(strict=False),
        )
        checks[field_name] = check
        if blocker is not None:
            blockers.append(blocker)
    return _scheduler_root_preflight_payload(config, checks, blockers)


def _scheduler_runtime_root_preflight(config: ProductionSchedulerConfig) -> dict[str, Any]:
    if not config.require_runtime_roots:
        return _scheduler_root_preflight_not_required(config)
    allowed_roots = _scheduler_allowed_roots(config)
    allowed_roots_check, allowed_roots_blocker = _scheduler_allowed_roots_policy_check(config, allowed_roots)
    enforce_approved_roots = allowed_roots_blocker is None
    checks: dict[str, Any] = {}
    checks["allowed_roots_policy"] = allowed_roots_check
    blockers: list[dict[str, Any]] = []
    if allowed_roots_blocker is not None:
        blockers.append(allowed_roots_blocker)
    for field_name, path in (
        ("workspace_root", config._workspace_root_preflight_path),
        ("object_store_root", config._object_store_root_preflight_path),
        ("published_artifact_root", config._published_artifact_root_preflight_path),
        ("runtime_root", config._runtime_root_preflight_path),
        ("temp_root", config._temp_root_preflight_path),
        ("lock_root", config._lock_root_preflight_path),
        ("evidence_root", config._evidence_root_preflight_path),
    ):
        # The published artifact root is a control-node display mount. Compute
        # stages write to object_store_root; the local publish stage creates and
        # mirrors artifacts into this root after Slurm work completes.
        allow_publish_root_create = field_name == "published_artifact_root"
        check, blocker = _scheduler_root_check(
            field_name,
            path,
            allowed_roots,
            required=True,
            must_exist=not allow_publish_root_create,
            allow_create=allow_publish_root_create,
            require_approved_root=enforce_approved_roots and field_name not in {"lock_root", "evidence_root"},
            require_under_workspace=field_name in {"lock_root", "evidence_root"},
            workspace_root=config._workspace_root_preflight_path.resolve(strict=False),
        )
        checks[field_name] = check
        if blocker is not None:
            blockers.append(blocker)
    service_role_check, service_role_blocker = _scheduler_service_role_check(config.service_role)
    checks["service_role"] = service_role_check
    if service_role_blocker is not None:
        blockers.append(service_role_blocker)
    return _scheduler_root_preflight_payload(config, checks, blockers)


def _scheduler_root_preflight_not_required(config: ProductionSchedulerConfig) -> dict[str, Any]:
    return {
        "status": "not_required",
        "required": False,
        "blockers": [],
        "checks": {},
        "allowed_roots": [str(root) for root in _scheduler_allowed_roots(config)],
    }


def _scheduler_root_preflight_payload(
    config: ProductionSchedulerConfig,
    checks: Mapping[str, Any],
    blockers: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "status": "blocked" if blockers else "ready",
        "required": True,
        "blockers": [dict(blocker) for blocker in blockers],
        "checks": dict(checks),
        "allowed_roots": [str(root) for root in _scheduler_allowed_roots(config)],
    }


def _scheduler_root_check(
    field_name: str,
    value: Path | str | None,
    allowed_roots: Sequence[Path],
    *,
    required: bool,
    must_exist: bool,
    allow_create: bool,
    require_approved_root: bool = True,
    require_under_workspace: bool = False,
    workspace_root: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if value in (None, ""):
        check = {
            "configured": False,
            "path": None,
            "exists": False,
            "is_dir": False,
            "contained": False,
            "approved_root_required": require_approved_root,
            "writable": False,
        }
        if required:
            return check, _scheduler_root_blocker(field_name, "MISSING", None)
        return check, None
    path = Path(value).expanduser()
    if not path.is_absolute():
        check = {
            "configured": True,
            "path": str(path),
            "exists": False,
            "is_dir": False,
            "contained": False,
            "approved_root_required": require_approved_root,
            "writable": False,
        }
        return check, _scheduler_root_blocker(field_name, "RELATIVE", str(path))
    resolved = path.resolve(strict=False)
    exists = False
    is_dir = False
    is_symlink = False
    writable = False
    unsafe_reason: str | None = None
    try:
        path_stat = path.lstat()
        exists = True
        is_symlink = stat.S_ISLNK(path_stat.st_mode)
        is_dir = stat.S_ISDIR(path_stat.st_mode)
        if is_dir and not is_symlink:
            writable = _directory_is_writable(path)
    except FileNotFoundError:
        exists = False
        if allow_create:
            parent = path.parent
            try:
                parent_stat = parent.lstat()
                parent_is_dir = stat.S_ISDIR(parent_stat.st_mode)
                parent_is_symlink = stat.S_ISLNK(parent_stat.st_mode)
                writable = parent_is_dir and not parent_is_symlink and _directory_is_writable(parent)
            except FileNotFoundError:
                writable = False
            except OSError as error:
                unsafe_reason = _scheduler_root_os_error_reason(error)
    except OSError as error:
        unsafe_reason = _scheduler_root_os_error_reason(error)
    contained = _path_is_under_any(resolved, allowed_roots) if require_approved_root else True
    under_workspace = True
    if require_under_workspace:
        if workspace_root is None:
            under_workspace = False
        else:
            try:
                resolved.relative_to(workspace_root)
            except ValueError:
                under_workspace = False
    check = {
        "configured": True,
        "path": str(resolved),
        "exists": exists,
        "is_dir": is_dir,
        "symlink": is_symlink,
        "contained": contained,
        "approved_root_required": require_approved_root,
        "writable": writable,
        "allow_create": allow_create,
    }
    if require_under_workspace:
        check["under_workspace"] = under_workspace
    if unsafe_reason is not None:
        check["unsafe_reason"] = unsafe_reason
        return check, _scheduler_root_blocker(field_name, unsafe_reason, str(resolved))
    if require_under_workspace and not under_workspace:
        return check, _scheduler_root_blocker(field_name, "OUT_OF_WORKSPACE", str(resolved))
    if is_symlink:
        return check, _scheduler_root_blocker(field_name, "SYMLINK", str(resolved))
    if require_approved_root and not contained:
        return check, _scheduler_root_blocker(field_name, "OUT_OF_APPROVED_ROOT", str(resolved))
    if must_exist and not exists:
        return check, _scheduler_root_blocker(field_name, "NOT_FOUND", str(resolved))
    if exists and not is_dir:
        return check, _scheduler_root_blocker(field_name, "NOT_DIRECTORY", str(resolved))
    if not writable:
        return check, _scheduler_root_blocker(field_name, "NOT_WRITABLE", str(resolved))
    return check, None


def _scheduler_root_blocker(field_name: str, reason: str, path: str | None) -> dict[str, Any]:
    code = f"SCHEDULER_ROOT_{field_name.upper()}_{reason}"
    blocker = {
        "code": code,
        "field": field_name,
        "reason": reason.lower(),
        "message": f"Production scheduler {field_name} is not a safe writable runtime root.",
    }
    if path is not None:
        blocker["path"] = path
    return blocker


def _scheduler_root_os_error_reason(error: OSError) -> str:
    if error.errno in {ELOOP, ENOTDIR}:
        return "UNSAFE_PATH"
    if error.errno in {EACCES, EPERM}:
        return "NOT_WRITABLE"
    return "UNAVAILABLE"


def _directory_is_writable(path: Path) -> bool:
    try:
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
            return False
        if path_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH) == 0:
            return False
        if path_stat.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0:
            return False
        return os.access(path, os.W_OK | os.X_OK)
    except OSError:
        return False


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


def _scheduler_service_role_check(service_role: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    role = (service_role or "").strip()
    check = {"configured": bool(role), "value": role or None, "compute_control": role == "compute_control"}
    if role != "compute_control":
        return (
            check,
            {
                "code": "SCHEDULER_ROOT_SERVICE_ROLE_NOT_COMPUTE_CONTROL",
                "field": "NHMS_SERVICE_ROLE",
                "message": "Production scheduler no-flag business validation must run as compute_control.",
            },
        )
    return check, None


def _scheduler_allowed_roots_policy_check(
    config: ProductionSchedulerConfig,
    allowed_roots: Sequence[Path],
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    configured_roots = tuple(root for root in config.allowed_storage_roots if root not in (None, ""))
    check = {
        "env": "NHMS_SCHEDULER_ALLOWED_ROOTS",
        "configured": bool(configured_roots),
        "non_empty": bool(allowed_roots),
        "allowed_roots": [str(root) for root in allowed_roots],
        "independent_policy_required": True,
    }
    if not allowed_roots:
        return check, _scheduler_root_blocker("allowed_roots", "MISSING", None)
    return check, None


def _scheduler_allowed_roots(config: ProductionSchedulerConfig) -> tuple[Path, ...]:
    roots: list[Path] = []
    for value in config.allowed_storage_roots:
        if value in (None, ""):
            continue
        root = Path(value).expanduser().resolve(strict=False)
        if root not in roots:
            roots.append(root)
    return tuple(roots)


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
    normalized = status.strip().lower()
    return (
        _is_failed_model_run_status(normalized)
        or normalized
        in {
            "blocked",
            "cancelled",
            "preflight_blocked",
            "unavailable",
        }
        or normalized.endswith(("_blocked", "_cancelled", "_unavailable"))
    )


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


def _normalize_sources(sources: Sequence[str]) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    normalized: list[str] = []
    exclusions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_source in sources:
        source_id = normalize_source_id(raw_source)
        if source_id in seen:
            exclusions.append(
                {
                    "type": "source",
                    "source_id": source_id,
                    "status": "excluded",
                    "reason": "duplicate_source",
                }
            )
            continue
        seen.add(source_id)
        normalized.append(source_id)
    return tuple(normalized), exclusions


def _confined_path(value: Path | str, workspace_root: Path, field_name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = workspace_root / path
    resolved_parent = path.parent.resolve()
    candidate = resolved_parent / path.name
    _require_under_workspace(resolved_parent, workspace_root, field_name)
    return candidate


def _reject_blank_config_path(value: Path | str | None, field_name: str) -> None:
    if isinstance(value, str) and value.strip() == "":
        raise ValueError(f"production scheduler {field_name} must not be blank")


def _optional_config_path(value: Path | str | None) -> Path | None:
    if value in (None, ""):
        return None
    return Path(value).expanduser().resolve()


def _config_path_preserve_final_component(value: Path | str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.parent.resolve(strict=False) / path.name


def _config_path_relative_to_preserve_final(value: Path | str, base: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.parent.resolve(strict=False) / path.name


def _optional_config_path_relative_to_preserve_final(value: Path | str | None, base: Path) -> Path | None:
    if value in (None, ""):
        return None
    return _config_path_relative_to_preserve_final(value, base)


def _resolve_optional_config_path(value: Path | None) -> Path | None:
    if value is None:
        return None
    return value.resolve()


def _optional_config_path_relative_to(value: Path | str | None, base: Path) -> Path | None:
    if value in (None, ""):
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _env_flag(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    try:
        return int(str(value))
    except ValueError:
        return default


def _env_allowed_cycle_hours_utc(name: str, default: Sequence[int]) -> tuple[int, ...]:
    value = os.getenv(name)
    if value is None:
        return _normalize_allowed_cycle_hours_utc(default)
    return _parse_allowed_cycle_hours_utc(value, name)


def _parse_allowed_cycle_hours_utc(value: str, name: str = "allowed_cycle_hours_utc") -> tuple[int, ...]:
    if value == "":
        raise ValueError(f"{name} must contain at least one UTC cycle hour")
    parsed: list[int] = []
    for raw_token in value.split(","):
        token = raw_token.strip()
        if token == "":
            raise ValueError(f"{name} must not contain empty cycle hour tokens")
        try:
            hour = int(token)
        except ValueError as error:
            raise ValueError(f"{name} must contain integer UTC cycle hours") from error
        parsed.append(hour)
    return _normalize_allowed_cycle_hours_utc(parsed, field_name=name)


def _normalize_allowed_cycle_hours_utc(
    value: Sequence[int],
    *,
    field_name: str = "allowed_cycle_hours_utc",
) -> tuple[int, ...]:
    hours: set[int] = set()
    try:
        raw_hours = iter(value)
    except TypeError as error:
        raise ValueError(f"production scheduler {field_name} must contain integer UTC cycle hours") from error
    for raw_hour in raw_hours:
        if isinstance(raw_hour, bool) or not isinstance(raw_hour, int):
            raise ValueError(f"production scheduler {field_name} must contain integer UTC cycle hours")
        hour = raw_hour
        if hour < 0 or hour > 23:
            raise ValueError(f"production scheduler {field_name} must only contain values in 0..23")
        hours.add(hour)
    if not hours:
        raise ValueError(f"production scheduler {field_name} must contain at least one UTC cycle hour")
    return tuple(sorted(hours))


def _env_path_list(name: str) -> tuple[str, ...]:
    value = os.getenv(name)
    if value in (None, ""):
        return ()
    return tuple(item.strip() for item in str(value).split(os.pathsep) if item.strip())


def _require_under_workspace(path: Path, workspace_root: Path, field_name: str) -> None:
    try:
        path.relative_to(workspace_root)
    except ValueError as error:
        raise ValueError(f"production scheduler {field_name} must be under workspace_root") from error


def _require_safe_directory_final_component(path: Path, workspace_root: Path, field_name: str) -> None:
    _require_under_workspace(path.parent.resolve(), workspace_root, field_name)
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValueError(f"production scheduler {field_name} must be a safe directory") from error
    if stat.S_ISLNK(path_stat.st_mode):
        resolved = path.resolve(strict=False)
        _require_under_workspace(resolved, workspace_root, field_name)
        if resolved.exists() and not resolved.is_dir():
            raise ValueError(f"production scheduler {field_name} must be a directory")
        return
    if not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError(f"production scheduler {field_name} must be a directory")


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
