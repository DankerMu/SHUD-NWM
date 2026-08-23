from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any, Callable, Protocol

from workers.data_adapters.base import CycleDiscovery

# Pure implementations of the source-discovery evidence/metadata helpers.
#
# The owner module ``scheduler_discovery`` defines thin wrappers under the
# ORIGINAL names that pass the CURRENT owner-module globals as explicit
# dependencies, so reassigning a patchable owner symbol (a regex constant, a
# redaction/identity helper, ``MAX_DISCOVERED_CYCLES``...) is observed exactly
# as it was before the split.  These implementations take those dependencies
# as parameters and never read an owner global themselves; the dependency-free
# leaves (``_source_cycle_status_candidate``, ``_source_cycle_not_selected_reason``)
# are re-exported directly instead.


class CycleDiscoveryAdapter(Protocol):
    """Minimal adapter surface consumed by :func:`source_horizon_metadata` and
    :func:`_discover_source_window_impl`.

    Equivalent to the ``CycleDiscoveryAdapter`` protocol in
    ``scheduler_discovery``, kept local so this module stays import-order
    independent of it.  Only ``discover_cycles`` is required; ``config`` is
    read optionally via ``getattr``.
    """

    def discover_cycles(
        self,
        cycle_date: str | date | datetime,
        end_date: str | date | datetime | None = None,
    ) -> list[CycleDiscovery]:
        raise NotImplementedError


def _filter_allowed_cycle_hours_impl(
    discoveries: Sequence[CycleDiscovery],
    *,
    allowed_cycle_hours_utc: Sequence[int],
    ensure_utc: Callable[[datetime], datetime],
) -> tuple[list[CycleDiscovery], list[CycleDiscovery]]:
    allowed = {int(hour) for hour in allowed_cycle_hours_utc}
    selected: list[CycleDiscovery] = []
    excluded: list[CycleDiscovery] = []
    for discovery in discoveries:
        if ensure_utc(discovery.cycle_time).hour in allowed:
            selected.append(discovery)
        else:
            excluded.append(discovery)
    return selected, excluded


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


def _source_secret_text_safe_impl(
    value: str,
    *,
    redact_payload_fn: Callable[[str], Any],
    sensitive_text_re: re.Pattern[str],
) -> str:
    raw = str(value)
    safe = redact_payload_fn(raw)
    if not isinstance(safe, str):
        return str(safe)
    if safe == raw and sensitive_text_re.search(safe):
        return "[redacted]"
    return sensitive_text_re.sub("[redacted]", safe)


def _duplicate_cycle_evidence_impl(
    discovery: CycleDiscovery,
    *,
    reason: str,
    ensure_utc: Callable[[datetime], datetime],
    format_utc: Callable[[datetime], str],
    cycle_id_for_fn: Callable[[str, datetime], str],
) -> dict[str, Any]:
    return {
        "type": "source_cycle",
        "source_id": discovery.source_id,
        "cycle_id": cycle_id_for_fn(discovery.source_id, discovery.cycle_time),
        "cycle_time_utc": format_utc(discovery.cycle_time),
        "cycle_hour": ensure_utc(discovery.cycle_time).hour,
        "available": discovery.available,
        "status": "excluded",
        "reason": reason,
    }


def _backfill_deferred_evidence_impl(
    discovery: CycleDiscovery,
    *,
    reason: str,
    ensure_utc: Callable[[datetime], datetime],
    format_utc: Callable[[datetime], str],
    cycle_id_for_fn: Callable[[str, datetime], str],
) -> dict[str, Any]:
    return {
        "type": "backfill_deferred",
        "source_id": discovery.source_id,
        "cycle_id": cycle_id_for_fn(discovery.source_id, discovery.cycle_time),
        "cycle_time_utc": format_utc(discovery.cycle_time),
        "cycle_hour": ensure_utc(discovery.cycle_time).hour,
        "available": discovery.available,
        "status": "gap",
        "reason": reason,
    }


def source_horizon_metadata_impl(
    discovery: CycleDiscovery,
    adapter: CycleDiscoveryAdapter,
    *,
    ensure_utc: Callable[[datetime], datetime],
    normalize_source_id_fn: Callable[[str], str],
) -> dict[str, Any]:
    source_id = normalize_source_id_fn(discovery.source_id)
    cycle_time = ensure_utc(discovery.cycle_time)
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


def _discover_source_window_impl(
    adapter: CycleDiscoveryAdapter,
    *,
    source_id: str,
    start_time: datetime,
    end_time: datetime,
    max_discovered_cycles: int,
    resource_limit_error: type[BaseException],
) -> list[CycleDiscovery]:
    discoveries: list[CycleDiscovery] = []
    current_date = start_time.date()
    while current_date <= end_time.date():
        try:
            daily = adapter.discover_cycles(current_date)
        except TypeError:
            daily = adapter.discover_cycles(current_date, None)
        if len(discoveries) + len(daily) > max_discovered_cycles:
            raise resource_limit_error(
                "cycle_discovery_limit_exceeded",
                {
                    "max_discovered_cycles": max_discovered_cycles,
                    "discovered_cycle_count": len(discoveries) + len(daily),
                    "source_id": source_id,
                    "cycle_date": current_date.isoformat(),
                },
            )
        discoveries.extend(daily)
        current_date += timedelta(days=1)
    return discoveries
