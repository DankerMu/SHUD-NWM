"""Constants shared by the precipitation raster service and the layer catalog.

Deliberately dependency-free (stdlib only): ``services/tiles/mvt.py`` imports the
catalog-facing constants from here, so importing this module must never pull in
numpy or netCDF4.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

# Read-only display-side env naming the node-27 view of the NFS tree node-22
# writes under NHMS_OBJECT_STORE_COPYBACK_ROOT. The display API must never read
# that compute-side variable: `apps/api/runtime_mode.py` forbids its presence
# for the display_readonly role outright.
MIRROR_ROOT_ENV = "NHMS_PRECIP_MIRROR_ROOT"
# The same directory tree the MVT tile cache uses, so #2011 retention prunes one
# root. Kept equal to `services.tiles.mvt.MVT_FILE_CACHE_DIR_ENV` by a test.
FILE_CACHE_DIR_ENV = "NHMS_MVT_FILE_CACHE_DIR"

PRECIP_VARIABLE = "prcp_rate_or_amount"
SLICE_UNIT = "mm/day"
PRECIP_UNIT = "mm/24h"
PRECIP_WINDOW_HOURS = 24
PRECIP_STEP_HOURS = 3
PRECIP_WINDOW_SLICES = PRECIP_WINDOW_HOURS // PRECIP_STEP_HOURS
PRECIP_FORECAST_HORIZON_HOURS = 168

# The route segment is validated against this closed set BEFORE any
# normalization or filesystem access (`normalize_source_id` would happily accept
# `ERA5`).
PRECIP_ROUTE_SOURCES = ("gfs", "ifs")
GRID_IDS = {"gfs": "gfs_0p25", "IFS": "ifs_0p25"}
GRID_SCHEMA_VERSION = "nhms.grid_definition.v1"
GRID_LAYOUT = "rectilinear"
GRID_AXIS_ORDER = ("latitude", "longitude")
MAX_GRID_DEFINITION_BYTES = 4 * 1024 * 1024

# Static bbox of the canonical 0.25 deg product (lon_min, lat_min, lon_max,
# lat_max). Used by the DB-side layer catalog, which must not touch the mirror;
# the index and the renderer derive theirs from the mirrored grid.json.
PRECIP_BOUNDS = (63.0, 8.0, 145.0, 64.0)
PRECIP_BOUNDS_CRS = "EPSG:4326"
OUTPUT_WIDTH = 1316  # 4 x 329 grid columns

PRECIP_LAYER_ID = "precip"
PRECIP_IMAGE_URL_TEMPLATE = "/api/v1/precip/{source}/{cycle}/{valid_time}.png"
PRECIP_INDEX_URL_TEMPLATE = "/api/v1/precip/{source}/{cycle}/index"
PRECIP_REQUIRED_PLACEHOLDERS = ("source", "cycle", "valid_time")


@dataclass(frozen=True)
class Palette:
    """CMA 24h six-class scale: PLTE index 0 is transparent, 1-6 are the classes."""

    colors: tuple[str, ...]
    thresholds: tuple[float, ...]
    labels: tuple[str, ...]

    @property
    def version(self) -> str:
        """Changes whenever any hex value or threshold changes (spec requirement)."""
        payload = json.dumps(
            {"colors": list(self.colors), "thresholds": list(self.thresholds)},
            separators=(",", ":"),
        ).encode("utf-8")
        return f"cma24h6-{hashlib.sha256(payload).hexdigest()[:8]}"

    @property
    def legend(self) -> tuple[dict[str, Any], ...]:
        entries: list[dict[str, Any]] = []
        for index, threshold in enumerate(self.thresholds):
            upper = self.thresholds[index + 1] if index + 1 < len(self.thresholds) else None
            entries.append(
                {
                    "min": threshold,
                    "max": upper,
                    "color": self.colors[index],
                    "label": self.labels[index],
                }
            )
        return tuple(entries)

    def rgb_bytes(self) -> bytes:
        return b"".join(bytes.fromhex(color.lstrip("#")) for color in self.colors)


PALETTE_COLORS = ("#A6F28F", "#3DBA3D", "#61B8FF", "#0000FF", "#FA00FA", "#800040")
PALETTE_THRESHOLDS = (0.1, 10.0, 25.0, 50.0, 100.0, 250.0)
PALETTE_LABELS = ("0.1–10", "10–25", "25–50", "50–100", "100–250", "≥250")

PRECIP_PALETTE = Palette(colors=PALETTE_COLORS, thresholds=PALETTE_THRESHOLDS, labels=PALETTE_LABELS)
PALETTE_VERSION = PRECIP_PALETTE.version
PRECIP_LEGEND = PRECIP_PALETTE.legend
