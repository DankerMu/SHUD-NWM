"""Leaf types for the Basins geometry parser (split from ``basins_geometry``, #3081).

Holds the error type, the immutable carriers and the two pure helpers that
both ``basins_geometry`` and ``basins_geometry_shapes`` need. Imports nothing
from the parser modules so the dependency graph stays acyclic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BasinsGeometryError(RuntimeError):
    """Raised when Basins GIS or SHUD evidence cannot be parsed safely."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        path: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.path = path
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error_code": self.error_code, "message": str(self)}
        if self.path is not None:
            payload["path"] = self.path
        payload.update(self.details)
        return payload


@dataclass(frozen=True)
class RiverSegmentGeometry:
    river_segment_id: str
    segment_order: int
    downstream_segment_id: str | None
    length_m: float | None
    geom_wkt: str
    properties: dict[str, Any]


@dataclass(frozen=True)
class CrosswalkRow:
    """Single segment record extracted from ``gis/seg.shp`` for the crosswalk.

    Pure attribute carrier; no geometry. ``segment_order`` is the 0-indexed row
    offset within the source shapefile (matches ``pyshp.Reader``'s natural
    record iteration order). ``length_m`` is read from the ``Length`` dbf field
    when present and is ``None`` otherwise (qhh's ``seg.shp`` has only
    ``iRiv`` / ``iEle`` fields and therefore yields ``None``).
    """

    iRiv: int
    iEle: int
    segment_order: int
    length_m: float | None


@dataclass(frozen=True)
class _LayerSnapshot:
    base: Path
    data: dict[str, bytes]
    digests: dict[str, str]


@dataclass(frozen=True)
class _CoordinateTransform:
    source_name: str
    projection_method: str | None
    transformer: Any | None
    source_is_projected: bool


@dataclass(frozen=True)
class TrustedBasinsRoot:
    path: Path
    resolved_path: Path
    identity: tuple[int, int, int]


@dataclass
class _GisReadBudget:
    total_bytes: int = 0


def _check_resource_limit(count: int, limit: int, resource: str, path: str) -> None:
    if count > limit:
        raise BasinsGeometryError(
            "BASINS_REGISTRY_RESOURCE_LIMIT_EXCEEDED",
            f"Basins GIS {resource} exceeded import limit.",
            path=path,
            details={"resource": resource, "count": count, "limit": limit},
        )


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, int | float | str | bool) or value is None:
        return value
    return str(value)
