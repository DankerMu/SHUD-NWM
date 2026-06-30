from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Sequence

from packages.common.object_store import LocalObjectStore
from services.orchestrator.chain_types import CycleOrchestrationContext
from services.orchestrator.production_contract import production_stage_for

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")


def _runtime_forcing_metadata(values: Mapping[str, Any]) -> dict[str, Any]:
    forcing = {
        key: value
        for key, value in values.items()
        if key not in {"forcing_package_manifest_uri", "forcing_manifest_checksum"}
    }
    package_manifest_uri = (
        values.get("package_manifest_uri")
        or values.get("forcing_package_manifest_uri")
        or forcing.get("package_manifest_uri")
    )
    package_manifest_checksum = (
        values.get("package_manifest_checksum")
        or values.get("forcing_manifest_checksum")
        or forcing.get("package_manifest_checksum")
    )
    if package_manifest_uri not in (None, ""):
        forcing["package_manifest_uri"] = package_manifest_uri
    if package_manifest_checksum not in (None, ""):
        forcing["package_manifest_checksum"] = package_manifest_checksum
    return forcing


def _default_forcing_uri(
    source_id: str,
    compact_cycle: str,
    basin_version_id: str,
    model_id: str,
    object_store: LocalObjectStore,
) -> str:
    return _directory_uri(object_store, f"forcing/{source_id.lower()}/{compact_cycle}/{basin_version_id}/{model_id}/")


def _directory_uri(object_store: LocalObjectStore, key: str) -> str:
    return object_store.uri_for_key(key).rstrip("/") + "/"


def _preserve_directory_uri(value: str | None, object_store: LocalObjectStore, fallback_key: str) -> str:
    if value is not None and _has_uri_scheme(value):
        return value.rstrip("/") + "/"
    return _directory_uri(object_store, fallback_key)


