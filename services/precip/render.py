"""Web-Mercator resampling and a hand-written 8-bit palette PNG (numpy + zlib).

No Pillow: node-27's display venv has netCDF4 + numpy and nothing else, and this
change adds no dependency. The output is deterministic — the same field renders
to byte-identical PNGs, which is what lets two workers race on the file cache
without a lock.
"""

from __future__ import annotations

import math
import struct
import zlib

import numpy as np

from services.precip.constants import OUTPUT_WIDTH, PRECIP_PALETTE, Palette
from services.precip.field import GridDefinition

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ZLIB_LEVEL = 9


def mercator_y(latitude_degrees: float) -> float:
    """`y(phi) = ln(tan(pi/4 + phi/2))` — the spherical Web-Mercator ordinate."""
    return math.log(math.tan(math.pi / 4.0 + math.radians(latitude_degrees) / 2.0))


def _inverse_mercator_y(y: float | np.ndarray) -> np.ndarray:
    return np.degrees(2.0 * np.arctan(np.exp(y)) - math.pi / 2.0)


def image_size(grid: GridDefinition) -> tuple[int, int]:
    """Width 1316 px; height from the Mercator aspect ratio of the grid bbox."""
    lon_min, lat_min, lon_max, lat_max = grid.bounds
    span_y = mercator_y(lat_max) - mercator_y(lat_min)
    span_x = math.radians(lon_max - lon_min)
    height = int(round(OUTPUT_WIDTH * span_y / span_x))
    return (OUTPUT_WIDTH, max(1, height))


def row_for_latitude(latitude_degrees: float, grid: GridDefinition, height: int) -> int:
    """The output row whose pixel CENTRE is nearest `latitude_degrees`.

    Rows run north to south, so the Mercator-linear fraction of the spec
    (measured from the southern edge) is inverted here.
    """
    _, lat_min, _, lat_max = grid.bounds
    y_min = mercator_y(lat_min)
    y_max = mercator_y(lat_max)
    fraction_from_north = (y_max - mercator_y(latitude_degrees)) / (y_max - y_min)
    row = int(round(fraction_from_north * height - 0.5))
    return min(max(row, 0), height - 1)


def _sample_latitudes(grid: GridDefinition, height: int) -> np.ndarray:
    _, lat_min, _, lat_max = grid.bounds
    y_min = mercator_y(lat_min)
    y_max = mercator_y(lat_max)
    rows = np.arange(height, dtype="float64")
    ordinates = y_max - (rows + 0.5) * (y_max - y_min) / height
    return _inverse_mercator_y(ordinates)


def _sample_longitudes(grid: GridDefinition, width: int) -> np.ndarray:
    lon_min, _, lon_max, _ = grid.bounds
    columns = np.arange(width, dtype="float64")
    return lon_min + (columns + 0.5) * (lon_max - lon_min) / width


def resample(field: np.ndarray, grid: GridDefinition, size: tuple[int, int]) -> np.ndarray:
    """Bilinear sample of the rectilinear field at every output pixel centre."""
    width, height = size
    latitudes = np.asarray(grid.latitudes, dtype="float64")
    longitudes = np.asarray(grid.longitudes, dtype="float64")
    values = np.asarray(field, dtype="float64")
    # `np.interp` needs an increasing sample axis; the live grid's latitudes are
    # descending, so flip the axis AND the rows together.
    if latitudes[0] > latitudes[-1]:
        latitudes = latitudes[::-1]
        values = values[::-1, :]
    if longitudes[0] > longitudes[-1]:
        longitudes = longitudes[::-1]
        values = values[:, ::-1]

    row_positions = np.interp(_sample_latitudes(grid, height), latitudes, np.arange(latitudes.size, dtype="float64"))
    column_positions = np.interp(
        _sample_longitudes(grid, width), longitudes, np.arange(longitudes.size, dtype="float64")
    )
    row_low = np.clip(np.floor(row_positions).astype("int64"), 0, latitudes.size - 2)
    column_low = np.clip(np.floor(column_positions).astype("int64"), 0, longitudes.size - 2)
    row_weight = (row_positions - row_low)[:, None]
    column_weight = (column_positions - column_low)[None, :]

    top = values[row_low][:, column_low]
    top_right = values[row_low][:, column_low + 1]
    bottom = values[row_low + 1][:, column_low]
    bottom_right = values[row_low + 1][:, column_low + 1]
    upper = top * (1.0 - column_weight) + top_right * column_weight
    lower = bottom * (1.0 - column_weight) + bottom_right * column_weight
    return upper * (1.0 - row_weight) + lower * row_weight


def classify(values: np.ndarray, palette: Palette = PRECIP_PALETTE) -> np.ndarray:
    """Lower-inclusive bins -> palette indices 1..6; below the first bin or NaN -> 0."""
    data = np.asarray(values, dtype="float64")
    thresholds = np.asarray(palette.thresholds, dtype="float64")
    indices = np.searchsorted(thresholds, data, side="right").astype("uint8")
    # `searchsorted` puts NaN past the last bin; NaN is "no data", not "extreme".
    indices[np.isnan(data)] = 0
    return indices


def render_png(field: np.ndarray, grid: GridDefinition, palette: Palette = PRECIP_PALETTE) -> bytes:
    size = image_size(grid)
    indices = classify(resample(field, grid, size), palette)
    return encode_indexed_png(indices, palette)


def encode_indexed_png(indices: np.ndarray, palette: Palette = PRECIP_PALETTE) -> bytes:
    height, width = indices.shape
    header = struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0)
    plte = b"\x00\x00\x00" + palette.rgb_bytes()
    # One tRNS entry: index 0 fully transparent, every painted class opaque.
    trns = b"\x00"
    rows = np.zeros((height, width + 1), dtype="uint8")
    rows[:, 1:] = indices  # filter type 0 on every scanline
    idat = zlib.compress(rows.tobytes(), _ZLIB_LEVEL)
    return b"".join(
        [
            _PNG_SIGNATURE,
            _chunk(b"IHDR", header),
            _chunk(b"PLTE", plte),
            _chunk(b"tRNS", trns),
            _chunk(b"IDAT", idat),
            _chunk(b"IEND", b""),
        ]
    )


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))
