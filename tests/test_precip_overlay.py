"""display-v2 I8 (#2010): precipitation raster service.

Seams under test (design.md "Sketch seams under test"):

- the three pure functions of ``services/precip`` (``resolve_window`` /
  ``accumulate_24h`` / ``render_png``) against tmp mirror roots holding small
  synthetic grids written in the live canonical layout, and
- the two public routes through ``TestClient`` — they take no DB dependency, so
  no ``dependency_overrides`` entry is needed for them.

The real 225x329 grid is used only where the contract fixes an output number
(PNG width 1316, its Mercator height, the 36 deg N row); every window/route case
uses a 4x5 grid so the fixture writes stay cheap.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import pathlib
import struct
import threading
import time
import zlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import netCDF4
import numpy as np
import pytest
import yaml
from fastapi.testclient import TestClient

from apps.api import main
from apps.api.routes import hydro_display
from apps.api.routes import precip as precip_routes
from services.precip import (
    GridDefinition,
    Palette,
    PrecipSliceInvalid,
    PrecipWindowIncomplete,
    accumulate_24h,
    cache_file_path,
    classify,
    discover_mirrored_cycles,
    image_size,
    load_grid,
    mercator_y,
    render_png,
    resolve_window,
    row_for_latitude,
    slice_digest,
)
from services.precip import cache as precip_cache
from services.precip.constants import (
    FILE_CACHE_DIR_ENV,
    MIRROR_ROOT_ENV,
    OUTPUT_WIDTH,
    PALETTE_THRESHOLDS,
    PALETTE_VERSION,
    PRECIP_BOUNDS,
    PRECIP_LEGEND,
    PRECIP_PALETTE,
)
from services.tiles.mvt import MVT_FILE_CACHE_DIR_ENV, layer_metadata

REPO_ROOT = Path(__file__).resolve().parents[1]

CYCLE_0902_12 = datetime(2026, 9, 2, 12, tzinfo=UTC)
CYCLE_0902_00 = datetime(2026, 9, 2, 0, tzinfo=UTC)
CYCLE_0901_12 = datetime(2026, 9, 1, 12, tzinfo=UTC)
CYCLE_0901_00 = datetime(2026, 9, 1, 0, tzinfo=UTC)
CYCLE_0903_00 = datetime(2026, 9, 3, 0, tzinfo=UTC)

FULL_LEADS = tuple(range(3, 169, 3))
WINDOW_LEADS = tuple(range(3, 25, 3))

SMALL_LATITUDES = (40.0, 39.75, 39.5, 39.25)
SMALL_LONGITUDES = (116.0, 116.25, 116.5, 116.75, 117.0)
SMALL_SHAPE = (len(SMALL_LATITUDES), len(SMALL_LONGITUDES))


# --------------------------------------------------------------------------
# fixture builders
# --------------------------------------------------------------------------


def _cycle_token(cycle: datetime) -> str:
    return cycle.strftime("%Y%m%d%H")


def _slice_key(storage_source: str, cycle: datetime, lead: int) -> str:
    token = _cycle_token(cycle)
    return (
        f"canonical/{storage_source}/{token}/prcp_rate_or_amount/"
        f"{storage_source}_{token}_prcp_rate_or_amount_f{lead:03d}.nc"
    )


def _write_slice(
    root: Path,
    *,
    storage_source: str,
    cycle: datetime,
    lead: int,
    values: Sequence[float] | np.ndarray | None = None,
    unit: str = "mm/day",
    point_count: int | None = None,
) -> Path:
    """One canonical slice in the live layout: 1-D ``point`` var + global attrs."""
    path = root / _slice_key(storage_source, cycle, lead)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = point_count if point_count is not None else SMALL_SHAPE[0] * SMALL_SHAPE[1]
    payload = np.full(count, float(lead), dtype="float64") if values is None else np.asarray(values, dtype="float64")
    dataset = netCDF4.Dataset(str(path), "w", format="NETCDF4")
    try:
        dataset.createDimension("point", payload.size)
        variable = dataset.createVariable(
            "prcp_rate_or_amount", "f8", ("point",), fill_value=float("nan")
        )
        variable[:] = payload
        dataset.cycle_time = cycle.strftime("%Y-%m-%dT%H:%M:%SZ")
        dataset.valid_time = (cycle + timedelta(hours=lead)).strftime("%Y-%m-%dT%H:%M:%SZ")
        dataset.lead_time_hours = lead
        dataset.unit = unit
        dataset.grid_id = "gfs_0p25" if storage_source == "gfs" else "ifs_0p25"
        dataset.lineage_json = "{}"
    finally:
        dataset.close()
    return path


def _write_grid(
    root: Path,
    *,
    storage_source: str,
    grid_id: str | None = None,
    latitudes: Sequence[float] = SMALL_LATITUDES,
    longitudes: Sequence[float] = SMALL_LONGITUDES,
    payload: dict[str, Any] | None = None,
) -> Path:
    resolved_grid_id = grid_id or ("gfs_0p25" if storage_source == "gfs" else "ifs_0p25")
    path = root / f"canonical/{storage_source}/grid/{resolved_grid_id}/grid.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    body = payload if payload is not None else {
        "schema_version": "nhms.grid_definition.v1",
        "grid_id": resolved_grid_id,
        "layout": "rectilinear",
        "axis_order": ["latitude", "longitude"],
        "shape": [len(latitudes), len(longitudes)],
        "latitudes": [float(value) for value in latitudes],
        "longitudes": [float(value) for value in longitudes],
    }
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _write_cycle(
    root: Path,
    *,
    storage_source: str,
    cycle: datetime,
    leads: Sequence[int] = WINDOW_LEADS,
    skip: Sequence[int] = (),
) -> None:
    (root / f"canonical/{storage_source}/{_cycle_token(cycle)}/prcp_rate_or_amount").mkdir(
        parents=True, exist_ok=True
    )
    for lead in leads:
        if lead in skip:
            continue
        _write_slice(root, storage_source=storage_source, cycle=cycle, lead=lead)


def _real_grid() -> GridDefinition:
    """The live 225x329 rectilinear grid (63-145E, 8-64N, latitudes descending)."""
    return GridDefinition(
        grid_id="gfs_0p25",
        shape=(225, 329),
        latitudes=np.linspace(64.0, 8.0, 225),
        longitudes=np.linspace(63.0, 145.0, 329),
        axis_order=("latitude", "longitude"),
    )


def _small_grid() -> GridDefinition:
    return GridDefinition(
        grid_id="gfs_0p25",
        shape=SMALL_SHAPE,
        latitudes=np.asarray(SMALL_LATITUDES, dtype="float64"),
        longitudes=np.asarray(SMALL_LONGITUDES, dtype="float64"),
        axis_order=("latitude", "longitude"),
    )


class _FsCounter:
    """Counts filesystem probes below ``root``; scoped, never left global."""

    def __init__(self, monkeypatch_context: Any, root: Path) -> None:
        self.root = str(root)
        self.paths: list[str] = []
        real_stat = os.stat
        real_listdir = os.listdir
        real_is_dir = pathlib.Path.is_dir
        real_is_file = pathlib.Path.is_file
        real_exists = pathlib.Path.exists

        def _record(target: Any) -> None:
            text = str(target)
            if text.startswith(self.root):
                self.paths.append(text)

        def _stat(target: Any, *args: Any, **kwargs: Any) -> Any:
            _record(target)
            return real_stat(target, *args, **kwargs)

        def _listdir(target: Any = ".") -> Any:
            _record(target)
            return real_listdir(target)

        def _path_is_dir(this: pathlib.Path, *args: Any, **kwargs: Any) -> Any:
            _record(this)
            return real_is_dir(this, *args, **kwargs)

        def _path_is_file(this: pathlib.Path, *args: Any, **kwargs: Any) -> Any:
            _record(this)
            return real_is_file(this, *args, **kwargs)

        def _path_exists(this: pathlib.Path, *args: Any, **kwargs: Any) -> Any:
            _record(this)
            return real_exists(this, *args, **kwargs)

        monkeypatch_context.setattr(os, "stat", _stat)
        monkeypatch_context.setattr(os, "listdir", _listdir)
        monkeypatch_context.setattr(pathlib.Path, "is_dir", _path_is_dir)
        monkeypatch_context.setattr(pathlib.Path, "is_file", _path_is_file)
        monkeypatch_context.setattr(pathlib.Path, "exists", _path_exists)

    def touched(self, fragment: str) -> bool:
        return any(fragment in path for path in self.paths)


def _precip_client(
    monkeypatch: pytest.MonkeyPatch,
    mirror_root: Path | None,
    cache_root: Path | None = None,
) -> TestClient:
    """A TestClient for the DB-free precip routes; no dependency override needed."""
    if mirror_root is None:
        monkeypatch.delenv(MIRROR_ROOT_ENV, raising=False)
    else:
        monkeypatch.setenv(MIRROR_ROOT_ENV, str(mirror_root))
    if cache_root is None:
        monkeypatch.delenv(FILE_CACHE_DIR_ENV, raising=False)
    else:
        monkeypatch.setenv(FILE_CACHE_DIR_ENV, str(cache_root))
    return TestClient(main.create_app(), raise_server_exceptions=False)


def _png_chunks(data: bytes) -> list[tuple[str, bytes]]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks: list[tuple[str, bytes]] = []
    offset = 8
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        tag = data[offset + 4 : offset + 8].decode("ascii")
        body = data[offset + 8 : offset + 8 + length]
        crc = data[offset + 8 + length : offset + 12 + length]
        assert struct.unpack(">I", crc)[0] == zlib.crc32(tag.encode("ascii") + body)
        chunks.append((tag, body))
        offset += 12 + length
    return chunks


def _png_chunk(data: bytes, tag: str) -> bytes:
    return next(body for name, body in _png_chunks(data) if name == tag)


def _png_pixels(data: bytes) -> np.ndarray:
    width, height, depth, colour_type = struct.unpack(">IIBB", _png_chunk(data, "IHDR")[:10])
    assert (depth, colour_type) == (8, 3)
    idat = b"".join(body for name, body in _png_chunks(data) if name == "IDAT")
    raw = zlib.decompress(idat)
    assert len(raw) == height * (1 + width)
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(height, 1 + width)
    assert not rows[:, 0].any(), "every scanline must use filter type 0"
    return rows[:, 1:]


# --------------------------------------------------------------------------
# 5.1 window resolution
# --------------------------------------------------------------------------


def test_lead_zero_window_resolves_from_prior_cycles(tmp_path: Path) -> None:
    """Invariant row 1: the lead-0 window is served entirely by earlier cycles."""
    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0902_00, leads=FULL_LEADS)
    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0901_12, leads=FULL_LEADS)

    slices = resolve_window("gfs", CYCLE_0902_12, CYCLE_0902_12, tmp_path)

    assert [(item.cycle, item.lead_hours) for item in slices] == [
        (CYCLE_0901_12, 3),
        (CYCLE_0901_12, 6),
        (CYCLE_0901_12, 9),
        (CYCLE_0901_12, 12),
        (CYCLE_0902_00, 3),
        (CYCLE_0902_00, 6),
        (CYCLE_0902_00, 9),
        (CYCLE_0902_00, 12),
    ]
    assert len(slices) == 8
    assert all(item.cycle != CYCLE_0902_12 for item in slices)
    # GFS ships no f000; every resolved lead is >= 3h, so its absence is no gap.
    assert min(item.lead_hours for item in slices) >= 3


def test_window_inside_forecast_horizon_uses_the_requested_cycle(tmp_path: Path) -> None:
    """Invariant row 2: valid_time = cycle + 48h resolves to f027...f048."""
    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0902_12, leads=FULL_LEADS)

    slices = resolve_window("gfs", CYCLE_0902_12, CYCLE_0902_12 + timedelta(hours=48), tmp_path)

    assert [item.lead_hours for item in slices] == [27, 30, 33, 36, 39, 42, 45, 48]
    assert {item.cycle for item in slices} == {CYCLE_0902_12}


def test_newer_mirrored_cycles_are_never_borrowed_from(tmp_path: Path) -> None:
    """Invariant row 3: the requested cycle is the upper bound on slice cycles."""
    for cycle in (CYCLE_0901_00, CYCLE_0901_12, CYCLE_0902_00, CYCLE_0902_12):
        _write_cycle(tmp_path, storage_source="gfs", cycle=cycle, leads=FULL_LEADS)

    slices = resolve_window("gfs", CYCLE_0901_12, CYCLE_0902_12, tmp_path)

    assert {item.cycle for item in slices} == {CYCLE_0901_12}
    assert [item.lead_hours for item in slices] == [3, 6, 9, 12, 15, 18, 21, 24]
    # Without the upper bound an unbounded "most recent C <= T-3h" rule would
    # have taken 2026-09-02T00:00:00Z for every end time at or after 03Z.
    assert not any(_cycle_token(CYCLE_0902_00) in item.object_key for item in slices)
    assert not any(_cycle_token(CYCLE_0902_12) in item.object_key for item in slices)


def test_mirroring_a_newer_cycle_leaves_the_slice_set_and_cache_key_unchanged(tmp_path: Path) -> None:
    for cycle in (CYCLE_0901_00, CYCLE_0901_12, CYCLE_0902_00, CYCLE_0902_12):
        _write_cycle(tmp_path, storage_source="gfs", cycle=cycle, leads=FULL_LEADS)
    before = resolve_window("gfs", CYCLE_0901_12, CYCLE_0902_12, tmp_path)

    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0903_00, leads=FULL_LEADS)
    after = resolve_window("gfs", CYCLE_0901_12, CYCLE_0902_12, tmp_path)

    assert [item.object_key for item in after] == [item.object_key for item in before]
    assert slice_digest(after) == slice_digest(before)


def test_a_missing_lead_never_falls_back_to_an_older_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant row 4: the selected cycle's gap fails closed, it is not patched."""
    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0902_00, leads=FULL_LEADS, skip=(9,))
    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0901_12, leads=FULL_LEADS)
    # The route-level gate would otherwise answer PRECIP_CYCLE_NOT_MIRRORED first.
    (tmp_path / f"canonical/gfs/{_cycle_token(CYCLE_0902_12)}/prcp_rate_or_amount").mkdir(parents=True)

    with monkeypatch.context() as patch:
        counter = _FsCounter(patch, tmp_path)
        with pytest.raises(PrecipWindowIncomplete) as excinfo:
            resolve_window("gfs", CYCLE_0902_12, CYCLE_0902_12, tmp_path)

    assert excinfo.value.missing == (
        "canonical/gfs/2026090200/prcp_rate_or_amount/gfs_2026090200_prcp_rate_or_amount_f009.nc"
    )
    # The fallback lead of the older cycle is never even stat-ed.
    assert not counter.touched("gfs_2026090112_prcp_rate_or_amount_f021.nc")


