"""Grid definition loading and the 24h accumulation.

The mirrored slice is a NetCDF4 file with a single 1-D `prcp_rate_or_amount`
variable over dimension `point` (float64, `_FillValue = nan`, read back as a
masked array); the 2-D layout comes from `grid.json`'s `shape`/`axis_order`.
Nothing here hardcodes 225x329 — tests use small grids.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

import netCDF4
import numpy as np

from services.precip.constants import (
    GRID_AXIS_ORDER,
    GRID_LAYOUT,
    GRID_SCHEMA_VERSION,
    MAX_GRID_DEFINITION_BYTES,
    PRECIP_STEP_HOURS,
    PRECIP_VARIABLE,
    SLICE_UNIT,
)
from services.precip.errors import PrecipSliceInvalid
from services.precip.mirror import Slice, grid_id_for, grid_object_key


@dataclass(frozen=True)
class GridDefinition:
    """A rectilinear lat/lon grid; `latitudes` keeps the file's order (descending on the live grid)."""

    grid_id: str
    shape: tuple[int, int]
    latitudes: np.ndarray
    longitudes: np.ndarray
    axis_order: tuple[str, str]

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """(lon_min, lat_min, lon_max, lat_max) in EPSG:4326."""
        return (
            float(np.min(self.longitudes)),
            float(np.min(self.latitudes)),
            float(np.max(self.longitudes)),
            float(np.max(self.latitudes)),
        )

    @property
    def point_count(self) -> int:
        return int(self.shape[0]) * int(self.shape[1])


def load_grid(mirror_root: Path | str, storage_source: str, grid_id: str | None = None) -> GridDefinition:
    resolved_grid_id = grid_id or grid_id_for(storage_source)
    object_key = grid_object_key(storage_source, resolved_grid_id)
    path = Path(mirror_root) / object_key
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PrecipSliceInvalid(object_key, "grid_definition_missing") from exc
    if size > MAX_GRID_DEFINITION_BYTES:
        raise PrecipSliceInvalid(object_key, "grid_definition_too_large")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PrecipSliceInvalid(object_key, "grid_definition_unparseable") from exc
    if not isinstance(payload, dict):
        raise PrecipSliceInvalid(object_key, "grid_definition_unparseable")
    if payload.get("schema_version") != GRID_SCHEMA_VERSION:
        raise PrecipSliceInvalid(object_key, "grid_definition_schema_unsupported")
    if payload.get("layout") != GRID_LAYOUT:
        raise PrecipSliceInvalid(object_key, "grid_definition_layout_unsupported")
    if tuple(payload.get("axis_order") or ()) != GRID_AXIS_ORDER:
        raise PrecipSliceInvalid(object_key, "grid_definition_axis_order_unsupported")
    latitudes = _axis(object_key, payload.get("latitudes"))
    longitudes = _axis(object_key, payload.get("longitudes"))
    shape = payload.get("shape")
    if list(shape or ()) != [latitudes.size, longitudes.size]:
        raise PrecipSliceInvalid(object_key, "grid_definition_shape_mismatch")
    return GridDefinition(
        grid_id=str(payload.get("grid_id") or resolved_grid_id),
        shape=(latitudes.size, longitudes.size),
        latitudes=latitudes,
        longitudes=longitudes,
        axis_order=GRID_AXIS_ORDER,
    )


def _axis(object_key: str, values: object) -> np.ndarray:
    if not isinstance(values, list) or len(values) < 2:
        raise PrecipSliceInvalid(object_key, "grid_definition_axis_too_short")
    try:
        axis = np.asarray(values, dtype="float64")
    except (TypeError, ValueError) as exc:
        raise PrecipSliceInvalid(object_key, "grid_definition_axis_not_numeric") from exc
    if not np.all(np.isfinite(axis)):
        raise PrecipSliceInvalid(object_key, "grid_definition_axis_not_numeric")
    steps = np.diff(axis)
    if not (np.all(steps > 0.0) or np.all(steps < 0.0)):
        raise PrecipSliceInvalid(object_key, "grid_definition_axis_not_monotonic")
    return axis


# HDF5 under netCDF4 1.7.4 is not built thread-safe here, and FastAPI runs a
# `def` route handler in the anyio worker threadpool -- two concurrent PNG
# requests inside ONE uvicorn worker therefore opened two Datasets on the same
# thread-hostile library and raised `RuntimeError: NetCDF: Not a valid ID`
# (reproduced: 6 threads x 60 window reads). Every open/read/close below is
# serialized process-wide. This is an INTRA-process lock only: the file-cache
# race across workers is still lock-free by design (D4), because deterministic
# rendering makes both writers produce the same bytes.
_NETCDF_LOCK = threading.Lock()


def accumulate_24h(slices: list[Slice], grid: GridDefinition) -> np.ndarray:
    """Sum of `rate_i * 3/24` over the window, in mm/24h, shaped by the grid.

    NaN propagates: a masked cell in any slice leaves the accumulated cell NaN
    (rendered as the transparent palette index), never a silent zero.
    """
    total = np.zeros(grid.shape, dtype="float64")
    for item in slices:
        total += _slice_values(item, grid) * (PRECIP_STEP_HOURS / 24.0)
    return total


def _slice_values(item: Slice, grid: GridDefinition) -> np.ndarray:
    with _NETCDF_LOCK:
        try:
            dataset = netCDF4.Dataset(str(item.path), "r")
        except (OSError, RuntimeError) as exc:
            raise PrecipSliceInvalid(item.object_key, "slice_unreadable") from exc
        try:
            variable = dataset.variables.get(PRECIP_VARIABLE)
            if variable is None:
                raise PrecipSliceInvalid(item.object_key, "slice_variable_missing")
            unit = getattr(dataset, "unit", None)
            if unit != SLICE_UNIT:
                raise PrecipSliceInvalid(item.object_key, "slice_unit_mismatch")
            # netCDF4 hands back a masked array when `_FillValue` is set; fill the
            # mask with NaN rather than the fill value so the gap keeps propagating.
            values = np.ma.filled(variable[:], np.nan).astype("float64", copy=False).ravel()
        finally:
            dataset.close()
    if values.size != grid.point_count:
        raise PrecipSliceInvalid(item.object_key, "slice_point_count_mismatch")
    return values.reshape(grid.shape)
