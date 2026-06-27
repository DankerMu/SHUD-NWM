# ruff: noqa: E402,E501,F401,F821,I001
from __future__ import annotations

from services.orchestrator import scheduler as _scheduler

globals().update(_scheduler.__dict__)

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


EXPORTS = {
    name: value
    for name, value in globals().items()
    if name
    in (
        "_SCHEDULER_CANDIDATE_COMPAT_ALIAS_NAMES",
        "_SCHEDULER_CANDIDATE_COMPAT_ALIAS_OWNER_NAMES",
        "_SCHEDULER_CANDIDATE_COMPAT_FACADE_ALIASES",
        "_SCHEDULER_CANDIDATE_COMPAT_FACADE_MISSING",
        "_SCHEDULER_CANDIDATE_COMPAT_FORWARDER_NAMES",
        "_SCHEDULER_CANDIDATE_COMPAT_OWNER_ALIASES",
        "_SCHEDULER_CANDIDATE_COMPAT_OWNER_MISSING",
        "_SCHEDULER_DISCOVERY_COMPAT_ALIAS_NAMES",
        "_SCHEDULER_DISCOVERY_COMPAT_ALIAS_OWNER_NAMES",
        "_SCHEDULER_DISCOVERY_COMPAT_FACADE_ALIASES",
        "_SCHEDULER_DISCOVERY_COMPAT_FACADE_MISSING",
        "_SCHEDULER_DISCOVERY_COMPAT_FORWARDER_NAMES",
        "_SCHEDULER_DISCOVERY_COMPAT_OWNER_ALIASES",
        "_SCHEDULER_DISCOVERY_COMPAT_OWNER_MISSING",
        "_SCHEDULER_EVIDENCE_COMPAT_DIRECT_NAMES",
        "_SCHEDULER_EVIDENCE_COMPAT_DIRECT_OWNER_NAMES",
        "_SCHEDULER_EVIDENCE_COMPAT_FACADE_DIRECTS",
        "_SCHEDULER_EVIDENCE_COMPAT_FACADE_MISSING",
        "_SCHEDULER_EVIDENCE_COMPAT_FACADE_WRAPPERS",
        "_SCHEDULER_EVIDENCE_COMPAT_FORWARDER_NAMES",
        "_SCHEDULER_EVIDENCE_COMPAT_FORWARDER_OWNER_NAMES",
        "_SCHEDULER_EVIDENCE_COMPAT_OWNER_DIRECTS",
        "_SCHEDULER_EVIDENCE_COMPAT_OWNER_MISSING",
        "_SCHEDULER_EVIDENCE_COMPAT_OWNER_WRAPPERS",
        "_SCHEDULER_EVIDENCE_COMPAT_WRAPPER_NAMES",
        "_SCHEDULER_EVIDENCE_COMPAT_WRAPPER_OWNER_NAMES",
        "_SCHEDULER_EXECUTION_COMPAT_FACADE_MISSING",
        "_SCHEDULER_EXECUTION_COMPAT_FACADE_WRAPPERS",
        "_SCHEDULER_EXECUTION_COMPAT_FORWARDER_NAMES",
        "_SCHEDULER_EXECUTION_COMPAT_FORWARDER_OWNER_NAMES",
        "_SCHEDULER_EXECUTION_COMPAT_OWNER_MISSING",
        "_SCHEDULER_EXECUTION_COMPAT_OWNER_WRAPPERS",
        "_SCHEDULER_EXECUTION_COMPAT_WRAPPER_NAMES",
        "_SCHEDULER_EXECUTION_COMPAT_WRAPPER_OWNER_NAMES",
        "_accepted_horizon_from_hours",
        "_bounded_evidence_payload",
        "_evidence_status",
        "_now",
        "_sleep",
        "_source_forecast_hours",
        "_source_object_identity",
        "_source_policy_identity",
    )
}