def test_no_mirrored_cycle_before_a_window_end_time(tmp_path: Path) -> None:
    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0902_12, leads=FULL_LEADS)

    with pytest.raises(PrecipWindowIncomplete) as excinfo:
        resolve_window("gfs", CYCLE_0902_12, CYCLE_0902_12, tmp_path)

    assert excinfo.value.missing is None
    assert excinfo.value.reason == "no_mirrored_cycle_before_window_end"
    assert excinfo.value.window_end == CYCLE_0902_12 - timedelta(hours=21)


def test_resolve_window_rejects_a_non_enum_source_before_touching_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as patch:
        counter = _FsCounter(patch, tmp_path)
        for source in ("ERA5", "best", "compare", "GFS"):
            with pytest.raises(ValueError):
                resolve_window(source, CYCLE_0902_12, CYCLE_0902_12, tmp_path)
    assert counter.paths == []


def test_ifs_route_source_resolves_to_the_upper_case_mirror_directory(tmp_path: Path) -> None:
    _write_cycle(tmp_path, storage_source="IFS", cycle=CYCLE_0902_12, leads=FULL_LEADS)
    _write_grid(tmp_path, storage_source="IFS")

    slices = resolve_window("ifs", CYCLE_0902_12, CYCLE_0902_12 + timedelta(hours=24), tmp_path)

    assert slices[0].object_key == (
        "canonical/IFS/2026090212/prcp_rate_or_amount/IFS_2026090212_prcp_rate_or_amount_f003.nc"
    )
    assert slices[0].path == tmp_path / slices[0].object_key
    grid = load_grid(tmp_path, "IFS")
    assert grid.grid_id == "ifs_0p25"


