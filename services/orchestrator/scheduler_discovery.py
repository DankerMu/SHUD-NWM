from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from packages.common.redaction import redact_payload
from packages.common.source_identity import normalize_source_id
from services.orchestrator.scheduler_state import _ensure_utc, _evidence_safe, _format_utc
from workers.data_adapters.base import CycleDiscovery, cycle_id_for

MAX_DISCOVERED_CYCLES = 10000


class SchedulerResourceLimitError(ValueError):
    def __init__(self, reason: str, details: Mapping[str, Any]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = dict(details)


class CycleDiscoveryAdapter(Protocol):
    def discover_cycles(
        self,
        cycle_date: str | date | datetime,
        end_date: str | date | datetime | None = None,
    ) -> list[CycleDiscovery]:
        raise NotImplementedError


class SchedulerConfigLike(Protocol):
    sources: Sequence[str]
    allowed_cycle_hours_utc: Sequence[int]
    lookback_hours: int
    cycle_lag_hours: int
    max_cycles_per_source: int
    backfill_enabled: bool
    retry_limit: int
    candidate_state_job_limit: int
    candidate_state_event_limit: int


class SchedulerModelLike(Protocol):
    model_id: str


class SchedulerCandidateLike(Protocol):
    source_id: str
    cycle_time_utc: datetime
    model_id: str
    run_id: str
    forcing_version_id: str
    candidate_id: str


class CandidateStateDecisionLike(Protocol):
    reason: str | None


class DiscoverSourceWindowProvider(Protocol):
    def __call__(
        self,
        adapter: CycleDiscoveryAdapter,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[CycleDiscovery]:
        raise NotImplementedError


class CycleCompletionStatusProvider(Protocol):
    def __call__(
        self,
        discovery: CycleDiscovery,
        models: Sequence[SchedulerModelLike],
        *,
        horizon: Mapping[str, Any] | None = None,
    ) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class SchedulerSourceCycle:
    discovery: CycleDiscovery
    horizon: Mapping[str, Any]


@dataclass(frozen=True)
class SchedulerDiscoveryContext:
    config: SchedulerConfigLike
    adapters: Mapping[str, CycleDiscoveryAdapter]
    active_repository: Any | None
    floor_to_source_cycle_boundary: Callable[[datetime, Sequence[str]], datetime]
    source_horizon_metadata: Callable[[CycleDiscovery, CycleDiscoveryAdapter], dict[str, Any]]
    candidate_factory: Callable[..., SchedulerCandidateLike]
    candidate_state_provider_caller: Callable[..., Mapping[str, Any] | None]
    candidate_state_decider: Callable[
        [SchedulerCandidateLike, Mapping[str, Any] | None],
        CandidateStateDecisionLike | None,
    ]
    discover_source_window_provider: DiscoverSourceWindowProvider | None = None
    cycle_completion_status_provider: CycleCompletionStatusProvider | None = None


def cycle_completion_status(
    context: SchedulerDiscoveryContext,
    discovery: CycleDiscovery,
    models: Sequence[SchedulerModelLike],
    *,
    horizon: Mapping[str, Any] | None = None,
) -> str:
    """Return 'complete' if every model's full pipeline is done for this cycle, else 'gap'."""

    completed_provider = (
        getattr(context.active_repository, "has_completed_pipeline", None)
        if context.active_repository is not None
        else None
    )
    if callable(completed_provider) and models:
        if all(
            completed_provider(
                source_id=discovery.source_id,
                cycle_time=discovery.cycle_time,
                model_id=model.model_id,
            )
            for model in models
        ):
            return "complete"

    state_provider = (
        getattr(context.active_repository, "candidate_state", None)
        if context.active_repository is not None
        else None
    )
    if callable(state_provider) and models:
        cycle_horizon = dict(horizon or {})
        for model in models:
            candidate = context.candidate_factory(discovery=discovery, model=model, horizon=cycle_horizon)
            state = context.candidate_state_provider_caller(
                state_provider,
                source_id=candidate.source_id,
                cycle_time=candidate.cycle_time_utc,
                model_id=candidate.model_id,
                run_id=candidate.run_id,
                forcing_version_id=candidate.forcing_version_id,
                candidate_id=candidate.candidate_id,
                retry_limit=context.config.retry_limit,
                job_limit=context.config.candidate_state_job_limit,
                event_limit=context.config.candidate_state_event_limit,
            )
            decision = context.candidate_state_decider(candidate, state)
            if decision is None or decision.reason not in {
                "terminal_hydro_success",
                "terminal_pipeline_success",
            }:
                return "gap"
        return "complete"

    if not callable(completed_provider) or not models:
        return "gap"
    for model in models:
        if not completed_provider(
            source_id=discovery.source_id,
            cycle_time=discovery.cycle_time,
            model_id=model.model_id,
        ):
            return "gap"
    return "complete"


def discover_cycles(
    context: SchedulerDiscoveryContext,
    started_at: datetime,
    models: Sequence[SchedulerModelLike] = (),
) -> tuple[list[SchedulerSourceCycle], list[dict[str, Any]]]:
    raw_end_time = started_at - timedelta(hours=context.config.cycle_lag_hours)
    end_time = context.floor_to_source_cycle_boundary(raw_end_time, context.config.sources)
    start_time = context.floor_to_source_cycle_boundary(
        end_time - timedelta(hours=context.config.lookback_hours),
        context.config.sources,
    )
    source_cycles: list[SchedulerSourceCycle] = []
    evidence: list[dict[str, Any]] = []
    seen_cycles: set[tuple[str, str]] = set()
    source_order = {source_id.lower(): index for index, source_id in enumerate(context.config.sources)}
    backfill_mode = bool(context.config.backfill_enabled and models)

    for source_id in context.config.sources:
        adapter = context.adapters.get(source_id)
        if adapter is None:
            source_evidence = {
                "source_id": source_id,
                "available": False,
                "status": "blocked",
                "reason": "source_adapter_unavailable",
                "cycle_id": None,
                "cycle_time_utc": None,
            }
            evidence.append(source_evidence)
            continue

        source_window_provider = context.discover_source_window_provider or discover_source_window
        discoveries = source_window_provider(
            adapter,
            source_id=source_id,
            start_time=start_time,
            end_time=end_time,
        )
        discoveries = [
            discovery
            for discovery in discoveries
            if discovery.source_id == source_id and start_time <= _ensure_utc(discovery.cycle_time) <= end_time
        ]
        discoveries, disallowed = _filter_allowed_cycle_hours(
            discoveries,
            allowed_cycle_hours_utc=context.config.allowed_cycle_hours_utc,
        )
        evidence.extend(_cycle_hour_not_allowed_evidence(discovery) for discovery in disallowed)
        discoveries.sort(key=lambda discovery: discovery.cycle_time, reverse=not backfill_mode)
        deduped: list[CycleDiscovery] = []
        for discovery in discoveries:
            cycle_key = (source_id, cycle_id_for(source_id, discovery.cycle_time))
            if cycle_key in seen_cycles:
                evidence.append(_duplicate_cycle_evidence(discovery, reason="duplicate_source_cycle"))
                continue
            seen_cycles.add(cycle_key)
            deduped.append(discovery)

        if backfill_mode:
            selected_for_source = _select_backfill_source_cycles(
                context,
                source_id=source_id,
                adapter=adapter,
                discoveries=deduped,
                models=models,
                evidence=evidence,
            )
        else:
            selected_for_source = _select_legacy_source_cycles(
                context,
                adapter=adapter,
                discoveries=deduped,
                evidence=evidence,
            )

        for discovery in selected_for_source:
            horizon = context.source_horizon_metadata(discovery, adapter)
            source_cycles.append(SchedulerSourceCycle(discovery=discovery, horizon=horizon))
            evidence.append(_source_cycle_evidence(discovery, horizon=horizon))

    source_cycles.sort(
        key=lambda item: (
            item.discovery.cycle_time,
            source_order.get(item.discovery.source_id.lower(), 999),
            item.discovery.cycle_hour,
        )
    )
    if backfill_mode and source_cycles:
        earliest_cycle_time = min(item.discovery.cycle_time for item in source_cycles)
        deferred_later_cycles = [
            item for item in source_cycles if item.discovery.cycle_time > earliest_cycle_time
        ]
        if deferred_later_cycles:
            source_cycles = [
                item for item in source_cycles if item.discovery.cycle_time == earliest_cycle_time
            ]
            for item in deferred_later_cycles:
                evidence.append(
                    _backfill_deferred_evidence(
                        item.discovery,
                        reason="backfill_deferred_waiting_for_global_prior_cycle",
                    )
                )
    return source_cycles, evidence


def _select_backfill_source_cycles(
    context: SchedulerDiscoveryContext,
    *,
    source_id: str,
    adapter: CycleDiscoveryAdapter,
    discoveries: Sequence[CycleDiscovery],
    models: Sequence[SchedulerModelLike],
    evidence: list[dict[str, Any]],
) -> list[CycleDiscovery]:
    complete_count = 0
    gaps: list[CycleDiscovery] = []
    for discovery in discoveries:
        horizon = context.source_horizon_metadata(discovery, adapter)
        completion_status_provider = context.cycle_completion_status_provider
        if completion_status_provider is None:
            status = cycle_completion_status(context, discovery, models, horizon=horizon)
        else:
            status = completion_status_provider(discovery, models, horizon=horizon)
        if status == "complete":
            complete_count += 1
            continue
        gaps.append(discovery)
    available_gaps = [discovery for discovery in gaps if discovery.available]
    unavailable_gaps = [discovery for discovery in gaps if not discovery.available]
    # Backfill cycles feed cross-cycle warm start state. Even when operators
    # raise max_cycles_per_source for discovery breadth, only the oldest
    # available incomplete cycle may execute in this pass; later available gaps
    # wait until the prior window has produced a usable state. Unavailable
    # source cycles are evidence only and must not consume the execution slot.
    selected_for_source = available_gaps[:1]
    deferred = available_gaps[1:]
    for discovery in unavailable_gaps:
        item = _source_cycle_evidence(discovery, horizon=context.source_horizon_metadata(discovery, adapter))
        item["selection_status"] = "not_selected"
        item["selection_reason"] = _source_cycle_not_selected_reason(discovery)
        evidence.append(item)
    for discovery in deferred:
        evidence.append(
            _backfill_deferred_evidence(
                discovery,
                reason="backfill_deferred_waiting_for_prior_cycle",
            )
        )
    evidence.append(
        {
            "type": "backfill_audit",
            "source_id": source_id,
            "discovered_count": len(discoveries),
            "complete_count": complete_count,
            "gap_count": len(gaps),
            "available_gap_count": len(available_gaps),
            "unavailable_gap_count": len(unavailable_gaps),
            "selected_count": len(selected_for_source),
            "deferred_count": len(deferred),
        }
    )
    return selected_for_source


def _select_legacy_source_cycles(
    context: SchedulerDiscoveryContext,
    *,
    adapter: CycleDiscoveryAdapter,
    discoveries: Sequence[CycleDiscovery],
    evidence: list[dict[str, Any]],
) -> list[CycleDiscovery]:
    available: list[CycleDiscovery] = []
    unavailable_deferred: list[CycleDiscovery] = []
    for discovery in discoveries:
        if discovery.available:
            available.append(discovery)
        else:
            unavailable_deferred.append(discovery)
    if available:
        selected_for_source = available[: context.config.max_cycles_per_source]
    else:
        selected_for_source = unavailable_deferred[: context.config.max_cycles_per_source]
    selected_ids = {discovery.cycle_id for discovery in selected_for_source}
    for discovery in [item for item in unavailable_deferred if item.cycle_id not in selected_ids]:
        item = _source_cycle_evidence(discovery, horizon=context.source_horizon_metadata(discovery, adapter))
        item["selection_status"] = "not_selected"
        item["selection_reason"] = _source_cycle_not_selected_reason(discovery)
        evidence.append(item)
    return selected_for_source


def discover_source_window(
    adapter: CycleDiscoveryAdapter,
    *,
    source_id: str,
    start_time: datetime,
    end_time: datetime,
) -> list[CycleDiscovery]:
    discoveries: list[CycleDiscovery] = []
    current_date = start_time.date()
    while current_date <= end_time.date():
        try:
            daily = adapter.discover_cycles(current_date)
        except TypeError:
            daily = adapter.discover_cycles(current_date, None)
        if len(discoveries) + len(daily) > MAX_DISCOVERED_CYCLES:
            raise SchedulerResourceLimitError(
                "cycle_discovery_limit_exceeded",
                {
                    "max_discovered_cycles": MAX_DISCOVERED_CYCLES,
                    "discovered_cycle_count": len(discoveries) + len(daily),
                    "source_id": source_id,
                    "cycle_date": current_date.isoformat(),
                },
            )
        discoveries.extend(daily)
        current_date += timedelta(days=1)
    return discoveries


def _filter_allowed_cycle_hours(
    discoveries: Sequence[CycleDiscovery],
    *,
    allowed_cycle_hours_utc: Sequence[int],
) -> tuple[list[CycleDiscovery], list[CycleDiscovery]]:
    allowed = {int(hour) for hour in allowed_cycle_hours_utc}
    selected: list[CycleDiscovery] = []
    excluded: list[CycleDiscovery] = []
    for discovery in discoveries:
        if _ensure_utc(discovery.cycle_time).hour in allowed:
            selected.append(discovery)
        else:
            excluded.append(discovery)
    return selected, excluded


def _source_cycle_evidence(discovery: CycleDiscovery, *, horizon: Mapping[str, Any]) -> dict[str, Any]:
    available = bool(discovery.available)
    status = discovery.status or ("discovered" if available else "unavailable")
    cycle_time_hour_utc = _ensure_utc(discovery.cycle_time).hour
    evidence = {
        "source_id": discovery.source_id,
        "cycle_id": discovery.cycle_id,
        "cycle_time_utc": _format_utc(discovery.cycle_time),
        "cycle_hour": cycle_time_hour_utc,
        "horizon": dict(horizon),
        "available": available,
        "status": status,
        "reason": (
            discovery.reason if discovery.reason is not None else (None if available else "source_cycle_unavailable")
        ),
        "classifier": discovery.classifier,
        "retryable": discovery.retryable,
        "probe_uri": _source_secret_text_safe(discovery.probe_uri) if discovery.probe_uri is not None else None,
        "db_cycle_status_written": None,
        "cycle_status_candidate": _source_cycle_status_candidate(discovery, available=available),
    }
    if discovery.evidence:
        evidence["discovery_evidence"] = _source_discovery_evidence_safe(discovery.evidence)
    return _evidence_safe(evidence)


def _cycle_hour_not_allowed_evidence(discovery: CycleDiscovery) -> dict[str, Any]:
    evidence = _source_cycle_evidence(discovery, horizon={})
    evidence["selection_status"] = "excluded"
    evidence["selection_reason"] = "cycle_hour_not_allowed"
    evidence["status"] = "excluded"
    evidence["reason"] = "cycle_hour_not_allowed"
    return evidence


def _source_cycle_status_candidate(discovery: CycleDiscovery, *, available: bool) -> str:
    if available:
        return "discovered"
    if discovery.status == "probe_failed" or discovery.reason == "source_cycle_probe_failed":
        return "probe_failed"
    if discovery.status == "rate_limited" or discovery.reason == "source_cycle_rate_limited":
        return "rate_limited"
    return "unavailable"


def _source_cycle_not_selected_reason(discovery: CycleDiscovery) -> str:
    if discovery.reason == "source_cycle_probe_failed" or discovery.status == "probe_failed":
        return "source_cycle_probe_failed_does_not_consume_source_budget"
    if discovery.reason == "source_cycle_rate_limited" or discovery.status == "rate_limited":
        return "source_cycle_rate_limited_does_not_consume_source_budget"
    return "source_cycle_unavailable_does_not_consume_source_budget"


SOURCE_DISCOVERY_SENSITIVE_KEY_RE = re.compile(
    r"(authorization|auth|header|env|token|signature|credential|secret|password|passwd|pwd|api[_-]?key|"
    r"access[_-]?key|session[_-]?key)",
    re.IGNORECASE,
)
SOURCE_DISCOVERY_SENSITIVE_TEXT_RE = re.compile(
    r"(authorization|bearer|basic|token|signature|credential|secret|password|passwd|pwd|api[_-]?key|"
    r"access[_-]?key|session[_-]?key)",
    re.IGNORECASE,
)


def _source_discovery_evidence_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            if SOURCE_DISCOVERY_SENSITIVE_KEY_RE.search(key_text):
                redacted["[redacted_key]"] = "[redacted]"
            else:
                redacted[key_text] = _source_discovery_evidence_safe(nested)
        return _evidence_safe(redacted)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_source_discovery_evidence_safe(item) for item in value]
    if isinstance(value, str):
        return _source_secret_text_safe(value)
    return _evidence_safe(value)


def _source_secret_text_safe(value: str) -> str:
    raw = str(value)
    safe = redact_payload(raw)
    if not isinstance(safe, str):
        return str(safe)
    if safe == raw and SOURCE_DISCOVERY_SENSITIVE_TEXT_RE.search(safe):
        return "[redacted]"
    return SOURCE_DISCOVERY_SENSITIVE_TEXT_RE.sub("[redacted]", safe)


def _duplicate_cycle_evidence(discovery: CycleDiscovery, *, reason: str) -> dict[str, Any]:
    return {
        "type": "source_cycle",
        "source_id": discovery.source_id,
        "cycle_id": cycle_id_for(discovery.source_id, discovery.cycle_time),
        "cycle_time_utc": _format_utc(discovery.cycle_time),
        "cycle_hour": _ensure_utc(discovery.cycle_time).hour,
        "available": discovery.available,
        "status": "excluded",
        "reason": reason,
    }


def _backfill_deferred_evidence(discovery: CycleDiscovery, *, reason: str) -> dict[str, Any]:
    return {
        "type": "backfill_deferred",
        "source_id": discovery.source_id,
        "cycle_id": cycle_id_for(discovery.source_id, discovery.cycle_time),
        "cycle_time_utc": _format_utc(discovery.cycle_time),
        "cycle_hour": _ensure_utc(discovery.cycle_time).hour,
        "available": discovery.available,
        "status": "gap",
        "reason": reason,
    }


def source_horizon_metadata(discovery: CycleDiscovery, adapter: CycleDiscoveryAdapter) -> dict[str, Any]:
    source_id = normalize_source_id(discovery.source_id)
    cycle_time = _ensure_utc(discovery.cycle_time)
    config = getattr(adapter, "config", None)
    max_lead_hours: int | None = None
    forecast_step_hours: int | None = None
    forecast_start_hour = 0
    if config is not None and hasattr(config, "forecast_end_hour_for_cycle"):
        max_lead_hours = int(config.forecast_end_hour_for_cycle(cycle_time.hour))
    elif config is not None and hasattr(config, "forecast_end_hour"):
        max_lead_hours = int(getattr(config, "forecast_end_hour"))
    elif source_id == "IFS":
        max_lead_hours = 144 if cycle_time.hour in {6, 18} else 168
    elif source_id == "gfs":
        max_lead_hours = 168
    if config is not None and hasattr(config, "forecast_step_hours"):
        forecast_step_hours = int(getattr(config, "forecast_step_hours"))
    if config is not None and hasattr(config, "forecast_start_hour"):
        forecast_start_hour = int(getattr(config, "forecast_start_hour"))
    return {
        "max_lead_hours": max_lead_hours,
        "forecast_horizon_hours": max_lead_hours,
        "forecast_start_hour": forecast_start_hour,
        "forecast_step_hours": forecast_step_hours,
        "policy": "source_cycle",
    }
