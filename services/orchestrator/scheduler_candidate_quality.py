from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from services.orchestrator import scheduler as _scheduler

__all__ = (
    "_candidate_artifact_refs",
    "_candidate_display_evidence",
    "_candidate_forcing_evidence",
    "_candidate_output_evidence",
    "_candidate_output_key",
    "_candidate_output_river_manifest",
    "_candidate_output_uri",
    "_candidate_product_counts",
    "_candidate_quality_states",
    "_candidate_residual_blockers",
    "_candidate_resource_summary",
    "_candidate_station_count",
    "_candidate_station_ids",
    "_first_present_int",
    "_first_present_value",
    "_has_uri_scheme",
    "_is_non_submitted_terminal_or_unavailable_status",
    "_model_package_manifest_uri",
    "_nested_bool",
)


def _candidate_artifact_refs(candidate: Any, *, output_uri: str | None) -> dict[str, Any]:
    refs = {
        "model_package_uri": _scheduler._redact_secret_manifest_for_evidence(
            candidate.model_package_uri, "model_package_uri"
        ),
        "model_package_manifest_uri": _scheduler._redact_secret_manifest_for_evidence(
            _model_package_manifest_uri(candidate),
            "model_package_manifest_uri",
        ),
        "output_key": _candidate_output_key(candidate),
    }
    resolved_output_uri = output_uri or _candidate_output_uri(candidate)
    if resolved_output_uri is not None:
        refs["output_uri"] = _scheduler._redact_secret_manifest_for_evidence(resolved_output_uri, "output_uri")
    manifest_uri = candidate.resource_profile.get("manifest_uri")
    if manifest_uri not in (None, ""):
        refs["resource_manifest_uri"] = _scheduler._redact_secret_manifest_for_evidence(
            str(manifest_uri),
            "resource_manifest_uri",
        )
    return _scheduler._evidence_safe(refs)


def _candidate_resource_summary(
    candidate: Any,
    *,
    stage_statuses: Sequence[Mapping[str, Any]],
    outcome: Mapping[str, Any] | None,
) -> dict[str, Any]:
    resource_profile = _scheduler._resource_profile_evidence(candidate.resource_profile)
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
        "candidate_accounting": dict(
            _scheduler._mapping_value(outcome.get("accounting") if outcome is not None else None)
        ),
        "candidate_resource_metrics": _scheduler._resource_metrics_from_mapping(
            (outcome.get("resource_metrics") or outcome.get("accounting")) if outcome is not None else {}
        ),
    }
    return _scheduler._evidence_safe(payload)


def _candidate_forcing_evidence(candidate: Any) -> dict[str, Any]:
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
    return _scheduler._evidence_safe(payload)


def _candidate_output_evidence(
    candidate: Any, *, output_uri: str | None, outcome: Mapping[str, Any] | None
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
        "output_uri": _scheduler._redact_secret_manifest_for_evidence(resolved_output_uri, "output_uri")
        if resolved_output_uri
        else None,
        "output_key": _candidate_output_key(candidate),
        "shud_output_uri": _scheduler._redact_secret_manifest_for_evidence(
            _first_present_value(outcome, candidate.resource_profile, "shud_output_uri", "output_uri"),
            "shud_output_uri",
        ),
        "parsed_row_count": parsed_row_count,
        "segment_count": output_segment_count,
        "output_segment_count": output_segment_count,
        "gis_segment_count": candidate.segment_count,
        "canonical_product_counts": _candidate_product_counts(candidate, outcome=outcome),
    }
    return _scheduler._evidence_safe(payload)


def _candidate_display_evidence(candidate: Any) -> dict[str, Any]:
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
    return _scheduler._evidence_safe(payload)


def _candidate_quality_states(candidate: Any, *, outcome: Mapping[str, Any] | None, status: str) -> dict[str, Any]:
    forcing = _candidate_forcing_evidence(candidate)
    display = _candidate_display_evidence(candidate)
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
        "display": display,
    }
    return _scheduler._evidence_safe(payload)


def _candidate_residual_blockers(
    candidate: Any,
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
    return _scheduler._evidence_safe(blockers)


def _candidate_product_counts(candidate: Any, *, outcome: Mapping[str, Any] | None) -> dict[str, Any]:
    explicit = _first_present_value(outcome, candidate.resource_profile, "canonical_product_counts", "product_counts")
    if isinstance(explicit, Mapping):
        return _scheduler._evidence_safe(dict(explicit))
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


def _candidate_output_river_manifest(candidate: Any) -> dict[str, Any]:
    explicit = candidate.resource_profile.get("output_river")
    payload = dict(explicit) if isinstance(explicit, Mapping) else {}
    output_segment_count = _scheduler._coerce_output_segment_count(
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
    return _scheduler._evidence_safe(payload)


def _first_present_value(outcome: Mapping[str, Any] | None, profile: Mapping[str, Any], *keys: str) -> Any:
    for source in (outcome, profile):
        if not isinstance(source, Mapping):
            continue
        for key in keys:
            value = source.get(key)
            if value not in (None, ""):
                return value
    return None


def _first_present_int(outcome: Mapping[str, Any] | None, profile: Mapping[str, Any], *keys: str) -> int | None:
    value = _first_present_value(outcome, profile, *keys)
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _model_package_manifest_uri(candidate: Any) -> str:
    resource_profile = dict(candidate.resource_profile)
    explicit = resource_profile.get("manifest_uri")
    if explicit not in (None, ""):
        return str(explicit)
    package_uri = candidate.model_package_uri.rstrip("/")
    if package_uri.endswith("/package"):
        return f"{package_uri.removesuffix('/package')}/manifest.json"
    return f"{package_uri}/manifest.json"


def _candidate_output_key(candidate: Any) -> str:
    return f"runs/{candidate.run_id}/output/"


def _candidate_output_uri(candidate: Any, object_store: Any | None = None) -> str | None:
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


def _candidate_station_count(candidate: Any) -> int | None:
    value = candidate.resource_profile.get("station_count")
    if value in (None, ""):
        forcing = candidate.resource_profile.get("forcing_station_metadata")
        if isinstance(forcing, Mapping):
            value = forcing.get("station_count")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _candidate_station_ids(candidate: Any) -> list[str]:
    value = candidate.resource_profile.get("station_ids")
    if value in (None, ""):
        forcing = candidate.resource_profile.get("forcing_station_metadata")
        if isinstance(forcing, Mapping):
            value = forcing.get("station_ids")
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return [str(item) for item in value]
    return []


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


def _is_non_submitted_terminal_or_unavailable_status(status: str) -> bool:
    normalized = status.strip().lower()
    return (
        _scheduler._is_failed_model_run_status(normalized)
        or normalized
        in {
            "blocked",
            "cancelled",
            "preflight_blocked",
            "unavailable",
        }
        or normalized.endswith(("_blocked", "_cancelled", "_unavailable"))
    )