def test_gfs_route_source_keeps_the_lower_case_mirror_directory(tmp_path: Path) -> None:
    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0902_12, leads=FULL_LEADS)
    _write_grid(tmp_path, storage_source="gfs")

    slices = resolve_window("gfs", CYCLE_0902_12, CYCLE_0902_12 + timedelta(hours=24), tmp_path)

    assert slices[0].object_key == (
        "canonical/gfs/2026090212/prcp_rate_or_amount/gfs_2026090212_prcp_rate_or_amount_f003.nc"
    )
    assert load_grid(tmp_path, "gfs").grid_id == "gfs_0p25"


def test_cycle_discovery_ignores_non_cycle_directories(tmp_path: Path) -> None:
    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0902_00, leads=(3,))
    (tmp_path / "canonical/gfs/grid/gfs_0p25").mkdir(parents=True)
    (tmp_path / "canonical/gfs/2026093199/prcp_rate_or_amount").mkdir(parents=True)
    (tmp_path / "canonical/gfs/202609020").mkdir(parents=True)
    (tmp_path / "canonical/gfs/2026090100").mkdir(parents=True)  # no prcp tree -> not mirrored

    assert discover_mirrored_cycles(tmp_path, "gfs") == (CYCLE_0902_00,)


# --------------------------------------------------------------------------
# 5.1 accumulation
# --------------------------------------------------------------------------


def test_accumulate_24h_sums_rate_times_three_over_twentyfour(tmp_path: Path) -> None:
    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0902_12, leads=FULL_LEADS)
    slices = resolve_window("gfs", CYCLE_0902_12, CYCLE_0902_12 + timedelta(hours=24), tmp_path)

    field = accumulate_24h(slices, _small_grid())

    # Each fixture slice carries its lead hour as the mm/day rate.
    expected = sum(lead * 3.0 / 24.0 for lead in (3, 6, 9, 12, 15, 18, 21, 24))
    assert field.shape == SMALL_SHAPE
    assert np.allclose(field, expected)


def test_accumulate_24h_propagates_nan_without_zero_filling(tmp_path: Path) -> None:
    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0902_12, leads=FULL_LEADS)
    holed = np.full(SMALL_SHAPE[0] * SMALL_SHAPE[1], 6.0)
    holed[0] = np.nan
    _write_slice(tmp_path, storage_source="gfs", cycle=CYCLE_0902_12, lead=12, values=holed)
    slices = resolve_window("gfs", CYCLE_0902_12, CYCLE_0902_12 + timedelta(hours=24), tmp_path)

    field = accumulate_24h(slices, _small_grid())

    assert math.isnan(field[0, 0])
    assert not np.isnan(field[0, 1])


def test_accumulate_24h_rejects_a_slice_whose_unit_is_not_mm_per_day(tmp_path: Path) -> None:
    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0902_12, leads=FULL_LEADS)
    _write_slice(tmp_path, storage_source="gfs", cycle=CYCLE_0902_12, lead=9, unit="kg m-2 s-1")
    slices = resolve_window("gfs", CYCLE_0902_12, CYCLE_0902_12 + timedelta(hours=24), tmp_path)

    with pytest.raises(PrecipSliceInvalid) as excinfo:
        accumulate_24h(slices, _small_grid())

    assert excinfo.value.reason == "slice_unit_mismatch"
    assert excinfo.value.object_key.endswith("gfs_2026090212_prcp_rate_or_amount_f009.nc")
    assert str(tmp_path) not in str(excinfo.value.object_key)


def test_accumulate_24h_rejects_a_slice_whose_point_count_disagrees_with_the_grid(tmp_path: Path) -> None:
    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0902_12, leads=FULL_LEADS)
    _write_slice(tmp_path, storage_source="gfs", cycle=CYCLE_0902_12, lead=6, point_count=7)
    slices = resolve_window("gfs", CYCLE_0902_12, CYCLE_0902_12 + timedelta(hours=24), tmp_path)

    with pytest.raises(PrecipSliceInvalid) as excinfo:
        accumulate_24h(slices, _small_grid())

    assert excinfo.value.reason == "slice_point_count_mismatch"


# --------------------------------------------------------------------------
# 5.1 grid definition
# --------------------------------------------------------------------------


def test_load_grid_rejects_a_grid_definition_over_four_megabytes(tmp_path: Path) -> None:
    path = _write_grid(tmp_path, storage_source="gfs")
    padding = {
        "schema_version": "nhms.grid_definition.v1",
        "grid_id": "gfs_0p25",
        "layout": "rectilinear",
        "axis_order": ["latitude", "longitude"],
        "shape": [4, 5],
        "latitudes": list(SMALL_LATITUDES),
        "longitudes": list(SMALL_LONGITUDES),
        "padding": "x" * (4 * 1024 * 1024 + 16),
    }
    path.write_text(json.dumps(padding), encoding="utf-8")

    with pytest.raises(PrecipSliceInvalid) as excinfo:
        load_grid(tmp_path, "gfs")

    assert excinfo.value.reason == "grid_definition_too_large"
    assert excinfo.value.object_key == "canonical/gfs/grid/gfs_0p25/grid.json"


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ({"schema_version": "nhms.grid_definition.v2"}, "grid_definition_schema_unsupported"),
        ({"layout": "curvilinear"}, "grid_definition_layout_unsupported"),
        ({"axis_order": ["longitude", "latitude"]}, "grid_definition_axis_order_unsupported"),
        ({"shape": [5, 5]}, "grid_definition_shape_mismatch"),
        ({"latitudes": [40.0, 40.0, 39.5, 39.25]}, "grid_definition_axis_not_monotonic"),
    ],
)
def test_load_grid_rejects_a_definition_that_is_not_a_v1_rectilinear_grid(
    tmp_path: Path, mutation: dict[str, Any], reason: str
) -> None:
    body = {
        "schema_version": "nhms.grid_definition.v1",
        "grid_id": "gfs_0p25",
        "layout": "rectilinear",
        "axis_order": ["latitude", "longitude"],
        "shape": [4, 5],
        "latitudes": list(SMALL_LATITUDES),
        "longitudes": list(SMALL_LONGITUDES),
    }
    body.update(mutation)
    _write_grid(tmp_path, storage_source="gfs", payload=body)

    with pytest.raises(PrecipSliceInvalid) as excinfo:
        load_grid(tmp_path, "gfs")

    assert excinfo.value.reason == reason


