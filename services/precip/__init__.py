"""Past-24h precipitation raster service (display-v2 I8, #2010).

`(source, cycle, valid_time)` maps by exactly two pinned rules onto one set of
mirrored canonical slices that depends only on cycles at or before the requested
one; the same slice set yields the same PNG bytes, the same cache file and the
same ETag.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from services.precip.cache import (
    cache_file_path,
    cache_root,
    precip_etag,
    read_cached_png,
    tmp_suffix,
    write_cached_png,
)
from services.precip.constants import (
    FILE_CACHE_DIR_ENV,
    GRID_IDS,
    MIRROR_ROOT_ENV,
    OUTPUT_WIDTH,
    PALETTE_VERSION,
    PRECIP_BOUNDS,
    PRECIP_BOUNDS_CRS,
    PRECIP_FORECAST_HORIZON_HOURS,
    PRECIP_IMAGE_URL_TEMPLATE,
    PRECIP_INDEX_URL_TEMPLATE,
    PRECIP_LEGEND,
    PRECIP_PALETTE,
    PRECIP_REQUIRED_PLACEHOLDERS,
    PRECIP_STEP_HOURS,
    PRECIP_UNIT,
    PRECIP_WINDOW_HOURS,
    Palette,
)
from services.precip.errors import (
    PrecipCycleNotMirrored,
    PrecipError,
    PrecipSliceInvalid,
    PrecipWindowIncomplete,
)
from services.precip.mirror import (
    Slice,
    cycle_is_mirrored,
    cycle_token,
    discover_mirrored_cycles,
    grid_id_for,
    grid_object_key,
    horizon_valid_times,
    precip_directory_key,
    resolve_window,
    slice_digest,
    slice_object_key,
    storage_source_for,
    window_end_times,
)

if TYPE_CHECKING:  # pragma: no cover - typing/pyflakes only, never imported at runtime
    from services.precip.field import GridDefinition, accumulate_24h, load_grid
    from services.precip.render import (
        classify,
        encode_indexed_png,
        image_size,
        mercator_y,
        render_png,
        resample,
        row_for_latitude,
    )

# `field` and `render` pull in numpy and netCDF4 (~46 ms and two shared
# libraries). `services/tiles/mvt.py` imports only the stdlib-only constants
# module from this package, but importing a submodule executes this __init__
# first -- so an eager `from services.precip.field import ...` here would drag
# the whole array stack into every display request that touches the layer
# catalog. PEP 562 keeps the names re-exported without importing the modules
# until one of them is actually read (pinned decision 15; the boundary is
# asserted by a subprocess `sys.modules` test).
_LAZY_EXPORTS = {
    "GridDefinition": "services.precip.field",
    "accumulate_24h": "services.precip.field",
    "load_grid": "services.precip.field",
    "classify": "services.precip.render",
    "encode_indexed_png": "services.precip.render",
    "image_size": "services.precip.render",
    "mercator_y": "services.precip.render",
    "render_png": "services.precip.render",
    "resample": "services.precip.render",
    "row_for_latitude": "services.precip.render",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value  # bind once; later reads skip this hook entirely
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "FILE_CACHE_DIR_ENV",
    "GRID_IDS",
    "GridDefinition",
    "MIRROR_ROOT_ENV",
    "OUTPUT_WIDTH",
    "PALETTE_VERSION",
    "PRECIP_BOUNDS",
    "PRECIP_BOUNDS_CRS",
    "PRECIP_FORECAST_HORIZON_HOURS",
    "PRECIP_IMAGE_URL_TEMPLATE",
    "PRECIP_INDEX_URL_TEMPLATE",
    "PRECIP_LEGEND",
    "PRECIP_PALETTE",
    "PRECIP_REQUIRED_PLACEHOLDERS",
    "PRECIP_STEP_HOURS",
    "PRECIP_UNIT",
    "PRECIP_WINDOW_HOURS",
    "Palette",
    "PrecipCycleNotMirrored",
    "PrecipError",
    "PrecipSliceInvalid",
    "PrecipWindowIncomplete",
    "Slice",
    "accumulate_24h",
    "cache_file_path",
    "cache_root",
    "classify",
    "cycle_is_mirrored",
    "cycle_token",
    "discover_mirrored_cycles",
    "encode_indexed_png",
    "grid_id_for",
    "grid_object_key",
    "horizon_valid_times",
    "image_size",
    "load_grid",
    "mercator_y",
    "precip_directory_key",
    "precip_etag",
    "read_cached_png",
    "render_png",
    "resample",
    "resolve_window",
    "row_for_latitude",
    "slice_digest",
    "slice_object_key",
    "storage_source_for",
    "tmp_suffix",
    "window_end_times",
    "write_cached_png",
]
