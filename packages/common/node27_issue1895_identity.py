"""Current identity-only latest-product binding for hot GFS/IFS lanes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlencode

from packages.common.node27_issue1895_http import bound_response_json
from packages.common.node27_issue1895_lanes import (
    EXACT_IDENTITY_SQL,
    SOURCES,
    assert_shared_network,
    identity_from_row,
    identity_projection,
)
from packages.common.node27_issue1895_query import iso_utc, parse_issue_time, scenario_for_source
from packages.common.node27_issue1895_types import Issue1895ReadinessError

IDENTITY_ONLY_PATH = "/api/v1/mvp/qhh/latest-product"
IDENTITY_BODY_LIMIT = 65536
FORBIDDEN_SERIES_FIELDS = frozenset(
    {"series", "values", "headers", "river_series", "station_series", "coverage"}
)
REQUIRED_DATA_FIELDS = (
    "run_id",
    "model_id",
    "basin_id",
    "basin_version_id",
    "river_network_version_id",
    "source_id",
    "cycle_time",
)


def identity_only_url(*, origin: str, source: str, basin_id: str) -> str:
    query = urlencode({"source": source, "identity_only": "true", "basin_id": basin_id})
    return f"{origin}{IDENTITY_ONLY_PATH}?{query}"


def _mapping(value: object, *, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Issue1895ReadinessError(
            "identity-only payload is not an object",
            code=code,
            stage="identity",
        )
    return value


def validate_identity_only_product(
    payload: Any,
    *,
    source: str,
    basin_id: str,
) -> dict[str, Any]:
    """Validate `{status:'ok', data:{status:'ready', ...}}` without series bodies."""

    bound_response_json(payload, code="API_JSON_TOO_COMPLEX", stage="identity")
    document = _mapping(payload, code="IDENTITY_BODY_INVALID")
    forbidden = sorted(field for field in FORBIDDEN_SERIES_FIELDS if field in document)
    if forbidden:
        raise Issue1895ReadinessError(
            "identity-only proof read a river-series body",
            code="IDENTITY_BODY_FORBIDDEN",
            stage="identity",
        )
    if document.get("status") != "ok":
        raise Issue1895ReadinessError(
            "identity-only envelope status is not ok",
            code="IDENTITY_STATUS_INVALID",
            stage="identity",
        )
    data = _mapping(document.get("data"), code="IDENTITY_DATA_INVALID")
    forbidden_data = sorted(field for field in FORBIDDEN_SERIES_FIELDS if field in data)
    if forbidden_data:
        raise Issue1895ReadinessError(
            "identity-only data contains a river-series body",
            code="IDENTITY_BODY_FORBIDDEN",
            stage="identity",
        )
    if str(data.get("status") or "") != "ready":
        raise Issue1895ReadinessError(
            "identity-only product is not ready",
            code="IDENTITY_NOT_READY",
            stage="identity",
        )
    availability = data.get("availability")
    if isinstance(availability, Mapping) and availability.get("ready") is False:
        raise Issue1895ReadinessError(
            "identity-only availability.ready is false",
            code="IDENTITY_NOT_READY",
            stage="identity",
        )
    missing = [field for field in REQUIRED_DATA_FIELDS if not str(data.get(field) or "").strip()]
    if missing:
        raise Issue1895ReadinessError(
            "identity-only product is missing required fields",
            code="IDENTITY_INCOMPLETE",
            stage="identity",
        )
    observed_source = str(data.get("source_id") or "").strip().upper()
    if observed_source != source:
        raise Issue1895ReadinessError(
            "identity-only source_id does not match the requested source",
            code="IDENTITY_SOURCE_MISMATCH",
            stage="identity",
        )
    observed_basin = str(data.get("basin_id") or "").strip()
    if observed_basin != basin_id:
        raise Issue1895ReadinessError(
            "identity-only basin_id does not match the operator pin",
            code="IDENTITY_BASIN_MISMATCH",
            stage="identity",
        )
    cycle = iso_utc(parse_issue_time(str(data["cycle_time"])))
    return {
        "run_id": str(data["run_id"]).strip(),
        "model_id": str(data["model_id"]).strip(),
        "basin_id": observed_basin,
        "basin_version_id": str(data["basin_version_id"]).strip(),
        "river_network_version_id": str(data["river_network_version_id"]).strip(),
        "source_id": source,
        "cycle_time": cycle,
        "run_status": str(data.get("run_status") or data.get("status") or "ready"),
        "scenario": scenario_for_source(source),
        "status": "ready",
    }


def bind_hot_identities(
    *,
    api_products: Mapping[str, Any],
    db_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    basin_id: str,
) -> dict[str, dict[str, Any]]:
    """Bind current GFS/IFS identity-only API products to exact readonly DB rows."""

    bound: dict[str, dict[str, Any]] = {}
    for source in SOURCES:
        if source not in api_products:
            raise Issue1895ReadinessError(
                "identity-only API product is missing for a canonical source",
                code="IDENTITY_SOURCE_MISSING",
                stage="identity",
            )
        api_identity = validate_identity_only_product(
            api_products[source],
            source=source,
            basin_id=basin_id,
        )
        rows = list(db_rows.get(source) or ())
        if len(rows) != 1:
            raise Issue1895ReadinessError(
                "exact DB identity row is missing or duplicated",
                code="IDENTITY_DB_ROW_INVALID",
                stage="identity",
            )
        db_identity = identity_from_row(rows[0], source=source)
        if identity_projection(api_identity) != identity_projection(db_identity):
            raise Issue1895ReadinessError(
                "identity-only API product does not match the exact DB row",
                code="IDENTITY_DB_MISMATCH",
                stage="identity",
            )
        bound[source] = db_identity
    assert_shared_network(tuple(bound.values()))
    if bound["GFS"]["run_id"] == bound["IFS"]["run_id"] and bound["GFS"]["cycle_time"] == bound["IFS"]["cycle_time"]:
        if bound["GFS"]["model_id"] == bound["IFS"]["model_id"]:
            raise Issue1895ReadinessError(
                "GFS and IFS current identities collapsed onto one run",
                code="IDENTITY_SOURCE_COLLAPSE",
                stage="identity",
            )
    return bound


def exact_identity_params(identity: Mapping[str, Any], *, source: str) -> tuple[str, ...]:
    return (
        str(identity["basin_id"]),
        str(identity["run_id"]),
        str(identity["model_id"]),
        str(identity["cycle_time"]),
        source,
    )


__all__ = (
    "EXACT_IDENTITY_SQL",
    "IDENTITY_BODY_LIMIT",
    "IDENTITY_ONLY_PATH",
    "bind_hot_identities",
    "exact_identity_params",
    "identity_only_url",
    "validate_identity_only_product",
)