def test_load_grid_reports_an_absent_definition_by_object_key(tmp_path: Path) -> None:
    with pytest.raises(PrecipSliceInvalid) as excinfo:
        load_grid(tmp_path, "IFS")

    assert excinfo.value.object_key == "canonical/IFS/grid/ifs_0p25/grid.json"
    assert excinfo.value.reason == "grid_definition_missing"


def test_load_grid_reads_the_live_layout(tmp_path: Path) -> None:
    _write_grid(tmp_path, storage_source="gfs")

    grid = load_grid(tmp_path, "gfs")

    assert grid.shape == SMALL_SHAPE
    assert grid.axis_order == ("latitude", "longitude")
    assert grid.bounds == (116.0, 39.25, 117.0, 40.0)


# --------------------------------------------------------------------------
# 5.1 rendering
# --------------------------------------------------------------------------


def test_render_png_has_the_pinned_structure_and_palette() -> None:
    grid = _real_grid()
    field = np.zeros(grid.shape, dtype="float64")
    field[10:20, 10:20] = 30.0

    data = render_png(field, grid, PRECIP_PALETTE)

    width, height, depth, colour_type, compression, filter_method, interlace = struct.unpack(
        ">IIBBBBB", _png_chunk(data, "IHDR")
    )
    assert (width, depth, colour_type) == (OUTPUT_WIDTH, 8, 3)
    assert OUTPUT_WIDTH == 1316
    assert (compression, filter_method, interlace) == (0, 0, 0)
    assert height == image_size(grid)[1] == 1219
    plte = _png_chunk(data, "PLTE")
    assert len(plte) == 7 * 3
    assert plte[3:].hex().upper() == "A6F28F3DBA3D61B8FF0000FFFA00FA800040"
    trns = _png_chunk(data, "tRNS")
    assert trns[0] == 0
    assert [name for name, _ in _png_chunks(data)][0] == "IHDR"
    assert [name for name, _ in _png_chunks(data)][-1] == "IEND"
    assert _png_pixels(data).shape == (1219, 1316)


def test_render_png_is_deterministic() -> None:
    grid = _small_grid()
    rng = np.random.default_rng(2010)
    field = rng.uniform(0.0, 300.0, size=grid.shape)

    assert render_png(field, grid, PRECIP_PALETTE) == render_png(field, grid, PRECIP_PALETTE)


def test_classification_bins_are_lower_inclusive() -> None:
    values = np.asarray(
        [0.0, 0.09999, 0.1, 9.999, 10.0, 24.999, 25.0, 49.999, 50.0, 99.999, 100.0, 249.999, 250.0, 1000.0, np.nan]
    )

    assert classify(values, PRECIP_PALETTE).tolist() == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 0]
    assert list(PALETTE_THRESHOLDS) == [0.1, 10.0, 25.0, 50.0, 100.0, 250.0]


@pytest.mark.parametrize("value", [0.1, 10.0, 25.0, 50.0, 100.0, 250.0])
def test_a_threshold_value_paints_the_class_whose_lower_bound_it_is(value: float) -> None:
    grid = _small_grid()
    field = np.full(grid.shape, value, dtype="float64")

    pixels = _png_pixels(render_png(field, grid, PRECIP_PALETTE))

    expected = int(np.searchsorted(np.asarray(PALETTE_THRESHOLDS), value, side="right"))
    assert set(np.unique(pixels).tolist()) == {expected}


def test_below_threshold_and_nan_render_as_the_transparent_index() -> None:
    grid = _small_grid()
    for field in (np.full(grid.shape, 0.09), np.full(grid.shape, np.nan)):
        pixels = _png_pixels(render_png(field, grid, PRECIP_PALETTE))
        assert set(np.unique(pixels).tolist()) == {0}


def test_the_thirty_six_north_band_lands_on_the_mercator_row() -> None:
    grid = _real_grid()
    height = image_size(grid)[1]
    field = np.zeros(grid.shape, dtype="float64")
    row_of_36n = int(np.argmin(np.abs(grid.latitudes - 36.0)))
    assert grid.latitudes[row_of_36n] == pytest.approx(36.0)
    field[row_of_36n, :] = 30.0

    # Formula recomputed here, independently of the renderer.
    def _y(lat_deg: float) -> float:
        return math.log(math.tan(math.pi / 4 + math.radians(lat_deg) / 2))

    fraction_from_south = (_y(36.0) - _y(8.0)) / (_y(64.0) - _y(8.0))
    expected_row = int(round((1.0 - fraction_from_south) * height - 0.5))

    assert abs(row_for_latitude(36.0, grid, height) - expected_row) <= 1

    pixels = _png_pixels(render_png(field, grid, PRECIP_PALETTE))
    painted_rows = np.flatnonzero(pixels.any(axis=1))
    # Bilinear sampling smears the band by one 0.25 deg grid step either side
    # (~5 output rows at 36 deg N); nothing outside that footprint is painted.
    smear = 6
    assert painted_rows.min() >= expected_row - smear
    assert painted_rows.max() <= expected_row + smear
    assert pixels[expected_row].any()


def test_palette_version_changes_with_any_hex_or_threshold() -> None:
    recoloured = Palette(
        colors=("#A6F28E",) + PRECIP_PALETTE.colors[1:],
        thresholds=PRECIP_PALETTE.thresholds,
        labels=PRECIP_PALETTE.labels,
    )
    rethresholded = Palette(
        colors=PRECIP_PALETTE.colors,
        thresholds=(0.2,) + PRECIP_PALETTE.thresholds[1:],
        labels=PRECIP_PALETTE.labels,
    )

    assert recoloured.version != PALETTE_VERSION
    assert rethresholded.version != PALETTE_VERSION
    assert PRECIP_PALETTE.version == PALETTE_VERSION


def test_mercator_row_mapping_is_monotonic_from_north_to_south() -> None:
    grid = _real_grid()
    height = image_size(grid)[1]

    rows = [row_for_latitude(lat, grid, height) for lat in (64.0, 50.0, 36.0, 20.0, 8.0)]

    assert rows == sorted(rows)
    assert rows[0] == 0
    assert rows[-1] == height - 1
    assert mercator_y(0.0) == pytest.approx(0.0)


# --------------------------------------------------------------------------
# 5.2 / 5.3 PNG route: cache, headers, errors
# --------------------------------------------------------------------------


def _complete_ifs_mirror(root: Path) -> None:
    """Three consecutive IFS cycles, so a lead-3h window resolves completely."""
    for cycle in (CYCLE_0901_12, CYCLE_0902_00, CYCLE_0902_12):
        _write_cycle(root, storage_source="IFS", cycle=cycle, leads=FULL_LEADS)
    _write_grid(root, storage_source="IFS")


def _solo_ifs_mirror(root: Path) -> None:
    """Only the requested cycle is mirrored (the oldest-retained-cycle shape)."""
    _write_cycle(root, storage_source="IFS", cycle=CYCLE_0902_12, leads=FULL_LEADS)
    _write_grid(root, storage_source="IFS")


def _complete_gfs_mirror(root: Path, *, cycles: Sequence[datetime] = (CYCLE_0901_12, CYCLE_0902_12)) -> None:
    for cycle in cycles:
        _write_cycle(root, storage_source="gfs", cycle=cycle, leads=FULL_LEADS)
    _write_grid(root, storage_source="gfs")


