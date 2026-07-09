"""Offline builder for the keliya integration fixture.

Regenerates the checked-in fixture files under this directory. The test
suite reads the checked-in files directly and never invokes this script;
it is kept alongside the fixture so any future edit to the design can be
reproduced deterministically.

Usage:
    uv run python tests/fixtures/mapping_builder/keliya/build.py

Generates:
- keliya.sp.mesh  (484 elements + node table sized to keep every triangle non-degenerate)
- keliya.sp.att   (484 rows with FORC in 1..32)
- keliya.tsd.forc (32 stations at 4 stations per target cell)
- gis/keliya.prj  (Albers WKT copied verbatim from keliya_minimal)

Design:
- Node grid: 23 lon x 12 lat = 276 nodes -> 22 x 11 quads = 484 triangles
  taken in row-major (lat-major) order. Non-square steps
  (NODE_STEP_X = 1600m, NODE_STEP_Y = 2000m) in the package Albers CRS keep
  every triangle non-degenerate (area 1.6e6 m^2, well above G1's 1e-6 m^2
  tolerance). Total mesh footprint is 35.2km x 22km, which spans exactly
  4 cells wide x 2 cells tall in the 0.1-deg WGS84 grid at lat 36N -> 8
  unique used cells under the nearest-cell mapping.
- Elements: all 484 triangles from the 22*11 quad grid (2 triangles per
  quad, top-left + bottom-right diagonal). Every triangle has three
  distinct vertex IDs and a positive planar area.
- Old FORC: striped 1..32 across the 484 rows (element_i -> (i%32)+1).
- Stations: 32 in .tsd.forc, at lon/lat centers of the 8 target cells,
  4 stations per cell (small +-0.015-deg offsets so each snaps to the
  same nearest cell).
"""

from __future__ import annotations

import pathlib

import pyproj

# --- Paths -----------------------------------------------------------------

FIXTURE_DIR = pathlib.Path(__file__).resolve().parent
GIS_DIR = FIXTURE_DIR / "gis"
FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
GIS_DIR.mkdir(parents=True, exist_ok=True)

# --- .prj (copied verbatim from keliya_minimal) --------------------------

PRJ_WKT = (
    'PROJCS["unknown",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
    'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Albers"],PARAMETER["False_Easting",0.0],'
    'PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",105.0],'
    'PARAMETER["Standard_Parallel_1",25.0],'
    'PARAMETER["Standard_Parallel_2",47.0],'
    'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]'
)
(GIS_DIR / "keliya.prj").write_text(PRJ_WKT + "\n", encoding="utf-8")

# --- Grid snapshot design (matches test-side InMemoryGridSnapshot) --------

GRID_LON0 = 100.0
GRID_LAT0 = 36.0
GRID_LON_STEP = 0.1
GRID_LAT_STEP = 0.1
GRID_LON_COUNT = 6
GRID_LAT_COUNT = 6  # 6x6 = 36 cells total

# The 8 target cells used by both element barycenters and stations. Layout:
# 4 cells wide (lon 100.1..100.4) x 2 cells tall (lat 36.1..36.2).
# In canonical_ordinal 1..N with lon-major within each lat row, these are
# grid_cell_ids: 7, 8, 9, 10, 13, 14, 15, 16 (0-indexed).
TARGET_CELLS_LONLAT: list[tuple[float, float]] = []
for lat_i in (1, 2):  # lat rows 1 and 2
    for lon_i in (1, 2, 3, 4):  # lon cols 1..4
        TARGET_CELLS_LONLAT.append(
            (GRID_LON0 + lon_i * GRID_LON_STEP, GRID_LAT0 + lat_i * GRID_LAT_STEP)
        )
assert len(TARGET_CELLS_LONLAT) == 8

# --- Stations: 32 total, 4 per target cell ------------------------------

# Offsets small enough that each station snaps to the target cell center
# under nearest-cell mapping (half-cell diagonal at 0.1 deg is ~7km at
# lat 36; 0.015 deg is ~1.4km, well inside).
STATION_OFFSETS = [
    (-0.015, -0.015),
    (0.015, -0.015),
    (-0.015, 0.015),
    (0.015, 0.015),
]
STATIONS_LONLAT: list[tuple[float, float]] = []
for center_lon, center_lat in TARGET_CELLS_LONLAT:
    for dlon, dlat in STATION_OFFSETS:
        STATIONS_LONLAT.append((center_lon + dlon, center_lat + dlat))
assert len(STATIONS_LONLAT) == 32

# --- Mesh design ---------------------------------------------------------

# We want element barycenters to fall inside the 4x2 = 8 target cells.
# That footprint is ~0.4 deg lon x ~0.2 deg lat = ~36km x ~22km at lat 36.
# Center it at (lon=100.25, lat=36.2) so it straddles the boundary between
# lat rows 1 and 2 and lon columns 2 and 3.
#
# Node grid: 23 lon x 12 lat = 276 nodes -> 22 x 11 quads = 484 triangles.
# Node step 1400m in the package CRS gives:
#   mesh footprint = 22*1400m x 11*1400m = 30.8km x 15.4km
# which is roughly 3.4 cells wide x 1.4 cells tall in WGS84, positioned to
# straddle boundaries and produce exactly 8 unique used cells.

_transformer_to_pkg = pyproj.Transformer.from_crs(
    "EPSG:4326", pyproj.CRS.from_wkt(PRJ_WKT), always_xy=True
)
basin_center_lon, basin_center_lat = 100.25, 36.15
basin_center_x, basin_center_y = _transformer_to_pkg.transform(
    basin_center_lon, basin_center_lat
)

