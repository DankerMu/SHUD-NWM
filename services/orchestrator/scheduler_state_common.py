from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from packages.common.object_store import LocalObjectStore
from packages.common.redaction import redact_payload
from packages.common.slurm_env import secret_manifest_key_reason, secret_manifest_value_reason
from services.orchestrator.scheduler_state_types import SchedulerCandidateLike
from workers.data_adapters.base import format_cycle_time


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _coerce_optional_nonnegative_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number

def _first_state_datetime(payload: Mapping[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        value = payload.get(key)
        parsed = _parse_state_datetime(value)
        if parsed is not None:
            return parsed
    return None

def _parse_state_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, str) and value.strip():
        try:
            return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None

def _first_state_int(payload: Mapping[str, Any], *keys: str, default: int | None = None) -> int | None:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return _coerce_int(value, default=default or 0)
    return default

def _first_nonempty(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None

def _coerce_mapping_for_state(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None

def _redact_secret_manifest_for_evidence(value: Any, path: str = "manifest") -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            field_path = f"{path}.{key_text}"
            if secret_manifest_key_reason(key_text) is not None and not (
                key_text.lower().endswith("_configured") and isinstance(nested, bool)
            ):
                continue
            redacted[key_text] = _redact_secret_manifest_for_evidence(nested, field_path)
        return redacted
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_redact_secret_manifest_for_evidence(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, str) and secret_manifest_value_reason(value) is not None:
        return "[redacted]"
    return value

def _candidate_production_identity(candidate: SchedulerCandidateLike) -> dict[str, Any]:
    identity = {
        "run_id": candidate.run_id,
        "model_id": candidate.model_id,
        "basin_id": candidate.basin_id,
        "source": candidate.source_id,
        "source_id": candidate.source_id,
        "cycle_time": _format_utc(candidate.cycle_time_utc),
        "basin_version_id": candidate.basin_version_id,
        "river_network_version_id": candidate.river_network_version_id,
        "canonical_product_id": _candidate_canonical_product_id(candidate),
        "forcing_version_id": candidate.forcing_version_id,
        "hydro_run_id": candidate.run_id,
        "published_manifest_id": _candidate_published_manifest_id(candidate),
    }
    pipeline_job_id = _candidate_contract_pipeline_job_id(candidate)
    if pipeline_job_id not in (None, ""):
        identity["pipeline_job_id"] = pipeline_job_id
    return identity

def _candidate_canonical_product_id(candidate: SchedulerCandidateLike) -> str:
    explicit = candidate.resource_profile.get("canonical_product_id")
    if explicit not in (None, ""):
        return str(explicit)
    return f"canon_{candidate.source_id.lower()}_{format_cycle_time(candidate.cycle_time_utc)}"

def _candidate_published_manifest_id(candidate: SchedulerCandidateLike) -> str:
    explicit = candidate.resource_profile.get("published_manifest_id")
    if explicit not in (None, ""):
        return str(explicit)
    return f"manifest_{candidate.run_id}"

def _candidate_contract_pipeline_job_id(candidate: SchedulerCandidateLike) -> str | None:
    explicit = candidate.resource_profile.get("pipeline_job_id")
    if explicit not in (None, ""):
        return str(explicit)
    return None

def _evidence_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return _format_utc(value)
    if isinstance(value, Mapping):
        manifest_redacted = _redact_secret_manifest_for_evidence(
            {str(key): _evidence_safe(nested) for key, nested in value.items()},
            "evidence",
        )
        return redact_payload(manifest_redacted)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_evidence_safe(item) for item in value]
    if isinstance(value, str):
        return redact_payload(value)
    return value

def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)

def _format_utc(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")

def _forecast_cycle_manifest_uri(candidate: SchedulerCandidateLike, state: Mapping[str, Any]) -> str | None:
    forecast_cycle = state.get("forecast_cycle")
    if isinstance(forecast_cycle, Mapping):
        value = forecast_cycle.get("manifest_uri")
        if value not in (None, ""):
            return str(value)
    value = state.get("manifest_uri") or state.get("raw_manifest_uri")
    if value not in (None, ""):
        return str(value)
    return f"raw/{candidate.source_id}/{format_cycle_time(candidate.cycle_time_utc)}/manifest.json"

def _is_raw_manifest_object_uri(manifest_uri: str) -> bool:
    value = manifest_uri.strip()
    if value.startswith("s3://"):
        parsed = urlparse(value)
        path = parsed.path.lstrip("/")
        return path.startswith("raw/") and path.endswith("/manifest.json")
    return value.startswith("raw/") and value.endswith("/manifest.json")

def _object_manifest_is_missing(candidate: SchedulerCandidateLike, manifest_uri: str) -> bool:
    object_root = candidate.resource_profile.get("object_store_root") or os.getenv("OBJECT_STORE_ROOT")
    if object_root in (None, ""):
        return False
    prefix = str(candidate.resource_profile.get("object_store_prefix") or os.getenv("OBJECT_STORE_PREFIX", ""))
    return not LocalObjectStore(str(object_root), object_store_prefix=prefix).exists(manifest_uri)

def _first_nested_state_value(payload: Mapping[str, Any], aliases: Sequence[tuple[str, ...]]) -> Any:
    for path in aliases:
        current: Any = payload
        for key in path:
            if not isinstance(current, Mapping) or key not in current:
                current = None
                break
            current = current[key]
        if current not in (None, ""):
            return current
    return None

def _looks_like_production_job_id(value: Any) -> bool:
    text = str(value or "")
    return text.startswith(("job_fcst_", "job_cycle_"))

def _stage_cycle_run_matches_candidate(run_id: str | None, expected_values: Mapping[str, str]) -> bool:
    if run_id in (None, ""):
        return False
    source = str(expected_values.get("source") or "").lower()
    cycle_time = str(expected_values.get("cycle_time") or "")
    model_id = str(expected_values.get("model_id") or "")
    if not source or not cycle_time or not model_id:
        return False
    try:
        compact_cycle = format_cycle_time(cycle_time)
    except (TypeError, ValueError):
        return False
    prefix = f"cycle_{source}_{compact_cycle}"
    text = str(run_id)
    return text == prefix or (
        text.startswith(f"{prefix}_") and (text.endswith(f"_{model_id}") or f"_{model_id}_" in text)
    )
