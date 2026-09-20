"""Layer definitions, MVT response shapes and the shared display logger.

Split out of `apps/api/routes/hydro_display.py` (#2026). This module imports
nothing from the facade, so both the facade and every owner module can depend on
it without a cycle.
"""

from __future__ import annotations

import logging

from services.tiles.mvt import (
    MVT_MAX_TILE_COORDINATE,
    MVT_MAX_ZOOM,
    MVT_MEDIA_TYPE,
)

# The literal logger name, not `__name__`: #2030's two budget warnings are
# emitted from `hydro_display_postgis.py` after the #2026 split and must keep
# reaching this exact logger, which is what `caplog` and systemd observe.
# #2030: budget-window truncation signal. `apps.api.routes.hydro_display` is a
# child of the `apps.api` tree that `apps/api/main.py::_install_api_log_handler`
# gives a stderr handler, so WARNING+ reaches systemd's
# `StandardError=append:/tmp/display-api.log` with no extra wiring.
logger = logging.getLogger("apps.api.routes.hydro_display")


HYDRO_NATIONAL_SOURCE_ID = "hydro-national"
HYDRO_NATIONAL_SOURCE_VERSION = "hydro-national-latest-per-basin-stream-type-v3"
RIVER_NETWORK_NATIONAL_SOURCE_ID = "river-network-national"
DISPLAY_PRODUCT_READY_STATUSES = {"succeeded", "parsed", "published"}
PUBLIC_LAYER_DEFINITIONS: tuple[tuple[str, str, str, list[str]], ...] = (
    ("discharge", "Discharge", "hydrology", ["q_down"]),
    ("river-network", "River network", "base", ["geometry"]),
    ("met-stations", "Meteorological stations", "base", ["station_point"]),
    # #2010: the precipitation raster is a PNG overlay, not an MVT layer. Its
    # entry is independent of `run_id` and of live-PostGIS readiness, and its
    # valid times come from `/api/v1/precip/{source}/{cycle}/index`, not from
    # `valid-times` (which answers `[]` for it via the non-discharge branch).
    ("precip", "Precipitation (past 24h)", "meteorology", ["precip_24h"]),
)
SUPPORTED_PUBLIC_LAYER_IDS = frozenset(definition[0] for definition in PUBLIC_LAYER_DEFINITIONS)
MVT_RESPONSE_HEADERS = {
    "Cache-Control": {"schema": {"type": "string"}},
    "ETag": {"schema": {"type": "string"}},
    "X-Tile-Layer-ID": {"schema": {"type": "string"}},
    "X-Tile-Checksum": {"schema": {"type": "string"}},
    "X-Tile-Cache": {"schema": {"type": "string", "enum": ["hit", "miss", "bypass"]}},
    "X-Tile-Cache-Key": {"schema": {"type": "string"}},
    "X-MVT-Schema-Version": {"schema": {"type": "string"}},
}
MVT_COLD_BUSY_RESPONSE_HEADERS = {
    "Retry-After": {"schema": {"type": "string"}},
    "Cache-Control": {"schema": {"type": "string"}},
    "X-Request-ID": {"schema": {"type": "string"}},
}
MVT_ROUTE_RESPONSES = {
    200: {
        "description": "Raw Mapbox vector tile",
        "headers": MVT_RESPONSE_HEADERS,
        "content": {MVT_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
    },
    424: {
        "description": "Live PostGIS MVT is unavailable for this canonical tile route.",
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
    },
    503: {
        "description": "Cold MVT generation is saturated; retry after the stated delay.",
        "headers": MVT_COLD_BUSY_RESPONSE_HEADERS,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ErrorResponse"}}},
    },
    "4XX": {"description": "MVT request validation error."},
    "5XX": {"description": "MVT server error."},
}
TILE_X_DESCRIPTION = (
    f"Web Mercator XYZ tile column. Global schema bounds are 0..{MVT_MAX_TILE_COORDINATE} "
    f"for max zoom {MVT_MAX_ZOOM}; each request also enforces 0 <= x < 2^z."
)
TILE_Y_DESCRIPTION = (
    f"Web Mercator XYZ tile row. Global schema bounds are 0..{MVT_MAX_TILE_COORDINATE} "
    f"for max zoom {MVT_MAX_ZOOM}; each request also enforces 0 <= y < 2^z."
)