def test_png_route_renders_caches_and_reuses_the_mirror_directory_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = tmp_path / "mirror"
    cache = tmp_path / "cache"
    _complete_ifs_mirror(mirror)
    client = _precip_client(monkeypatch, mirror, cache)

    cold = client.get("/api/v1/precip/ifs/2026-09-02T12:00:00Z/2026-09-02T15:00:00Z.png")

    assert cold.status_code == 200, cold.text
    assert cold.headers["content-type"] == "image/png"
    assert cold.headers["cache-control"] == "public, max-age=300"
    assert cold.headers["x-tile-cache"] == "miss"
    assert cold.headers["etag"].startswith('W/"precip-')
    assert cold.content[:8] == b"\x89PNG\r\n\x1a\n"

    slices = resolve_window("ifs", CYCLE_0902_12, datetime(2026, 9, 2, 15, tzinfo=UTC), mirror)
    digest = slice_digest(slices)
    written = sorted((cache / "precip" / "IFS" / "2026090212").iterdir())
    assert [path.name for path in written] == [f"2026-09-02T15:00:00Z.{PALETTE_VERSION}.{digest}.png"]
    assert written[0].read_bytes() == cold.content

    warm = client.get("/api/v1/precip/ifs/2026-09-02T12:00:00Z/2026-09-02T15:00:00Z.png")
    assert warm.status_code == 200
    assert warm.headers["x-tile-cache"] == "hit"
    assert warm.content == cold.content
    assert warm.headers["etag"] == cold.headers["etag"]


def test_cache_hit_does_not_open_netcdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mirror = tmp_path / "mirror"
    cache = tmp_path / "cache"
    _complete_ifs_mirror(mirror)
    client = _precip_client(monkeypatch, mirror, cache)
    url = "/api/v1/precip/ifs/2026-09-02T12:00:00Z/2026-09-02T15:00:00Z.png"
    assert client.get(url).status_code == 200

    opened: list[str] = []

    def _forbidden(*args: Any, **kwargs: Any) -> Any:
        opened.append(str(args[0] if args else kwargs))
        raise AssertionError("cache hit must not open NetCDF")

    monkeypatch.setattr(netCDF4, "Dataset", _forbidden)
    warm = client.get(url)

    assert warm.status_code == 200
    assert warm.headers["x-tile-cache"] == "hit"
    assert opened == []


def test_if_none_match_returns_304_without_reading_the_cache_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = tmp_path / "mirror"
    cache = tmp_path / "cache"
    _complete_ifs_mirror(mirror)
    client = _precip_client(monkeypatch, mirror, cache)
    url = "/api/v1/precip/ifs/2026-09-02T12:00:00Z/2026-09-02T15:00:00Z.png"
    warm_up = client.get(url)
    etag = warm_up.headers["etag"]

    real_read_bytes = pathlib.Path.read_bytes
    reads: list[str] = []

    def _counting_read_bytes(this: pathlib.Path) -> bytes:
        if str(this).startswith(str(cache)):
            reads.append(str(this))
        return real_read_bytes(this)

    monkeypatch.setattr(pathlib.Path, "read_bytes", _counting_read_bytes)
    response = client.get(url, headers={"If-None-Match": etag})

    assert response.status_code == 304
    assert response.headers["etag"] == etag
    assert response.headers["cache-control"] == "public, max-age=300"
    assert response.content == b""
    assert reads == []


def test_three_spellings_of_one_valid_time_share_one_cache_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = tmp_path / "mirror"
    cache = tmp_path / "cache"
    _complete_ifs_mirror(mirror)
    client = _precip_client(monkeypatch, mirror, cache)
    spellings = [
        "2026-09-02T15:00:00.000Z",
        "2026-09-02T15:00:00%2B00:00",
        "2026-09-02T15:00:00Z",
    ]

    statuses = []
    etags = set()
    for spelling in spellings:
        response = client.get(f"/api/v1/precip/ifs/2026-09-02T12:00:00Z/{spelling}.png")
        statuses.append((response.status_code, response.headers.get("x-tile-cache")))
        etags.add(response.headers["etag"])

    assert statuses == [(200, "miss"), (200, "hit"), (200, "hit")]
    assert len(etags) == 1
    files = list((cache / "precip" / "IFS" / "2026090212").iterdir())
    assert len(files) == 1
    assert files[0].name.startswith("2026-09-02T15:00:00Z.")


def test_a_late_intermediate_cycle_writes_a_new_cache_file_and_a_new_etag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = tmp_path / "mirror"
    cache = tmp_path / "cache"
    _complete_gfs_mirror(mirror, cycles=(CYCLE_0901_12, CYCLE_0902_12))
    client = _precip_client(monkeypatch, mirror, cache)
    url = "/api/v1/precip/gfs/2026-09-02T12:00:00Z/2026-09-02T12:00:00Z.png"

    first = client.get(url)
    assert first.status_code == 200, first.text
    before = resolve_window("gfs", CYCLE_0902_12, CYCLE_0902_12, mirror)
    assert {item.cycle for item in before} == {CYCLE_0901_12}
    first_file = next((cache / "precip" / "gfs" / "2026090212").iterdir())

    _write_cycle(mirror, storage_source="gfs", cycle=CYCLE_0902_00, leads=FULL_LEADS)
    second = client.get(url)

    assert second.status_code == 200, second.text
    assert second.headers["x-tile-cache"] == "miss"
    assert second.headers["etag"] != first.headers["etag"]
    after = resolve_window("gfs", CYCLE_0902_12, CYCLE_0902_12, mirror)
    assert [item.lead_hours for item in after[4:]] == [3, 6, 9, 12]
    assert {item.cycle for item in after[4:]} == {CYCLE_0902_00}
    files = sorted(path.name for path in (cache / "precip" / "gfs" / "2026090212").iterdir())
    assert len(files) == 2
    assert first_file.exists()
    assert slice_digest(before) != slice_digest(after)


