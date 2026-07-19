from __future__ import annotations

# ruff: noqa: E402,F401,F821,I001

import importlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any
from uuid import uuid4 as _uuid4

from packages.common.model_registry import PsycopgModelRegistryStore
from packages.common.redaction import redact_payload  # noqa: F401
from packages.common.slurm_env import (
    iter_secret_manifest_findings,
)
from packages.common.source_identity import normalize_source_id
from packages.common.state_manager import FileStateSnapshotIndexRepository, StateManager
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
    _db_free_default_adapters,
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
from services.orchestrator.scheduler_state_compat import install_scheduler_state_compat
from workers.data_adapters.base import CycleDiscovery, cycle_id_for, format_cycle_time

globals().update(install_scheduler_state_compat(globals(), _scheduler_state_module))

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
MAX_LOOKBACK_HOURS = 336
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
SLURM_ARRAY_STAGE_NAMES = {"forcing", "forecast", "parse", "state_save_qc"}
SAFE_SLURM_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
SAFE_SLURM_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:=,@+\-]*$")
SHELL_META_RE = re.compile(r"[;|&$`<>\n\r]")
PRODUCTION_SLURM_ENV_PASSTHROUGH_KEYS = (
    "GFS_FORECAST_START_HOUR",
    "GFS_FORECAST_END_HOUR",
    "GFS_FORECAST_STEP_HOURS",
    "GFS_FORECAST_RESOLUTION_SEGMENTS",
    "IFS_FORECAST_START_HOUR",
    "IFS_FORECAST_END_HOUR",
    "IFS_FORECAST_STEP_HOURS",
    "IFS_FORECAST_RESOLUTION_SEGMENTS",
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
    "exclude_nodes",
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
    "parse_output",
    "save_state_snapshot",
    "save_state_snapshot_array",
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


from services.orchestrator.scheduler_core import ProductionScheduler

ProductionScheduler.__module__ = __name__

from services.orchestrator.scheduler_file_providers import (  # noqa: E402
    CANONICAL_READINESS_INDEX_SCHEMA_VERSION,
    REGISTRY_MANIFEST_SCHEMA_VERSION,
    FileCanonicalReadinessProvider,
    FileRawHandoffCandidateRepository,
    FileSchedulerModelRegistry,
    SchedulerFileProviderError,
    publish_canonical_readiness_index,
    publish_scheduler_registry_manifest,
)
from services.orchestrator.file_orchestration_journal import (  # noqa: E402
    FILE_ORCHESTRATION_JOURNAL_SCHEMA_VERSION,
    FILE_ORCHESTRATION_LATEST_SCHEMA_VERSION,
    FileJournalRetryService,
    FileOrchestrationJournalError,
    FileOrchestrationJournalRepository,
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


def _candidate_execution_cohort_run_id(
    source_id: str,
    cycle_time: datetime,
    cohort_key: tuple[int, str],
    candidates: Sequence[SchedulerCandidate],
) -> str:
    return _scheduler_execution.candidate_execution_cohort_run_id(
        source_id,
        cycle_time,
        cohort_key,
        candidates,
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
        run_id_for_cohort=_candidate_execution_cohort_run_id,
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




from services.orchestrator import scheduler_candidate_runtime as _scheduler_candidate_runtime

globals().update(_scheduler_candidate_runtime.EXPORTS)

from services.orchestrator import scheduler_compat_runtime as _scheduler_compat_runtime

globals().update(_scheduler_compat_runtime.EXPORTS)
