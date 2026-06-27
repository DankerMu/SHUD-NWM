from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from services.orchestrator import scheduler as _scheduler

__all__ = (
    "active_model_duplicate_exclusions",
    "active_model_identity_groups",
    "active_model_model_id",
    "active_model_package_checksum",
    "active_model_package_uri",
    "coerce_output_segment_count",
    "coerce_registered_model",
    "fetch_active_model_details",
    "fetch_scheduler_model_detail",
    "filter_expression",
    "has_package_specific_checksum_context",
    "mapping_value",
    "matches_filters",
    "model_duplicate_identity_value_for_evidence",
    "model_exclusion",
    "resource_profile_summary",
)


def fetch_active_model_details(registry: Any) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    offset = 0
    limit = 500
    pages = 0
    while True:
        pages += 1
        if pages > _scheduler.MAX_REGISTRY_PAGES:
            raise _scheduler.SchedulerResourceLimitError(
                "registry_page_limit_exceeded",
                {"max_registry_pages": _scheduler.MAX_REGISTRY_PAGES, "model_count": len(rows)},
            )
        page = registry.list_models(basin_version_id=None, active=True, limit=limit, offset=offset)
        items = list(page.get("items") or [])
        for item in items:
            if len(rows) >= _scheduler.MAX_DISCOVERED_MODELS:
                raise _scheduler.SchedulerResourceLimitError(
                    "model_limit_exceeded",
                    {"max_discovered_models": _scheduler.MAX_DISCOVERED_MODELS, "model_count": len(rows)},
                )
            model_id = str(item.get("model_id") or "")
            rows.append(fetch_scheduler_model_detail(registry, model_id) if model_id else item)
        total = int(page.get("total") or len(rows))
        offset += len(items)
        if len(items) == 0 or offset >= total:
            break
    return rows


def fetch_scheduler_model_detail(registry: Any, model_id: str) -> Mapping[str, Any]:
    internal_getter = getattr(registry, "get_model_internal", None)
    if callable(internal_getter):
        return internal_getter(model_id)
    return registry.get_model(model_id)


def coerce_registered_model(row: Mapping[str, Any]) -> Any:
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
        return model_exclusion(row, "inactive_model")
    if resource_profile.get("runnable") is False:
        return model_exclusion(row, "not_runnable")
    if not required["shud_code_version"]:
        return model_exclusion(row, "not_shud_model")
    missing = sorted(key for key, value in required.items() if value in (None, ""))
    if missing:
        return {**model_exclusion(row, "incomplete_model_metadata"), "missing_fields": missing}

    segment_count = row.get("segment_count")
    output_segment_count = coerce_output_segment_count(resource_profile, fallback=segment_count)
    return _scheduler.RegisteredSchedulerModel(
        model_id=str(required["model_id"]),
        basin_id=str(required["basin_id"]),
        basin_version_id=str(required["basin_version_id"]),
        river_network_version_id=str(required["river_network_version_id"]),
        segment_count=int(segment_count) if segment_count not in (None, "") else None,
        output_segment_count=output_segment_count,
        model_package_uri=str(required["model_package_uri"]),
        shud_code_version=str(required["shud_code_version"]),
        resource_profile=dict(resource_profile),
        resource_profile_summary=resource_profile_summary(resource_profile),
        display_capabilities=mapping_value(resource_profile.get("display_capabilities")),
        frequency_capabilities=mapping_value(resource_profile.get("frequency_capabilities")),
    )


def active_model_duplicate_exclusions(rows: Sequence[Mapping[str, Any]]) -> dict[int, dict[str, Any]]:
    duplicate_groups: list[tuple[str, str, set[int]]] = []
    for identity_field, groups in (
        ("model_id", active_model_identity_groups(rows, active_model_model_id)),
        ("model_package_uri", active_model_identity_groups(rows, active_model_package_uri)),
        ("package_checksum", active_model_identity_groups(rows, active_model_package_checksum)),
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
                **model_exclusion(rows[index], "duplicate_active_model_identity"),
                "duplicate_identity_field": identity_field,
                "duplicate_identity_value": model_duplicate_identity_value_for_evidence(identity_field, value),
                "duplicate_model_ids": duplicate_model_ids,
                "duplicate_active_model_count": len(indexes),
            }
    return exclusions


def active_model_identity_groups(
    rows: Sequence[Mapping[str, Any]],
    value_getter: Callable[[Mapping[str, Any]], str | None],
) -> dict[str, set[int]]:
    groups: dict[str, set[int]] = {}
    for index, row in enumerate(rows):
        value = value_getter(row)
        if value:
            groups.setdefault(value, set()).add(index)
    return groups


def active_model_model_id(row: Mapping[str, Any]) -> str | None:
    value = row.get("model_id")
    return str(value) if value not in (None, "") else None


def active_model_package_uri(row: Mapping[str, Any]) -> str | None:
    value = row.get("model_package_uri")
    return str(value) if value not in (None, "") else None


def active_model_package_checksum(row: Mapping[str, Any]) -> str | None:
    resource_profile = row.get("resource_profile")
    if not isinstance(resource_profile, Mapping):
        return None
    if not has_package_specific_checksum_context(row, resource_profile):
        return None
    value = resource_profile.get("package_checksum")
    return str(value) if value not in (None, "") else None


def has_package_specific_checksum_context(row: Mapping[str, Any], resource_profile: Mapping[str, Any]) -> bool:
    if row.get("model_package_uri") not in (None, ""):
        return True
    for identity_field in ("model_package_uri", "manifest_uri", "model_package_manifest_uri", "package_uri"):
        if resource_profile.get(identity_field) not in (None, ""):
            return True
    lineage = str(resource_profile.get("lineage") or "")
    return lineage in {"basins_registry_import", "qhh_production_bootstrap"}


def model_duplicate_identity_value_for_evidence(field: str, value: str) -> str:
    if field == "model_package_uri":
        redacted = _scheduler._redact_secret_manifest_for_evidence(value, "model_package_uri")
        return str(redacted)
    if field == "package_checksum":
        return "[redacted]"
    return value


def resource_profile_summary(resource_profile: Mapping[str, Any]) -> dict[str, Any]:
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


def coerce_output_segment_count(resource_profile: Mapping[str, Any], *, fallback: Any = None) -> int | None:
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


def mapping_value(value: Any) -> Mapping[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def model_exclusion(row: Mapping[str, Any], reason: str) -> dict[str, Any]:
    return {
        "model_id": row.get("model_id"),
        "basin_id": row.get("basin_id"),
        "basin_version_id": row.get("basin_version_id"),
        "reason": reason,
    }


def matches_filters(model: Any, *, model_ids: Sequence[str], basin_ids: Sequence[str]) -> bool:
    if model_ids and model.model_id not in set(model_ids):
        return False
    return not (basin_ids and model.basin_id not in set(basin_ids) and model.basin_version_id not in set(basin_ids))


def filter_expression(model_ids: Sequence[str], basin_ids: Sequence[str]) -> str | None:
    parts: list[str] = []
    if model_ids:
        parts.append("model_id in [" + ",".join(model_ids) + "]")
    if basin_ids:
        parts.append("basin_id in [" + ",".join(basin_ids) + "]")
    return " and ".join(parts) if parts else None
