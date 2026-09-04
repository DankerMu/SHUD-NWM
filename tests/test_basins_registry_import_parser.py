"""Basins registry import: shapefile discovery, headers, projection and geometry counts.

Partition 2 of 7 of the former monolith ``tests/test_basins_registry_import.py``
(issue #1913): the 13 parser contracts, including the opt-in real-Basins parser
smoke, which stays a non-integration skip.  Shared test support lives in the
non-collectible ``tests/basins_registry_import_helpers.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pyproj import Transformer

import workers.model_registry.basins_geometry as basins_geometry
from tests.basins_registry_import_helpers import (
    _write_domain_shapefile,
    _write_line_shapefile,
    _write_registry_fixture,
    _write_river_shapefile_with_geometry,
)
from workers.model_registry.basins_discovery import (
    discover_basins_inventory,
    write_inventory,
)
from workers.model_registry.basins_geometry import parse_basins_geometry
from workers.model_registry.basins_registry_import import prepare_basins_import_sources
from workers.model_registry.cli import _argparse_main


def test_parser_reads_real_shapefiles_and_shud_evidence(tmp_path: Path) -> None:
    root, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(tmp_path)
    del root, inventory_path, manifest_path
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    assert parsed.domain_wkt.startswith("MULTIPOLYGON")
    # PR 2: segment_count now equals the .sp.riv reach count (one row per reach).
    assert parsed.segment_count == 2
    assert parsed.evidence_counts == {
        "river_count": 2,
        "river_columns": 6,
        "rivseg_segment_count": 2,
        "rivseg_columns": 4,
    }
    # segment_order carries the SHUD reach Index verbatim from river.shp.
    assert [segment.segment_order for segment in parsed.river_segments] == [1, 2]
    # downstream IDs follow the new <model>_reach_<iRiv:06d> convention.
    assert parsed.river_segments[0].downstream_segment_id == f"{model_id}_reach_000002"
    assert parsed.river_segments[1].downstream_segment_id is None
    assert parsed.river_segments[1].properties["terminal_reach"] is True
    # PR 2: parser emits single-part LineString WKT; SQL-side ST_Multi wraps
    # it into the geometry(MultiLineString, 4490) column at insert time.
    assert parsed.river_segments[0].geom_wkt.startswith("LINESTRING(")


def test_parser_emits_single_part_linestring_per_reach(tmp_path: Path) -> None:
    """PR 2 contract replaces the legacy cross-gap MultiLineString assertion:
    the parser is now driven by gis/river.shp (single-part flow-ordered
    reaches by construction) and emits one LineString per reach without
    any greedy stitching or gap split. Where seg.shp records used to carry
    multi-part bridges, river.shp does not -- the bridge is removed at the
    source. This test pins the new shape contract."""

    root, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path, sp_segment_count=1
    )
    del root, inventory_path, manifest_path
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    assert parsed.segment_count == 1
    wkt = parsed.river_segments[0].geom_wkt
    assert wkt.startswith("LINESTRING(")
    assert "MULTILINESTRING" not in wkt


def test_parser_emits_unique_reach_ids_from_river_shp_index(tmp_path: Path) -> None:
    """PR 2: river_segment_id is derived from river.shp's Index column,
    zero-padded to 6 digits. By construction every river.shp record has a
    unique Index (the .sp.riv invariant) so duplicate disambiguation
    machinery is no longer needed."""

    root, input_dir, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path, sp_segment_count=3
    )
    del root, inventory_path, manifest_path
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    ids = [segment.river_segment_id for segment in parsed.river_segments]
    assert len(ids) == 3
    assert len(set(ids)) == 3
    assert ids == [
        f"{model_id}_reach_000001",
        f"{model_id}_reach_000002",
        f"{model_id}_reach_000003",
    ]
    # The fixture sets Down=2 on Index=1 and Down=0 (terminal) on the rest.
    assert parsed.river_segments[0].downstream_segment_id == f"{model_id}_reach_000002"
    assert parsed.river_segments[1].downstream_segment_id is None
    assert parsed.river_segments[2].downstream_segment_id is None
    assert parsed.river_segments[1].properties["terminal_reach"] is True


def test_parser_enforces_gis_sidecar_byte_limit_before_buffering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]
    monkeypatch.setattr(basins_geometry, "MAX_BASINS_GIS_SIDECAR_BYTES", 1)

    with pytest.raises(basins_geometry.BasinsGeometryError) as error:
        parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name="alias-a",
            required_files=model["required_files"],
        )

    assert error.value.error_code == "BASINS_REGISTRY_RESOURCE_LIMIT_EXCEEDED"
    assert error.value.details["resource"] == "gis_sidecar_bytes"
    assert error.value.details["count"] > error.value.details["limit"]


def test_parser_rejects_projected_prj_with_epsg(tmp_path: Path) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    projected = (
        'PROJCS["WGS_1984_Web_Mercator_Auxiliary_Sphere",'
        'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
        'SPHEROID["WGS_1984",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Mercator_Auxiliary_Sphere"],AUTHORITY["EPSG","3857"]]\n'
    )
    (input_dir / "gis" / "domain.prj").write_text(projected, encoding="utf-8")
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    with pytest.raises(basins_geometry.BasinsGeometryError) as error:
        parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name="alias-a",
            required_files=model["required_files"],
        )

    assert error.value.error_code == "BASINS_REGISTRY_GIS_CRS_UNSUPPORTED"


def test_parser_reprojects_basins_albers_to_lon_lat(tmp_path: Path) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    albers_prj = (
        'PROJCS["unknown",GEOGCS["unknown",DATUM["WGS_1984",'
        'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
        'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
        'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]]],'
        'PROJECTION["Albers_Conic_Equal_Area"],PARAMETER["latitude_of_center",0],'
        'PARAMETER["longitude_of_center",102],PARAMETER["standard_parallel_1",34.35],'
        'PARAMETER["standard_parallel_2",33.85],PARAMETER["false_easting",0],'
        'PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
        'AXIS["Easting",EAST],AXIS["Northing",NORTH]]\n'
    )
    transformer = Transformer.from_crs("EPSG:4490", albers_prj, always_xy=True)
    _write_domain_shapefile(
        input_dir / "gis" / "domain",
        points=[transformer.transform(x, y) for x, y in [(100.0, 30.0), (101.0, 30.0), (101.0, 31.0), (100.0, 31.0)]],
        prj_text=albers_prj,
    )
    # PR 2: river.shp must carry the SHUD 14-field attribute table; build
    # one with projected geometry to assert the CRS transform path.
    _write_river_shapefile_with_geometry(
        input_dir / "gis" / "river",
        reaches=[
            [transformer.transform(100.1, 30.1), transformer.transform(100.5, 30.4)],
            [transformer.transform(100.5, 30.4), transformer.transform(100.8, 30.8)],
        ],
        downstreams=[2, 0],
        prj_text=albers_prj,
    )
    _write_line_shapefile(
        input_dir / "gis" / "seg",
        record_count=2,
        prj_text=albers_prj,
    )
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    assert "100.1" in parsed.river_segments[0].geom_wkt
    assert "30.1" in parsed.river_segments[0].geom_wkt
    assert "3600000" not in parsed.domain_wkt
    assert parsed.river_segments[0].properties["source_crs_projected"] is True
    assert parsed.river_segments[0].properties["source_projection_method"] == "albers equal area"


def test_parser_preserves_domain_polygon_holes(tmp_path: Path) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path, domain_with_hole=True)
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    assert parsed.domain_wkt.startswith("MULTIPOLYGON(((")
    assert ")), ((" not in parsed.domain_wkt
    assert "), (" in parsed.domain_wkt


def test_parser_enforces_resource_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]
    monkeypatch.setattr(basins_geometry, "MAX_BASINS_GIS_FEATURES", 1)

    with pytest.raises(basins_geometry.BasinsGeometryError) as error:
        parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name="alias-a",
            required_files=model["required_files"],
        )

    assert error.value.error_code == "BASINS_REGISTRY_RESOURCE_LIMIT_EXCEEDED"
    assert error.value.details["resource"] == "features"


def test_parser_enforces_shud_evidence_resource_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]
    (input_dir / "alias-a.sp.riv").write_text("# header\n1 2\n", encoding="utf-8")
    monkeypatch.setattr(basins_geometry, "MAX_BASINS_SHUD_EVIDENCE_LINES", 1)

    with pytest.raises(basins_geometry.BasinsGeometryError) as error:
        parse_basins_geometry(
            model_id=model_id,
            input_dir=input_dir,
            shud_input_name="alias-a",
            required_files=model["required_files"],
        )

    assert error.value.error_code == "BASINS_REGISTRY_RESOURCE_LIMIT_EXCEEDED"
    assert error.value.details == {"resource": "shud_evidence_lines", "count": 2, "limit": 1}


def test_parser_stops_at_declared_shud_count_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, input_dir, _, _, model_id = _write_registry_fixture(tmp_path)
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]
    trailing_payload = "\n".join("1 2 3" for _ in range(20))
    (input_dir / "alias-a.sp.riv").write_text(f"2\n{trailing_payload}\n", encoding="utf-8")
    (input_dir / "alias-a.sp.rivseg").write_text(f"2\n{trailing_payload}\n", encoding="utf-8")
    monkeypatch.setattr(basins_geometry, "MAX_BASINS_SHUD_EVIDENCE_LINES", 1)

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    assert parsed.segment_count == 2
    assert parsed.evidence_counts["river_count"] == 2
    assert parsed.evidence_counts["rivseg_segment_count"] == 2


def test_import_accepts_sp_riv_river_count_different_from_rivseg_segments(tmp_path: Path) -> None:
    """PR 2: segment_count now equals .sp.riv reach count (one row per
    reach), not the .sp.rivseg segment count. rivseg_segment_count is
    retained as historical evidence only."""

    _, _, inventory_path, manifest_path, model_id = _write_registry_fixture(
        tmp_path,
        sp_river_count=1,
        sp_segment_count=2,
    )

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)

    assert sources.ids["model_id"] == model_id
    # row granularity = reach count, not rivseg segment count.
    assert sources.geometry.segment_count == 1
    assert sources.geometry.evidence_counts["river_count"] == 1
    assert sources.geometry.evidence_counts["rivseg_segment_count"] == 2


def test_parsed_geometry_segment_count_equals_sp_riv_reach_count(tmp_path: Path) -> None:
    """PR 2: post-Path-C, both ``segment_count`` (core.river_segment row
    count) and ``output_segment_count`` (.sp.riv reach count) equal the
    .sp.riv reach count. .sp.rivseg is retained only as evidence -- it no
    longer drives any row granularity."""

    _, input_dir, _, _, model_id = _write_registry_fixture(
        tmp_path,
        sp_river_count=3,
        sp_segment_count=5,
    )
    inventory = discover_basins_inventory(tmp_path / "basins")
    model = inventory["models"][0]

    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-a",
        required_files=model["required_files"],
    )

    # PR 2 row granularity: reach count drives both numbers.
    assert parsed.output_segment_count == 3
    assert parsed.segment_count == 3
    # rivseg count survives in evidence_counts as historical record only.
    assert parsed.evidence_counts["river_count"] == 3
    assert parsed.evidence_counts["rivseg_segment_count"] == 5


@pytest.mark.skipif(
    os.getenv("NHMS_RUN_REAL_BASINS_IMPORT") != "1" or not Path("data/Basins").exists(),
    reason="real Basins parser smoke is opt-in and requires data/Basins",
)
def test_real_basins_parser_smoke_reprojects_and_uses_rivseg_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory = discover_basins_inventory(Path("data/Basins"))
    valid = next(model for model in inventory["models"] if model["status"] == "valid")
    inventory["models"] = [valid]
    inventory["model_count"] = 1
    inventory_path = tmp_path / "real-inventory.json"
    write_inventory(inventory, inventory_path)
    object_root = tmp_path / "object-store"
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(object_root))
    monkeypatch.setenv("OBJECT_STORE_PREFIX", "s3://nhms")
    manifest_path = tmp_path / "real-manifest.json"
    assert _argparse_main(
        [
            "publish-basins",
            "--inventory",
            str(inventory_path),
            "--model-id",
            valid["model_id"],
            "--version",
            "vbasins-real-parser-smoke",
            "--output",
            str(manifest_path),
        ]
    ) == 0

    sources = prepare_basins_import_sources(inventory_path=inventory_path, package_manifest_path=manifest_path)

    # PR 2: segment_count tracks .sp.riv reach count, not rivseg segment count.
    assert sources.geometry.segment_count == sources.geometry.evidence_counts["river_count"]
    assert sources.geometry.evidence_counts["river_count"] != sources.geometry.evidence_counts["rivseg_segment_count"]
    first_wkt = sources.geometry.river_segments[0].geom_wkt
    # PR 2 parser emits single-part LineString WKT; SQL-side ST_Multi wraps
    # it at insert time, but the parser product stays a plain LineString.
    assert first_wkt.startswith("LINESTRING(")
    first_point = first_wkt.removeprefix("LINESTRING(").split(",", 1)[0]
    first_numbers = [float(value) for value in first_point.split()]
    assert -180 <= first_numbers[0] <= 180
    assert -90 <= first_numbers[1] <= 90