def _has_uri_scheme(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    match = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", candidate)
    return match is not None


def _model_package_manifest_uri(basin: Mapping[str, Any], model_package_uri: str) -> str:
    resource_profile = _nested_mapping(basin.get("resource_profile"))
    explicit = (
        basin.get("model_package_manifest_uri")
        or basin.get("manifest_uri")
        or resource_profile.get("manifest_uri")
    )
    if explicit not in (None, ""):
        return str(explicit)
    package_uri = model_package_uri.rstrip("/")
    if package_uri.endswith("/package"):
        return f"{package_uri.removesuffix('/package')}/manifest.json"
    return f"{package_uri}/manifest.json"


def _station_metadata_for_basin(basin: Mapping[str, Any]) -> dict[str, Any]:
    resource_profile = _nested_mapping(basin.get("resource_profile"))
    explicit = _nested_mapping(
        basin.get("forcing_station_metadata")
        or basin.get("station_metadata")
        or resource_profile.get("forcing_station_metadata")
    )
    if explicit:
        station_ids = [str(item) for item in explicit.get("station_ids") or []]
        station_count = _optional_int(explicit.get("station_count"))
        if station_count is None:
            station_count = len(station_ids)
        state = "ready" if station_count > 0 else "unavailable"
        return {
            "schema_version": "nhms.forcing_station_metadata.v1",
            "state": str(explicit.get("state") or state),
            "station_count": station_count,
            "station_ids": station_ids,
            "source": str(explicit.get("source") or "registry_package_metadata"),
            "shud_station": explicit.get("shud_station"),
            "quality_flag": str(
                explicit.get("quality_flag") or ("ok" if station_count > 0 else "station_forcing_unavailable")
            ),
        }
    station_count = _optional_int(basin.get("station_count"))
    raw_station_ids = basin.get("station_ids")
    station_ids = (
        [str(item) for item in raw_station_ids or []]
        if isinstance(raw_station_ids, Sequence) and not isinstance(raw_station_ids, str | bytes)
        else []
    )
    if station_count is None and station_ids:
        station_count = len(station_ids)
    if station_count is None:
        station_count = 0
    state = "ready" if station_count > 0 else "unavailable"
    return {
        "schema_version": "nhms.forcing_station_metadata.v1",
        "state": state,
        "station_count": station_count,
        "station_ids": station_ids,
        "source": "registry_package_metadata",
        "quality_flag": "ok" if station_count > 0 else "station_forcing_unavailable",
    }


def _output_river_contract(basin: Mapping[str, Any]) -> dict[str, Any]:
    explicit = _nested_mapping(basin.get("output_river") or basin.get("shud_output_river"))
    resource_profile = _nested_mapping(basin.get("resource_profile"))
    gis_segment_count = _optional_int(basin.get("segment_count"))
    profile_output_river = _nested_mapping(resource_profile.get("output_river"))
    output_segment_count = _first_optional_int(
        basin.get("output_segment_count"),
        basin.get("shud_output_segment_count"),
        basin.get("shud_output_river_count"),
        resource_profile.get("output_segment_count"),
        resource_profile.get("shud_output_segment_count"),
        resource_profile.get("shud_output_river_count"),
        profile_output_river.get("output_segment_count"),
        profile_output_river.get("segment_count"),
    )
    if explicit:
        state = str(explicit.get("state") or "ready")
        segment_ids = [str(item) for item in explicit.get("river_segment_ids") or explicit.get("segment_ids") or []]
        explicit_segment_count = _first_optional_int(
            explicit.get("output_segment_count"),
            explicit.get("segment_count"),
        )
        resolved_segment_count = _first_optional_int(
            explicit_segment_count,
            output_segment_count,
            len(segment_ids) if segment_ids else None,
            gis_segment_count,
        )
        if resolved_segment_count is None:
            state = "unavailable"
            resolved_segment_count = 0
        return {
            "state": state,
            "river_network_version_id": str(basin["river_network_version_id"]),
            "segment_count": resolved_segment_count,
            "output_segment_count": resolved_segment_count,
            "gis_segment_count": gis_segment_count,
            "river_segment_ids": segment_ids,
            "identity_source": str(explicit.get("identity_source") or "registry_package_metadata"),
            "quality_flag": str(
                explicit.get("quality_flag") or ("ok" if state == "ready" else "output_river_unavailable")
            ),
        }
    if output_segment_count is None and gis_segment_count is None:
        return {
            "state": "unavailable",
            "river_network_version_id": str(basin["river_network_version_id"]),
            "segment_count": 0,
            "output_segment_count": 0,
            "gis_segment_count": None,
            "river_segment_ids": [],
            "identity_source": "registry_package_metadata",
            "quality_flag": "output_river_unavailable",
        }
    resolved_segment_count = output_segment_count if output_segment_count is not None else gis_segment_count
    return {
        "state": "ready" if resolved_segment_count > 0 else "unavailable",
        "river_network_version_id": str(basin["river_network_version_id"]),
        "segment_count": resolved_segment_count,
        "output_segment_count": resolved_segment_count,
        "gis_segment_count": gis_segment_count,
        "river_segment_ids": [],
        "identity_source": (
            "resource_profile.output_segment_count" if output_segment_count is not None else "registry_package_metadata"
        ),
        "quality_flag": "ok" if resolved_segment_count > 0 else "output_river_unavailable",
    }


def _display_contract(basin: Mapping[str, Any], *, output_uri: str) -> dict[str, Any]:
    capabilities = _nested_mapping(basin.get("display_capabilities"))
    optional_weather = _tri_state(
        basin.get("optional_weather_available"),
        capabilities.get("optional_weather_available"),
        capabilities.get("weather_products"),
    )
    tiles_enabled = bool(capabilities.get("tiles", True))
    unavailable = []
    if optional_weather is False:
        unavailable.append("optional_weather_products")
    return {
        "state": "ready" if tiles_enabled else "unavailable",
        "tiles_enabled": tiles_enabled,
        "output_uri": output_uri,
        "optional_weather_products": "available" if optional_weather is not False else "unavailable",
        "quality_flag": "ok" if not unavailable and tiles_enabled else "display_inputs_unavailable",
        "unavailable_products": unavailable,
    }


def _assembly_quality_states(
    basin: Mapping[str, Any],
    *,
    station_metadata: Mapping[str, Any],
    output_river: Mapping[str, Any],
    display: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    states = {
        "station_forcing": {
            "state": station_metadata.get("state"),
            "quality_flag": station_metadata.get("quality_flag"),
        },
        "display": {
            "state": display.get("state"),
            "quality_flag": display.get("quality_flag"),
            "unavailable_products": list(display.get("unavailable_products") or []),
        },
    }
    states["output_river"] = {
        "state": output_river.get("state"),
        "quality_flag": output_river.get("quality_flag"),
        "segment_count": output_river.get("segment_count"),
    }
    blockers: list[dict[str, Any]] = []
    if station_metadata.get("state") != "ready":
        blockers.append(
            {
                "code": "STATION_FORCING_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": station_metadata.get("quality_flag"),
                "residual_risk": "No forcing station metadata is available for this model package.",
            }
        )
    if output_river.get("state") != "ready":
        blockers.append(
            {
                "code": "OUTPUT_RIVER_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": output_river.get("quality_flag"),
                "residual_risk": (
                    "SHUD output-river segment metadata is unavailable; segment_count was not fabricated."
                ),
            }
        )
    for product in display.get("unavailable_products") or []:
        blockers.append(
            {
                "code": str(product).upper() + "_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": display.get("quality_flag"),
                "residual_risk": f"{product} is unavailable; durable model outputs remain reusable.",
            }
        )
    for item in basin.get("residual_blockers") or ():
        if isinstance(item, Mapping):
            blockers.append(dict(item))
    return states, blockers


def _model_run_stage_evidence(stage: str, entry: Mapping[str, Any], *, cycle_id: str) -> dict[str, Any]:
    assembly = _assembly_from_entry(entry)
    identity = dict(assembly.get("identity") or {})
    return {
        "stage": stage,
        "production_stage": production_stage_for(stage),
        "cycle_id": cycle_id,
        "candidate_id": identity.get("candidate_id") or entry.get("candidate_id"),
        "run_id": identity.get("run_id") or entry.get("run_id"),
        "hydro_run_id": identity.get("hydro_run_id") or entry.get("hydro_run_id") or entry.get("run_id"),
        "model_id": identity.get("model_id") or entry.get("model_id"),
        "source": identity.get("source") or identity.get("source_id") or entry.get("source_id"),
        "source_id": identity.get("source_id") or entry.get("source_id"),
        "cycle_time": identity.get("cycle_time") or entry.get("cycle_time"),
        "scenario_id": identity.get("scenario_id") or entry.get("scenario_id"),
        "canonical_product_id": identity.get("canonical_product_id") or entry.get("canonical_product_id"),
        "forcing_version_id": identity.get("forcing_version_id") or entry.get("forcing_version_id"),
        "published_manifest_id": identity.get("published_manifest_id") or entry.get("published_manifest_id"),
        "model_package_uri": identity.get("model_package_uri") or entry.get("model_package_uri"),
        "basin_id": identity.get("basin_id") or entry.get("basin_id"),
        "basin_version_id": identity.get("basin_version_id") or entry.get("basin_version_id"),
        "river_network_version_id": identity.get("river_network_version_id") or entry.get("river_network_version_id"),
        "output_uri": _nested_mapping(assembly.get("outputs")).get("output_uri") or entry.get("output_uri"),
        "quality_states": dict(assembly.get("quality_states") or entry.get("quality_states") or {}),
        "residual_blockers": list(assembly.get("residual_blockers") or entry.get("residual_blockers") or []),
    }


def _publish_quality_state(
    entry: Mapping[str, Any],
    *,
    cycle_id: str,
    model_run_stage_evidence: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    model_run_stage_evidence = model_run_stage_evidence or _model_run_stage_evidence
    evidence = model_run_stage_evidence("publish", entry, cycle_id=cycle_id)
    display_state = _nested_mapping(evidence.get("quality_states")).get("display") or {}
    return {
        **evidence,
        "state": _nested_mapping(display_state).get("state", "ready"),
        "quality_flag": _nested_mapping(display_state).get("quality_flag", "ok"),
        "unavailable_products": list(_nested_mapping(display_state).get("unavailable_products") or []),
    }


def _cycle_residual_blockers(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for entry in entries:
        run_id = str(entry.get("run_id") or "")
        for blocker in entry.get("residual_blockers") or []:
            if isinstance(blocker, Mapping):
                blockers.append({"run_id": run_id, **dict(blocker)})
        assembly = _assembly_from_entry(entry)
        for blocker in assembly.get("residual_blockers") or []:
            if isinstance(blocker, Mapping):
                candidate = {"run_id": run_id, **dict(blocker)}
                if candidate not in blockers:
                    blockers.append(candidate)
    return blockers


def _assembly_from_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    assembly = entry.get("model_run_assembly")
    return dict(assembly) if isinstance(assembly, Mapping) else {}


def _assembly_payload_from_runtime_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "identity": dict(_nested_mapping(manifest.get("identity"))),
        "forcing": dict(_nested_mapping(manifest.get("forcing"))),
        "runtime": dict(_nested_mapping(manifest.get("runtime"))),
        "outputs": dict(_nested_mapping(manifest.get("outputs"))),
        "display": dict(_nested_mapping(manifest.get("display"))),
        "quality_states": dict(_nested_mapping(manifest.get("quality_states"))),
        "residual_blockers": list(manifest.get("residual_blockers") or []),
    }


def _nested_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _tri_state(*values: Any) -> bool | None:
    for value in values:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "available", "ready", "yes", "1"}:
                return True
            if normalized in {"false", "unavailable", "missing", "blocked", "no", "0"}:
                return False
    return None


def _safe_project_name(value: str) -> str:
    candidate = value.strip() or "shud"
    if _SAFE_ID_RE.fullmatch(candidate):
        return candidate
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", candidate).strip("._-") or "shud"


def _project_name_for_basin(basin: Mapping[str, Any], *, fallback: str) -> str:
    resource_profile = _nested_mapping(basin.get("resource_profile"))
    runtime = _nested_mapping(basin.get("runtime"))
    for value in (
        basin.get("project_name"),
        basin.get("shud_input_name"),
        resource_profile.get("project_name"),
        resource_profile.get("shud_input_name"),
        runtime.get("project_name"),
        runtime.get("shud_input_name"),
        fallback,
    ):
        if value not in (None, ""):
            return _safe_project_name(str(value))
    return _safe_project_name(fallback)


def _cycle_payload_model_id(context: CycleOrchestrationContext) -> str:
    if context.active_basins:
        return str(context.active_basins[0].get("model_id") or "cycle")
    return "cycle"


def _basin_key(basin: Mapping[str, Any]) -> tuple[str, str]:
    return (str(basin.get("model_id") or ""), str(basin.get("basin_id") or basin.get("model_id") or ""))


def _basin_identifier(basin: Mapping[str, Any]) -> str:
    return str(basin.get("basin_id") or basin.get("model_id") or "")


def _nested_value(value: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = value
    for part in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _forecast_state_checkpoint_hours(forecast_horizon_hours: Any) -> list[int]:
    try:
        horizon = int(forecast_horizon_hours)
    except (TypeError, ValueError):
        return []
    return [hour for hour in (6, 12) if hour <= horizon]


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_optional_int(*values: Any) -> int | None:
    for value in values:
        coerced = _optional_int(value)
        if coerced is not None:
            return coerced
    return None


def _parse_gateway_time(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return _ensure_utc(value) if isinstance(value, datetime) else None
    if isinstance(value, str):
        return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    return None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _format_time_or_none(value: datetime | None) -> str | None:
    return _format_time(value) if value is not None else None
