"""Shared geographic bounding-box config for download spatial clipping.

A single source of truth for the "China mainland + 10 degree buffer" rectangle
used to clip GFS (server-side NOMADS subregion) and IFS (local cdo) downloads.
The bbox is folded into download product identity so a region change does not
collide with previously cached cycles.

Longitude convention: -180..180 (leftlon may be negative). The default China
region uses positive values (west=63, east=145). Keep env overrides in the
-180..180 style so NOMADS (GFS server-side subregion) and cdo (sellonlatbox for
IFS) clip the same area; mixing in 0..360-style longitudes can silently produce
mismatched regions across the two backends.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# China mainland body (18-54N / 73-135E) buffered outward by 10 degrees.
DEFAULT_BBOX_SOUTH = 8.0
DEFAULT_BBOX_NORTH = 64.0
DEFAULT_BBOX_WEST = 63.0
DEFAULT_BBOX_EAST = 145.0


@dataclass(frozen=True)
class GeoBBox:
    """Geographic bounding box.

    Longitudes use the -180..180 convention (west/east may be negative).
    Validation tolerates [-180, 360] for robustness, but -180..180 is the
    recommended/canonical form to keep GFS and IFS clipping consistent.
    """

    south: float
    north: float
    west: float
    east: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.south <= 90.0 or not -90.0 <= self.north <= 90.0:
            raise ValueError(f"Latitude out of range [-90, 90]: south={self.south}, north={self.north}")
        if not -180.0 <= self.west <= 360.0 or not -180.0 <= self.east <= 360.0:
            raise ValueError(f"Longitude out of range [-180, 360]: west={self.west}, east={self.east}")
        if self.south >= self.north:
            raise ValueError(f"south ({self.south}) must be < north ({self.north})")
        if self.west >= self.east:
            raise ValueError(f"west ({self.west}) must be < east ({self.east})")

    def as_dict(self) -> dict[str, float]:
        return {"south": self.south, "north": self.north, "west": self.west, "east": self.east}

    def identity(self) -> str:
        """Stable identity string for folding into source identity."""
        return f"bbox:s{self.south:g}:n{self.north:g}:w{self.west:g}:e{self.east:g}"


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def china_buffered_bbox_from_env() -> GeoBBox:
    return GeoBBox(
        south=_env_float("NHMS_DOWNLOAD_BBOX_SOUTH", DEFAULT_BBOX_SOUTH),
        north=_env_float("NHMS_DOWNLOAD_BBOX_NORTH", DEFAULT_BBOX_NORTH),
        west=_env_float("NHMS_DOWNLOAD_BBOX_WEST", DEFAULT_BBOX_WEST),
        east=_env_float("NHMS_DOWNLOAD_BBOX_EAST", DEFAULT_BBOX_EAST),
    )
