"""Live-PostGIS MVT tile generation: bind construction, budget signals, fetchers.

Split out of `apps/api/routes/hydro_display.py` (#2026). `_fetch_postgis_tile_bytes`,
`MVT_MAX_COORDINATES` and `national_discharge_cycle_coverage` are patched by tests
through THIS module: the four `_fetch_*_tile_bytes` wrappers and
`_postgis_tile_params` resolve them from here, and the facade's
`river_network_national_mvt_tile` route reaches `_fetch_postgis_tile_bytes` through
this module object for the same reason, so exactly one patch target exists.

`logger` is imported from `hydro_display_constants` rather than derived from
`__name__`: #2030's `MVT_TILE_BUDGET_TRUNCATED` / `MVT_TILE_FEATURE_OVERFLOW_BLANKED`
warnings must keep the `apps.api.routes.hydro_display` logger name that
`tests/test_hydro_display_mvt_scaling.py` and systemd observe.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from apps.api.errors import ApiError
from apps.api.routes.hydro_display_catalog import _require_live_postgis_mvt
from apps.api.routes.hydro_display_constants import logger
from apps.api.routes.hydro_display_instants import _format_time
from services.tiles.mvt import (
    MVT_BUFFER,
    MVT_EXTENT,
    MVT_MAX_COORDINATES,
    SUPPORTED_HYDRO_MVT_VARIABLES,
    collection_coordinate_limit,
    feature_limit,
    national_discharge_cycle_coverage,
    postgis_tile_sql,
    public_hydro_layer_id,
    simplification_tolerance_m,
)


def _fetch_postgis_tile_bytes(session: Session, layer: str, params: dict[str, Any], *, z: int, x: int, y: int) -> bytes:
    _require_live_postgis_mvt(session, layer)
    detail_layer_id = (
        public_hydro_layer_id(str(params["variable"]))
        if layer in {"hydro", "hydro-national"} and "variable" in params
        else layer
    )
    max_coordinates = collection_coordinate_limit(layer)
    bind = _postgis_tile_params(params, z=z, x=x, y=y, layer=layer)
    # #2165: one value for the bind, the 413 predicate and the truncation signal.
    max_features = bind["feature_limit"]
    row = session.execute(text(postgis_tile_sql(layer)), bind).mappings().first()
    feature_count = int(row.get("feature_count") or 0) if row else 0
    coordinate_count = int(row.get("coordinate_count") or 0) if row else 0
    source_identity_count = int(row.get("source_identity_count") or 0) if row else 0
    invalid_property_count = int(row.get("invalid_property_count") or 0) if row else 0
    # #2030: pre-truncation totals over `bounded_rows` plus the two non-budget
    # drop counters, so the fair-budget window stops dropping rows silently.
    intersecting_feature_count = int(row.get("intersecting_feature_count") or 0) if row else 0
    intersecting_coordinate_count = int(row.get("intersecting_coordinate_count") or 0) if row else 0
    feature_coordinate_overflow_count = int(row.get("feature_coordinate_overflow_count") or 0) if row else 0
    coordinate_dimension_overflow_count = int(row.get("coordinate_dimension_overflow_count") or 0) if row else 0
    # #2166: the largest per-feature coordinate count / dimension over
    # `bounded_rows`, reported next to the limits when an overflow blanks the tile.
    feature_coordinate_count = int(row.get("feature_coordinate_count") or 0) if row else 0
    coordinate_dimension_count = int(row.get("coordinate_dimension_count") or 0) if row else 0
    if invalid_property_count > 0:
        raise ApiError(
            status_code=500,
            code="MVT_TILE_CONTRACT_INVALID",
            message="Live PostGIS MVT tile source rows violate the public tile contract.",
            details={
                "layer_id": detail_layer_id,
                "z": z,
                "x": x,
                "y": y,
                "invalid_property_count": invalid_property_count,
                "properties": _mvt_invalid_properties(row.get("invalid_properties") if row else None),
            },
        )
    if feature_count > max_features or coordinate_count > max_coordinates:
        raise ApiError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="Live PostGIS MVT tile exceeded the configured feature or coordinate budget.",
            details={
                "layer_id": detail_layer_id,
                "z": z,
                "x": x,
                "y": y,
                "feature_count": feature_count,
                "max_features": max_features,
                "coordinate_count": coordinate_count,
                "max_coordinates": max_coordinates,
            },
        )
    if not row or source_identity_count <= 0:
        raise ApiError(
            status_code=424,
            code="MVT_LIVE_POSTGIS_UNAVAILABLE",
            message="Live PostGIS MVT query returned no source rows for the requested identity.",
            details={"layer_id": detail_layer_id, "z": z, "x": x, "y": y},
        )
    # #2030: on a window layer `budget_stats` is computed FROM the already
    # truncated `eligible`, so the 413 predicate above is unreachable there and an
    # over-budget tile is a 200 with fewer rows. Compare the selected totals with
    # the intersecting ones and say so. Both overflow counters must be 0: those
    # two paths drop rows before `budget_stats` for a different reason and keep
    # their existing (signal-free) behavior.
    if (
        (intersecting_coordinate_count > coordinate_count or intersecting_feature_count > feature_count)
        and feature_coordinate_overflow_count == 0
        and coordinate_dimension_overflow_count == 0
    ):
        logger.warning(
            "MVT_TILE_BUDGET_TRUNCATED layer_id=%s z=%s x=%s y=%s "
            "feature_count=%s/%s max_features=%s coordinate_count=%s/%s max_coordinates=%s",
            detail_layer_id,
            z,
            x,
            y,
            feature_count,
            intersecting_feature_count,
            max_features,
            coordinate_count,
            intersecting_coordinate_count,
            max_coordinates,
            extra={
                "layer_id": detail_layer_id,
                "z": z,
                "x": x,
                "y": y,
                "feature_count": feature_count,
                "intersecting_feature_count": intersecting_feature_count,
                "max_features": max_features,
                "coordinate_count": coordinate_count,
                "intersecting_coordinate_count": intersecting_coordinate_count,
                "max_coordinates": max_coordinates,
            },
        )
    # #2166: either overflow counter empties the shared `budget_gate`, so the tile
    # is a 200 with zero features and is cached like any other generation. HTTP
    # status and caching stay as they are (an error would never be cached and
    # every request would re-run SQL that cannot succeed); say so instead. The two
    # maxima are the values bound for THIS query, so the record reports the
    # limits that were actually in force.
    if feature_coordinate_overflow_count > 0 or coordinate_dimension_overflow_count > 0:
        max_feature_coordinates = bind["feature_coordinate_limit"]
        max_coordinate_dimensions = bind["max_coordinate_dimensions"]
        logger.warning(
            "MVT_TILE_FEATURE_OVERFLOW_BLANKED layer_id=%s z=%s x=%s y=%s "
            "feature_coordinate_overflow_count=%s feature_coordinate_count=%s max_feature_coordinates=%s "
            "coordinate_dimension_overflow_count=%s coordinate_dimension_count=%s max_coordinate_dimensions=%s",
            detail_layer_id,
            z,
            x,
            y,
            feature_coordinate_overflow_count,
            feature_coordinate_count,
            max_feature_coordinates,
            coordinate_dimension_overflow_count,
            coordinate_dimension_count,
            max_coordinate_dimensions,
            extra={
                "layer_id": detail_layer_id,
                "z": z,
                "x": x,
                "y": y,
                "feature_coordinate_overflow_count": feature_coordinate_overflow_count,
                "feature_coordinate_count": feature_coordinate_count,
                "max_feature_coordinates": max_feature_coordinates,
                "coordinate_dimension_overflow_count": coordinate_dimension_overflow_count,
                "coordinate_dimension_count": coordinate_dimension_count,
                "max_coordinate_dimensions": max_coordinate_dimensions,
            },
        )
    return bytes(row["tile"] or b"")


def _mvt_invalid_properties(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [item for item in str(value).split(",") if item]


def _fetch_hydro_mvt_tile_bytes(
    session: Session,
    *,
    run_id: str,
    variable: str,
    valid_time: datetime,
    basin_version_id: str,
    river_network_version_id: str,
    z: int,
    x: int,
    y: int,
) -> bytes:
    return _fetch_postgis_tile_bytes(
        session,
        "hydro",
        {
            "run_id": run_id,
            "variable": variable,
            "valid_time": valid_time,
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
        },
        z=z,
        x=x,
        y=y,
    )


def _fetch_hydro_national_mvt_tile_bytes(
    session: Session,
    *,
    variable: str,
    valid_time: datetime,
    z: int,
    x: int,
    y: int,
    source: str | None,
    cycle: datetime | None,
) -> bytes:
    if source is not None and cycle is not None:
        # #2153: an identity only SOME active networks cover renders a national map
        # whose missing basins look like "no flow", and it would be cached. Refuse
        # it with the per-cycle valid-times rule (one helper, set comparison). The
        # gate goes first so disabled/sqlite keeps its 424 with no statement run;
        # covered = empty falls through to the tile SQL's own no-run 424 unchanged.
        _require_live_postgis_mvt(session, "hydro-national")
        coverage = national_discharge_cycle_coverage(session, source=source, cycle=cycle)
        if coverage.covered_networks and not coverage.complete:
            raise ApiError(
                status_code=424,
                code="MVT_NATIONAL_IDENTITY_INCOMPLETE",
                message="The requested national identity is not covered by every active river network.",
                details={
                    "layer_id": public_hydro_layer_id(variable),
                    "source": source,
                    "cycle": _format_time(cycle),
                    "covered_network_count": len(coverage.covered_networks),
                    "active_network_count": len(coverage.active_networks),
                },
            )
    return _fetch_postgis_tile_bytes(
        session,
        "hydro-national",
        {"variable": variable, "valid_time": valid_time, "source": source, "cycle": cycle},
        z=z,
        x=x,
        y=y,
    )


def _fetch_river_network_mvt_tile_bytes(
    session: Session,
    *,
    basin_version_id: str,
    z: int,
    x: int,
    y: int,
) -> bytes:
    return _fetch_postgis_tile_bytes(session, "river-network", {"basin_version_id": basin_version_id}, z=z, x=x, y=y)


def _fetch_station_mvt_tile_bytes(
    session: Session,
    *,
    basin_version_id: str,
    z: int,
    x: int,
    y: int,
) -> bytes:
    return _fetch_postgis_tile_bytes(session, "met-stations", {"basin_version_id": basin_version_id}, z=z, x=x, y=y)


def _validate_supported_hydro_variable(variable: str) -> None:
    if variable in SUPPORTED_HYDRO_MVT_VARIABLES:
        return
    raise ApiError(
        status_code=422,
        code="VALIDATION_ERROR",
        message="Unsupported hydrological MVT variable.",
        details={"variable": variable, "supported": list(SUPPORTED_HYDRO_MVT_VARIABLES)},
    )


def _postgis_tile_params(
    params: dict[str, Any], *, z: int, x: int, y: int, layer: str | None = None
) -> dict[str, Any]:
    return {
        **params,
        "z": z,
        "x": x,
        "y": y,
        "feature_limit": feature_limit(layer),
        "feature_coordinate_limit": MVT_MAX_COORDINATES,
        "collection_coordinate_limit": collection_coordinate_limit(layer),
        "max_coordinate_dimensions": 3,
        "extent": MVT_EXTENT,
        "buffer": MVT_BUFFER,
        "simplification_tolerance_m": simplification_tolerance_m(z),
    }