def test_concurrent_window_reads_never_open_two_netcdf_datasets_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HDF5 is not thread-safe here and FastAPI runs `def` handlers in a threadpool.

    Unserialized, two concurrent window reads inside one worker raise
    `RuntimeError: NetCDF: Not a valid ID` (an HTTP 500). The reader therefore
    serializes every open/read/close process-wide; this asserts the property
    directly instead of waiting for the race to show up.
    """
    _write_cycle(tmp_path, storage_source="gfs", cycle=CYCLE_0902_12, leads=FULL_LEADS)
    slices = resolve_window("gfs", CYCLE_0902_12, CYCLE_0902_12 + timedelta(hours=24), tmp_path)
    grid = _small_grid()
    real_dataset = netCDF4.Dataset
    state = {"depth": 0, "peak": 0}
    guard = threading.Lock()

    def _tracking(*args: Any, **kwargs: Any) -> Any:
        with guard:
            state["depth"] += 1
            state["peak"] = max(state["peak"], state["depth"])
        try:
            time.sleep(0.005)
            return real_dataset(*args, **kwargs)
        finally:
            with guard:
                state["depth"] -= 1

    monkeypatch.setattr(netCDF4, "Dataset", _tracking)
    failures: list[BaseException] = []

    def _read() -> None:
        try:
            for _ in range(3):
                accumulate_24h(slices, grid)
        except BaseException as exc:  # noqa: BLE001 - reported through the assertion below
            failures.append(exc)

    threads = [threading.Thread(target=_read) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == []
    assert state["peak"] == 1


def test_two_cold_writers_leave_exactly_one_cache_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mirror = tmp_path / "mirror"
    cache = tmp_path / "cache"
    _complete_ifs_mirror(mirror)
    monkeypatch.setenv(MIRROR_ROOT_ENV, str(mirror))
    monkeypatch.setenv(FILE_CACHE_DIR_ENV, str(cache))
    # Two threads of one worker share a pid, so the pid-suffixed tmp names would
    # collide; distinct suffixes model two worker processes racing.
    suffixes = itertools.count(1)
    monkeypatch.setattr(precip_cache, "tmp_suffix", lambda: f"w{next(suffixes)}")
    # Both writers are cold regardless of thread timing: the race under test is
    # the two concurrent WRITES, not which one happened to read first.
    monkeypatch.setattr(precip_cache, "read_cached_png", lambda _path: None)

    app = main.create_app()
    url = "/api/v1/precip/ifs/2026-09-02T12:00:00Z/2026-09-02T15:00:00Z.png"
    results: dict[int, Any] = {}
    barrier = threading.Barrier(2)

    def _fetch(index: int) -> None:
        client = TestClient(app, raise_server_exceptions=False)
        barrier.wait(timeout=30)
        results[index] = client.get(url)

    threads = [threading.Thread(target=_fetch, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert set(results) == {0, 1}
    assert results[0].status_code == 200, results[0].text
    assert results[1].status_code == 200, results[1].text
    assert results[0].content == results[1].content
    directory = cache / "precip" / "IFS" / "2026090212"
    names = sorted(path.name for path in directory.iterdir())
    assert len(names) == 1, names
    assert not any(name.endswith(".tmp") for name in names)
    assert (directory / names[0]).read_bytes() == results[0].content


def test_a_cache_write_failure_still_serves_the_png(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mirror = tmp_path / "mirror"
    cache = tmp_path / "cache"
    _complete_ifs_mirror(mirror)
    cache.mkdir()
    # A regular file where the cache tree must be: every write below it raises
    # OSError. The PNG is still served; only the caching is skipped.
    (cache / "precip").write_text("not a directory", encoding="utf-8")
    client = _precip_client(monkeypatch, mirror, cache)

    response = client.get("/api/v1/precip/ifs/2026-09-02T12:00:00Z/2026-09-02T15:00:00Z.png")

    assert response.status_code == 200, response.text
    assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert response.headers["x-tile-cache"] == "miss"
    assert (cache / "precip").is_file()


def test_no_cache_directory_configured_still_renders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mirror = tmp_path / "mirror"
    _complete_ifs_mirror(mirror)
    client = _precip_client(monkeypatch, mirror, None)

    response = client.get("/api/v1/precip/ifs/2026-09-02T12:00:00Z/2026-09-02T15:00:00Z.png")

    assert response.status_code == 200, response.text
    assert response.headers["x-tile-cache"] == "miss"


def test_cache_file_path_mirrors_the_canonical_cycle_directory(tmp_path: Path) -> None:
    path = cache_file_path(
        tmp_path, storage_source="IFS", cycle_token="2026090212",
        valid_time="2026-09-02T15:00:00Z", palette_version="pv1", digest="abc123abc123",
    )

    assert path == tmp_path / "precip/IFS/2026090212/2026-09-02T15:00:00Z.pv1.abc123abc123.png"
    assert path.parent.relative_to(tmp_path).as_posix().endswith("IFS/2026090212")


def test_precip_file_cache_env_is_the_mvt_file_cache_env() -> None:
    """One env var, one directory tree; retention (#2011) prunes by that name."""
    assert FILE_CACHE_DIR_ENV == MVT_FILE_CACHE_DIR_ENV == "NHMS_MVT_FILE_CACHE_DIR"


# --------------------------------------------------------------------------
# 5.3 route errors
# --------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["ERA5", "best", "compare", "GFS"])
def test_a_non_enum_source_is_422_before_normalization_or_any_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    mirror = tmp_path / "mirror"
    _complete_gfs_mirror(mirror)
    client = _precip_client(monkeypatch, mirror, tmp_path / "cache")
    normalizations: list[str] = []

    def _counting_normalize(value: str) -> str:
        normalizations.append(value)
        raise AssertionError("normalize_source_id must not run for a non-enum source")

    monkeypatch.setattr(precip_routes, "normalize_source_id", _counting_normalize)

    with monkeypatch.context() as patch:
        counter = _FsCounter(patch, mirror)
        index = client.get(f"/api/v1/precip/{source}/2026-09-02T12:00:00Z/index")
        png = client.get(f"/api/v1/precip/{source}/2026-09-02T12:00:00Z/2026-09-02T12:00:00Z.png")

    assert index.status_code == 422, index.text
    assert png.status_code == 422, png.text
    assert normalizations == []
    assert counter.paths == []


def test_both_routes_404_when_the_mirror_root_is_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _precip_client(monkeypatch, None, tmp_path / "cache")

    with monkeypatch.context() as patch:
        counter = _FsCounter(patch, tmp_path)
        index = client.get("/api/v1/precip/gfs/2026-09-02T12:00:00Z/index")
        png = client.get("/api/v1/precip/gfs/2026-09-02T12:00:00Z/2026-09-02T12:00:00Z.png")

    for response in (index, png):
        assert response.status_code == 404, response.text
        body = response.json()
        assert body["error"]["code"] == "PRECIP_CYCLE_NOT_MIRRORED"
        assert body["error"]["details"]["reason"] == "mirror_root_unconfigured"
    assert counter.paths == []


def test_an_empty_mirror_root_is_treated_as_unconfigured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MIRROR_ROOT_ENV, "   ")
    monkeypatch.setenv(FILE_CACHE_DIR_ENV, str(tmp_path / "cache"))
    client = TestClient(main.create_app(), raise_server_exceptions=False)

    response = client.get("/api/v1/precip/gfs/2026-09-02T12:00:00Z/index")

    assert response.status_code == 404, response.text
    assert response.json()["error"]["details"]["reason"] == "mirror_root_unconfigured"