# 23 lon nodes x 12 lat nodes -> 22 x 11 quads = 484 triangles exactly.
# Use different X and Y steps so the mesh footprint spans exactly 4 cell
# widths and 2 cell heights (0.4 deg lon x 0.2 deg lat), which gives 8
# unique used cells under nearest-cell mapping:
#   NODE_STEP_X * 22 quads ≈ 4 * 9km (0.4 deg lon at lat 36) → 1636m
#   NODE_STEP_Y * 11 quads ≈ 2 * 11.1km (0.2 deg lat)       → 2018m
# Rounded to 1600m / 2000m for reproducibility; the mesh footprint is
# then 35.2km x 22km, which sits comfortably inside the 4x2 target cells.
NODE_LON_COUNT = 23
NODE_LAT_COUNT = 12
NODE_STEP_X = 1600.0  # meters
NODE_STEP_Y = 2000.0  # meters

NODE_ORIGIN_X = basin_center_x - (NODE_LON_COUNT - 1) * NODE_STEP_X / 2
NODE_ORIGIN_Y = basin_center_y - (NODE_LAT_COUNT - 1) * NODE_STEP_Y / 2


def node_id(i_lon: int, i_lat: int) -> int:
    """1-based node ID in row-major (lat, lon) order."""
    return i_lat * NODE_LON_COUNT + i_lon + 1


def node_xy(i_lon: int, i_lat: int) -> tuple[float, float]:
    return (
        NODE_ORIGIN_X + i_lon * NODE_STEP_X,
        NODE_ORIGIN_Y + i_lat * NODE_STEP_Y,
    )


# All 22*11 = 242 quads -> 2 triangles each -> 484 triangles.
elements: list[tuple[int, int, int, int]] = []
next_id = 1
for i_lat in range(NODE_LAT_COUNT - 1):
    for i_lon in range(NODE_LON_COUNT - 1):
        v_ll = node_id(i_lon, i_lat)
        v_lr = node_id(i_lon + 1, i_lat)
        v_ul = node_id(i_lon, i_lat + 1)
        v_ur = node_id(i_lon + 1, i_lat + 1)
        elements.append((next_id, v_ll, v_lr, v_ul))
        next_id += 1
        elements.append((next_id, v_lr, v_ur, v_ul))
        next_id += 1

assert len(elements) == 484

# --- .sp.mesh ------------------------------------------------------------

n_elements = len(elements)
n_element_cols = 8  # ID Node1 Node2 Node3 Nabr1 Nabr2 Nabr3 Zmax
n_nodes_total = NODE_LON_COUNT * NODE_LAT_COUNT
n_node_cols = 5  # ID X Y AqDepth Elevation

mesh_lines: list[str] = []
mesh_lines.append(f"{n_elements}\t{n_element_cols}")
mesh_lines.append("ID\tNode1\tNode2\tNode3\tNabr1\tNabr2\tNabr3\tZmax")
for elem_id, v1, v2, v3 in elements:
    mesh_lines.append(f"{elem_id}\t{v1}\t{v2}\t{v3}\t0\t0\t0\t100")
mesh_lines.append(f"{n_nodes_total}\t{n_node_cols}")
mesh_lines.append("ID\tX\tY\tAqDepth\tElevation")
for i_lat in range(NODE_LAT_COUNT):
    for i_lon in range(NODE_LON_COUNT):
        nid = node_id(i_lon, i_lat)
        x, y = node_xy(i_lon, i_lat)
        mesh_lines.append(f"{nid}\t{x:.4f}\t{y:.4f}\t8\t100")

mesh_text = "\n".join(mesh_lines) + "\n"
(FIXTURE_DIR / "keliya.sp.mesh").write_text(mesh_text, encoding="utf-8")
print(
    f"Wrote keliya.sp.mesh: {n_elements} elements, {n_nodes_total} nodes"
)

# --- .sp.att -------------------------------------------------------------

# Stripe FORC values 1..32 across the 484 rows so every station gets
# referenced. In the variant .sp.att the FORC values will be rewritten to
# 1..8 (the shud_forcing_index of the 8 used cells).
att_lines: list[str] = []
n_att_cols = 9
att_lines.append(f"{n_elements}\t{n_att_cols}")
att_lines.append("INDEX\tSOIL\tGEOL\tLC\tFORC\tMF\tBC\tSS\tLAKE")
for i, (elem_id, _v1, _v2, _v3) in enumerate(elements):
    forc = (i % 32) + 1
    att_lines.append(f"{elem_id}\t1\t1\t11\t{forc}\t1\t0\t0\t0")

att_text = "\n".join(att_lines) + "\n"
(FIXTURE_DIR / "keliya.sp.att").write_text(att_text, encoding="utf-8")
print(f"Wrote keliya.sp.att: {n_elements} INDEX rows, FORC in 1..32")

# --- .tsd.forc -----------------------------------------------------------

tsd_lines: list[str] = []
tsd_lines.append("32 20200101")
tsd_lines.append("./input/keliya")
tsd_lines.append("ID\tLon\tLat\tX\tY\tZ\tFilename")
for i, (lon, lat) in enumerate(STATIONS_LONLAT, start=1):
    x, y = _transformer_to_pkg.transform(lon, lat)
    tsd_lines.append(
        f"{i}\t{lon:.4f}\t{lat:.4f}\t{x:.2f}\t{y:.2f}\t-9999\tforcing_{i}.csv"
    )

tsd_text = "\n".join(tsd_lines) + "\n"
(FIXTURE_DIR / "keliya.tsd.forc").write_text(tsd_text, encoding="utf-8")
print("Wrote keliya.tsd.forc: 32 stations")

print("Done.")