def test_both_routes_404_before_any_slice_lookup_when_the_cycle_is_not_mirrored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older cycles could cover the whole window; the gate answers first anyway."""
    mirror = tmp_path / "mirror"
    cache = tmp_path / "cache"
    _write_cycle(mirror, storage_source="gfs", cycle=CYCLE_0902_00, leads=FULL_LEADS)
    _write_cycle(mirror, storage_source="gfs", cycle=CYCLE_0901_12, leads=FULL_LEADS)
    _write_grid(mirror, storage_source="gfs")
    client = _precip_client(monkeypatch, mirror, cache)

    with monkeypatch.context() as patch:
        counter = _FsCounter(patch, mirror)
        index = client.get("/api/v1/precip/gfs/2026-09-02T12:00:00Z/index")
        png = client.get("/api/v1/precip/gfs/2026-09-02T12:00:00Z/2026-09-02T12:00:00Z.png")

    for response in (index, png):
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "PRECIP_CYCLE_NOT_MIRRORED"
    assert not counter.touched("prcp_rate_or_amount/gfs_2026090200")
    assert not counter.touched("prcp_rate_or_amount/gfs_2026090112")
    assert not cache.exists()


def test_png_route_404s_for_an_incomplete_window_and_caches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = tmp_path / "mirror"
    cache = tmp_path / "cache"
    _write_cycle(mirror, storage_source="gfs", cycle=CYCLE_0902_00, leads=FULL_LEADS, skip=(9,))
    _write_cycle(mirror, storage_source="gfs", cycle=CYCLE_0901_12, leads=FULL_LEADS)
    (mirror / "canonical/gfs/2026090212/prcp_rate_or_amount").mkdir(parents=True)
    _write_grid(mirror, storage_source="gfs")
    client = _precip_client(monkeypatch, mirror, cache)

    response = client.get("/api/v1/precip/gfs/2026-09-02T12:00:00Z/2026-09-02T12:00:00Z.png")

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "PRECIP_WINDOW_INCOMPLETE"
    assert error["details"]["missing"] == (
        "canonical/gfs/2026090200/prcp_rate_or_amount/gfs_2026090200_prcp_rate_or_amount_f009.nc"
    )
    assert str(mirror) not in response.text
    assert not cache.exists()


def test_png_route_404s_when_no_mirrored_cycle_precedes_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = tmp_path / "mirror"
    _write_cycle(mirror, storage_source="gfs", cycle=CYCLE_0902_12, leads=FULL_LEADS)
    _write_grid(mirror, storage_source="gfs")
    client = _precip_client(monkeypatch, mirror, tmp_path / "cache")

    response = client.get("/api/v1/precip/gfs/2026-09-02T12:00:00Z/2026-09-02T12:00:00Z.png")

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "PRECIP_WINDOW_INCOMPLETE"
    assert error["details"]["reason"] == "no_mirrored_cycle_before_window_end"
    assert error["details"]["window_end"] == "2026-09-01T15:00:00Z"


def test_png_route_404s_on_an_invalid_grid_definition_rather_than_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = tmp_path / "mirror"
    _complete_ifs_mirror(mirror)
    _write_grid(mirror, storage_source="IFS", payload={"schema_version": "nhms.grid_definition.v1", "layout": "mesh"})
    client = _precip_client(monkeypatch, mirror, tmp_path / "cache")

    response = client.get("/api/v1/precip/ifs/2026-09-02T12:00:00Z/2026-09-02T15:00:00Z.png")

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "PRECIP_WINDOW_INCOMPLETE"
    assert error["details"]["reason"] == "grid_definition_layout_unsupported"
    assert error["details"]["object_key"] == "canonical/IFS/grid/ifs_0p25/grid.json"


def test_a_sub_second_instant_is_rejected_with_422(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mirror = tmp_path / "mirror"
    _complete_ifs_mirror(mirror)
    client = _precip_client(monkeypatch, mirror, tmp_path / "cache")

    response = client.get("/api/v1/precip/ifs/2026-09-02T12:00:00Z/2026-09-02T15:00:00.500Z.png")

    assert response.status_code == 422, response.text


def test_precip_routes_take_no_database_dependency() -> None:
    """The routes are DB-free: a bad request must not open a session."""
    paths = {
        "/api/v1/precip/{source}/{cycle}/index",
        "/api/v1/precip/{source}/{cycle}/{valid_time}.png",
    }
    for route in main.app.routes:
        if getattr(route, "path", None) in paths:
            names = {dependency.call for dependency in route.dependant.dependencies}
            assert hydro_display.get_hydro_display_session not in names


# --------------------------------------------------------------------------
# 5.3 index
# --------------------------------------------------------------------------


def test_index_lists_only_windows_that_resolve_completely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mirror = tmp_path / "mirror"
    _solo_ifs_mirror(mirror)
    client = _precip_client(monkeypatch, mirror, tmp_path / "cache")

    response = client.get("/api/v1/precip/ifs/2026-09-02T12:00:00Z/index")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["source"] == "ifs"
    assert data["cycle"] == "2026-09-02T12:00:00Z"
    assert data["window_hours"] == 24
    assert data["unit"] == "mm/24h"
    assert data["palette_version"] == PALETTE_VERSION
    assert data["bounds"] == [116.0, 39.25, 117.0, 40.0]
    assert data["image_size"] == list(image_size(_small_grid()))
    # Only the requested cycle is mirrored, so the first complete window is the
    # one whose earliest end time is the cycle + 3h: lead 24h, then every 3h step
    # through +168h.
    assert data["valid_times"][0] == "2026-09-03T12:00:00Z"
    assert data["valid_times"][-1] == "2026-09-09T12:00:00Z"
    assert len(data["valid_times"]) == 49
    assert all(instant.endswith("Z") and "." not in instant for instant in data["valid_times"])


def test_index_reads_no_netcdf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mirror = tmp_path / "mirror"
    _solo_ifs_mirror(mirror)
    client = _precip_client(monkeypatch, mirror, tmp_path / "cache")
    monkeypatch.setattr(
        netCDF4, "Dataset", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("index must not read NetCDF"))
    )

    response = client.get("/api/v1/precip/ifs/2026-09-02T12:00:00Z/index")

    assert response.status_code == 200, response.text


def test_index_legend_matches_the_layers_catalog_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mirror = tmp_path / "mirror"
    _solo_ifs_mirror(mirror)
    client = _precip_client(monkeypatch, mirror, tmp_path / "cache")

    legend = client.get("/api/v1/precip/ifs/2026-09-02T12:00:00Z/index").json()["data"]["legend"]

    assert legend == layer_metadata("precip")["legend"]
    assert [entry["color"] for entry in legend] == [
        "#A6F28F", "#3DBA3D", "#61B8FF", "#0000FF", "#FA00FA", "#800040"
    ]
    assert [entry["min"] for entry in legend] == [0.1, 10.0, 25.0, 50.0, 100.0, 250.0]
    assert [entry["max"] for entry in legend] == [10.0, 25.0, 50.0, 100.0, 250.0, None]
    assert legend[0]["label"] == "0.1–10"
    assert legend[-1]["label"] == "≥250"
    assert legend == [dict(entry) for entry in PRECIP_LEGEND]


def test_index_404s_for_an_unmirrored_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mirror = tmp_path / "mirror"
    _solo_ifs_mirror(mirror)
    client = _precip_client(monkeypatch, mirror, tmp_path / "cache")

    response = client.get("/api/v1/precip/ifs/2026-09-01T12:00:00Z/index")

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "PRECIP_CYCLE_NOT_MIRRORED"


def test_index_404s_on_an_invalid_grid_definition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mirror = tmp_path / "mirror"
    _solo_ifs_mirror(mirror)
    (mirror / "canonical/IFS/grid/ifs_0p25/grid.json").unlink()
    client = _precip_client(monkeypatch, mirror, tmp_path / "cache")

    response = client.get("/api/v1/precip/ifs/2026-09-02T12:00:00Z/index")

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "PRECIP_WINDOW_INCOMPLETE"
    assert response.json()["error"]["details"]["reason"] == "grid_definition_missing"


# --------------------------------------------------------------------------
# 5.4 catalog
# --------------------------------------------------------------------------


class _FakeValidTimes:
    valid_times = ["2026-09-02T12:00:00Z"]
    limit = 24
    observed_count = 1
    truncated = False


def _catalog_app(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(hydro_display, "display_ready_run", lambda _s: {"run_id": "run_latest"})
    monkeypatch.setattr(hydro_display, "_require_display_ready", lambda _s, run_id: {"run_id": run_id})
    monkeypatch.setattr(hydro_display, "_run_source_version", lambda _run: "run-source-v1")
    monkeypatch.setattr(hydro_display, "_require_run_source_identity", lambda _run, layer_id: ("bv_a", "rnv_a"))
    monkeypatch.setattr(hydro_display, "_river_network_source_version", lambda _s, _b: "river-source-v1")
    monkeypatch.setattr(hydro_display, "national_river_network_source_version", lambda _s: "river-national-v1")
    monkeypatch.setattr(hydro_display, "national_discharge_source_version", lambda _s, **_k: "national-hydro-v1")
    monkeypatch.setattr(hydro_display, "national_discharge_valid_times", lambda _s, **_k: _FakeValidTimes())
    monkeypatch.setattr(
        hydro_display,
        "national_discharge_cycles",
        lambda _s, **_k: {"source": "gfs", "cycles": [], "default_cycle": "2026-09-02T00:00:00Z"},
    )
    monkeypatch.setattr(hydro_display, "_mvt_live_postgis_enabled", lambda _s: False)
    monkeypatch.setattr(hydro_display, "display_catalog_cached", lambda _request, _key, load: load())
    app = main.create_app()
    app.dependency_overrides[hydro_display.get_hydro_display_session] = lambda: object()
    return app


def _entry(items: list[dict[str, Any]], layer_id: str) -> dict[str, Any]:
    return next(item for item in items if item["layer_id"] == layer_id)


def test_layers_catalog_carries_the_precip_entry_identically_runless_and_run_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _catalog_app(monkeypatch)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            runless = client.get("/api/v1/layers")
            run_scoped = client.get("/api/v1/layers", params={"run_id": "run_other"})
    finally:
        app.dependency_overrides.clear()

    assert runless.status_code == 200, runless.text
    assert run_scoped.status_code == 200, run_scoped.text
    entry = _entry(runless.json()["data"], "precip")
    assert entry == _entry(run_scoped.json()["data"], "precip")
    assert entry["layer_name"] == "Precipitation (past 24h)"
    assert entry["layer_type"] == "meteorology"
    assert entry["variables"] == ["precip_24h"]

    metadata = entry["metadata"]
    assert metadata["tile_format"] == "png"
    assert metadata["image_url_template"] == "/api/v1/precip/{source}/{cycle}/{valid_time}.png"
    assert metadata["index_url_template"] == "/api/v1/precip/{source}/{cycle}/index"
    assert metadata["required_placeholders"] == ["source", "cycle", "valid_time"]
    assert metadata["bounds"] == list(PRECIP_BOUNDS)
    assert metadata["bounds_crs"] == "EPSG:4326"
    assert metadata["window_hours"] == 24
    assert metadata["unit"] == "mm/24h"
    assert metadata["palette_version"] == PALETTE_VERSION
    # The catalog's swatches and the precip index's come from one constant.
    assert metadata["legend"] == [dict(entry) for entry in PRECIP_LEGEND]
    assert metadata["valid_times"] == []
    assert metadata["valid_time_observed_count"] == 0
    assert metadata["valid_times_truncated"] is False
    assert metadata["fallback_available"] is False
    assert metadata["release_blocking"] is False
    assert "tile_url_template" not in metadata

    # Unchanged siblings: the other three entries keep their MVT shape.
    for layer_id in ("discharge", "river-network", "met-stations"):
        assert _entry(runless.json()["data"], layer_id)["metadata"]["tile_format"] in {
            "mvt",
            "geojson_compatibility",
        }


def test_precip_entry_metadata_is_independent_of_run_and_postgis_readiness() -> None:
    baseline = layer_metadata("precip")

    assert layer_metadata("precip", run_id="run_a", release_blocking=True, national=True) == baseline
    assert baseline["release_blocking"] is False
    assert baseline["layer_id"] == "precip"


def test_precip_is_a_supported_public_layer_with_an_empty_valid_time_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "precip" in hydro_display.SUPPORTED_PUBLIC_LAYER_IDS
    app = _catalog_app(monkeypatch)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            empty = client.get("/api/v1/layers/precip/valid-times")
            scoped = client.get(
                "/api/v1/layers/precip/valid-times",
                params={"source": "gfs", "cycle": "2026-09-02T12:00:00Z"},
            )
    finally:
        app.dependency_overrides.clear()

    assert empty.status_code == 200, empty.text
    assert empty.json()["data"]["valid_times"] == []
    # Source/cycle discovery stays discharge-only.
    assert scoped.status_code == 422, scoped.text


# --------------------------------------------------------------------------
# 5.6 OpenAPI
# --------------------------------------------------------------------------


def _static_spec() -> dict[str, Any]:
    return yaml.safe_load((REPO_ROOT / "openapi" / "nhms.v1.yaml").read_text(encoding="utf-8"))


def test_static_openapi_documents_both_precip_routes_with_the_two_404_codes() -> None:
    spec = _static_spec()
    index = spec["paths"]["/api/v1/precip/{source}/{cycle}/index"]["get"]
    png = spec["paths"]["/api/v1/precip/{source}/{cycle}/{valid_time}.png"]["get"]

    for operation in (index, png):
        source = next(item for item in operation["parameters"] if item["name"] == "source")
        assert source["schema"]["enum"] == ["gfs", "ifs"]
        assert next(item for item in operation["parameters"] if item["name"] == "cycle")["schema"]["format"] == (
            "date-time"
        )
        assert "422" in operation["responses"]
        codes = operation["responses"]["404"]["content"]["application/json"]["schema"]["properties"]["error"][
            "properties"
        ]["code"]["enum"]
        assert codes == ["PRECIP_CYCLE_NOT_MIRRORED", "PRECIP_WINDOW_INCOMPLETE"]

    assert next(item for item in png["parameters"] if item["name"] == "valid_time")["schema"]["format"] == (
        "date-time"
    )
    assert png["responses"]["200"]["content"]["image/png"]["schema"]["format"] == "binary"
    assert set(png["responses"]["200"]["headers"]) == {"Cache-Control", "ETag", "X-Tile-Cache"}
    assert "304" in png["responses"]


def test_static_openapi_layer_metadata_carries_the_png_shape() -> None:
    metadata = _static_spec()["components"]["schemas"]["LayerMetadata"]["properties"]

    assert "png" in metadata["tile_format"]["enum"]
    for field in ("image_url_template", "index_url_template", "legend", "window_hours", "unit", "palette_version"):
        assert field in metadata, field
    assert metadata["legend"]["items"]["$ref"] == "#/components/schemas/PrecipLegendEntry"


def test_generated_frontend_types_expose_the_precip_index() -> None:
    types = (REPO_ROOT / "apps" / "frontend" / "src" / "api" / "types.ts").read_text(encoding="utf-8")

    assert "PrecipIndex:" in types
    assert "PrecipLegendEntry:" in types
    assert "/api/v1/precip/{source}/{cycle}/index" in types


# --------------------------------------------------------------------------
# 5.3a config example
# --------------------------------------------------------------------------


def test_display_example_documents_the_mirror_root_as_a_comment() -> None:
    text = (REPO_ROOT / "infra" / "env" / "display.example").read_text(encoding="utf-8")

    assert "# NHMS_PRECIP_MIRROR_ROOT=/home/ghdc/nwm/object-store" in text
    # A comment line is not read by `docker_runtime.parse_env_file`, so the
    # example stays a documentation hint rather than a configured value.
    assert f"\n{MIRROR_ROOT_ENV}=" not in text
    # The compute-side variable the display_readonly profile forbids is never
    # ASSIGNED here (it is only named in prose explaining why).
    assert "\nNHMS_OBJECT_STORE_COPYBACK_ROOT=" not in text


def test_display_code_never_reads_the_forbidden_copyback_root_env() -> None:
    for path in [
        REPO_ROOT / "apps" / "api" / "routes" / "precip.py",
        *(REPO_ROOT / "services" / "precip").glob("*.py"),
    ]:
        source = path.read_text(encoding="utf-8")
        # The quoted form is the only way the name could reach `os.getenv`;
        # naming it in prose (to explain why it is forbidden) is fine.
        assert '"NHMS_OBJECT_STORE_COPYBACK_ROOT"' not in source, path
        assert "'NHMS_OBJECT_STORE_COPYBACK_ROOT'" not in source, path


def test_etag_is_derived_from_the_identity_tuple(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mirror = tmp_path / "mirror"
    _complete_ifs_mirror(mirror)
    client = _precip_client(monkeypatch, mirror, tmp_path / "cache")

    response = client.get("/api/v1/precip/ifs/2026-09-02T12:00:00Z/2026-09-02T15:00:00Z.png")

    slices = resolve_window("ifs", CYCLE_0902_12, datetime(2026, 9, 2, 15, tzinfo=UTC), mirror)
    payload = json.dumps(
        [
            "ifs",
            "2026-09-02T12:00:00Z",
            "2026-09-02T15:00:00Z",
            PALETTE_VERSION,
            [item.object_key for item in slices],
        ],
        separators=(",", ":"),
    ).encode("utf-8")
    assert response.headers["etag"] == f'W/"precip-{hashlib.sha256(payload).hexdigest()}"'


