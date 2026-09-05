"""Shared, non-collectible support for the Basins registry-import suites.

Owns the 19 helper functions, the ``_FakeRiverSegmentCursor`` class and the four
private constants of the former monolith ``tests/test_basins_registry_import.py``
(baseline lines 49-50, 2018-2492, 2618-2720 and 3651-3675), plus the QHH sample
fixture constants and loaders that keep the QHH partition under the 1,000-line
structural limit (issue #1913).  Defines no ``test_*`` callable and collects zero
nodes.

The same-named helpers in ``tests/basins_package_helpers.py``
(``_make_valid_model`` / ``_invoke_click``) are deliberately NOT reused: theirs build
package-publication models and drive the publication CLI, not the registry/GIS
contract asserted here.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.integration_helpers import psycopg_connection
from workers.model_registry.basins_discovery import (
    discover_basins_inventory,
    write_inventory,
)
from workers.model_registry.basins_geometry import parse_basins_geometry
from workers.model_registry.basins_package import BASINS_PACKAGE_SCHEMA_VERSION
from workers.model_registry.cli import _click_main

_CLI_MODEL_ADMIN_AUTH_ARGS = ["--auth-actor-id", "cli-model-admin", "--auth-role", "model_admin"]


_PUBLIC_IMPORT_UNKNOWN_TARGET_ID = "unknown"


def _write_registry_fixture(
    tmp_path: Path,
    *,
    basin_slug: str = "basin-a",
    sp_river_count: int | None = None,
    sp_segment_count: int = 2,
    domain_with_hole: bool = False,
) -> tuple[Path, Path, Path, Path, str]:
    root = tmp_path / "basins"
    input_dir = _make_valid_model(
        root / basin_slug,
        "alias-a",
        sp_river_count=sp_river_count,
        sp_segment_count=sp_segment_count,
        domain_with_hole=domain_with_hole,
    )
    inventory = discover_basins_inventory(root)
    inventory_path = tmp_path / "inventory.json"
    write_inventory(inventory, inventory_path)
    model = inventory["models"][0]
    model_id = model["model_id"]
    manifest = _package_manifest_for_model(model, model_id, inventory=inventory)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root, input_dir, inventory_path, manifest_path, model_id


def _make_valid_model(
    model_dir: Path,
    input_name: str,
    *,
    sp_river_count: int | None = None,
    sp_segment_count: int,
    domain_with_hole: bool = False,
) -> Path:
    input_dir = model_dir / "input" / input_name
    input_dir.mkdir(parents=True)
    for suffix in (
        "cfg.para",
        "cfg.calib",
        "sp.att",
        "para.soil",
        "para.geol",
        "para.lc",
        "tsd.forc",
        "tsd.lai",
        "tsd.mf",
        "tsd.rl",
    ):
        (input_dir / f"{input_name}.{suffix}").write_text(f"{suffix}\n", encoding="utf-8")
    # Real SHUD headers: discovery validates the IC header's numeric-token shape
    # against the mesh element count, so placeholder bodies would make every model
    # here invalid (#1197).
    (input_dir / f"{input_name}.cfg.ic").write_text(
        "484\t6\t38920320.000000\n1\t0.1\n", encoding="utf-8"
    )
    (input_dir / f"{input_name}.sp.mesh").write_text("484\t8\nID\tNode1\n", encoding="utf-8")
    river_count = sp_segment_count if sp_river_count is None else sp_river_count
    sp_riv_rows = "".join(f"{index} 0 0 0.01 100 0\n" for index in range(1, river_count + 1))
    (input_dir / f"{input_name}.sp.riv").write_text(f"{river_count} 6\n{sp_riv_rows}", encoding="utf-8")
    (input_dir / f"{input_name}.sp.rivseg").write_text(
        f"{sp_segment_count} 4\n1 1 1 100\n",
        encoding="utf-8",
    )
    gis_dir = input_dir / "gis"
    gis_dir.mkdir()
    _write_domain_shapefile(gis_dir / "domain", with_hole=domain_with_hole)
    # PR 2 contract: river.shp is the authoritative reach geometry source,
    # with one record per .sp.riv reach and the full SHUD attribute table.
    _write_river_shapefile(gis_dir / "river", reach_count=river_count)
    # seg.shp keeps the existing (iRiv, iEle)-style records for crosswalk
    # writes; the legacy "ORDER/DOWN_ID/LENGTH_M" attribute layout still
    # exercises the old test paths and gives the seg-driven helpers
    # something to parse.
    _write_line_shapefile(gis_dir / "seg", record_count=sp_segment_count)
    forcing = model_dir / "forcing"
    forcing.mkdir()
    (forcing / "X000001.csv").write_text("time,value\n2026-01-01,1\n", encoding="utf-8")
    return input_dir


def _write_river_shapefile(
    base: Path,
    *,
    reach_count: int,
    prj_text: str | None = None,
) -> None:
    """Write a river.shp containing the 14 PR-2 required dbf fields.

    Index runs 1..reach_count. The first reach has Down=2 to exercise the
    downstream resolver; the rest terminate (Down=0) so we never reference
    an Index that's not in the fixture.
    """

    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYLINE)
    for name in (
        "Index",
        "Down",
        "Type",
        "Slope",
        "Length",
        "BC",
        "Depth",
        "BankSlope",
        "Width",
        "Sinuosity",
        "Manning",
        "Cwr",
        "KsatH",
        "BedThick",
    ):
        if name in ("Index", "Down", "Type", "BC"):
            writer.field(name, "N")
        else:
            writer.field(name, "F", decimal=6)
    for index in range(1, reach_count + 1):
        base_lon = 100.0 + 0.1 * (index - 1)
        # Two-vertex single-part LineString per reach -- single-part is
        # the PR 2 contract.
        writer.line([[[base_lon, 30.0], [base_lon + 0.05, 30.05]]])
        down_index = 2 if index == 1 and reach_count >= 2 else 0
        writer.record(
            index,
            down_index,
            2,
            0.001,
            100.0,
            0,
            1.5,
            0.5,
            10.0,
            1.05,
            0.035,
            1.0,
            1.0e-5,
            0.5,
        )
    writer.close()
    _write_prj(base.with_suffix(".prj"), prj_text=prj_text)


def _copy_matching_fixture_payload(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _replace_directory_with_symlink(path: Path, target: Path) -> None:
    shutil.rmtree(path)
    path.symlink_to(target, target_is_directory=True)


def _write_domain_shapefile(
    base: Path,
    *,
    with_hole: bool = False,
    points: list[tuple[float, float]] | None = None,
    prj_text: str | None = None,
) -> None:
    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYGON)
    writer.field("ID", "N")
    outer = points or [(100.0, 30.0), (101.0, 30.0), (101.0, 31.0), (100.0, 31.0)]
    closed_outer = [list(point) for point in [*outer, outer[0]]]
    rings = [closed_outer]
    if with_hole:
        rings.append([[100.2, 30.2], [100.2, 30.8], [100.8, 30.8], [100.8, 30.2], [100.2, 30.2]])
    writer.poly(rings)
    writer.record(1)
    writer.close()
    _write_prj(base.with_suffix(".prj"), prj_text=prj_text)


def _write_river_shapefile_with_geometry(
    base: Path,
    *,
    reaches: list[list[tuple[float, float]]],
    downstreams: list[int],
    prj_text: str | None = None,
) -> None:
    """Variant of ``_write_river_shapefile`` for tests that need specific
    polylines (e.g. projected-CRS reprojection coverage). Each ``reaches``
    entry is a single-part LineString point list; the Index column counts
    from 1 to match the polyline list length.
    """

    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYLINE)
    for name in (
        "Index",
        "Down",
        "Type",
        "Slope",
        "Length",
        "BC",
        "Depth",
        "BankSlope",
        "Width",
        "Sinuosity",
        "Manning",
        "Cwr",
        "KsatH",
        "BedThick",
    ):
        if name in ("Index", "Down", "Type", "BC"):
            writer.field(name, "N")
        else:
            writer.field(name, "F", decimal=6)
    assert len(reaches) == len(downstreams)
    for index, (line, down_index) in enumerate(zip(reaches, downstreams, strict=True), start=1):
        writer.line([[list(point) for point in line]])
        writer.record(
            index,
            int(down_index),
            2,
            0.001,
            100.0,
            0,
            1.5,
            0.5,
            10.0,
            1.05,
            0.035,
            1.0,
            1.0e-5,
            0.5,
        )
    writer.close()
    _write_prj(base.with_suffix(".prj"), prj_text=prj_text)


def _write_line_shapefile(
    base: Path,
    *,
    points: list[list[tuple[float, float]]] | None = None,
    records: list[tuple[int, int, int, float]] | None = None,
    record_count: int | None = None,
    prj_text: str | None = None,
) -> None:
    """Write a polyline shapefile -- used both for seg.shp fixtures and the
    legacy ad-hoc river/seg shapes some tests still construct directly.

    When ``record_count`` is supplied (the new seg.shp default), the file
    follows the SHUD ``(iRiv, iEle)``-style attribute layout that
    crosswalk parsing expects. Otherwise the legacy
    ``(SEG_ID, ORDER, DOWN_ID, LENGTH_M)`` shape is preserved so the older
    tests that build hand-crafted ``records=[...]`` lists keep working.
    """

    import shapefile

    writer = shapefile.Writer(str(base), shapeType=shapefile.POLYLINE)
    if record_count is not None:
        writer.field("iRiv", "N")
        writer.field("iEle", "N")
        for record_index in range(record_count):
            # iRiv cycles over the reach 1..reach_count range so every
            # crosswalk row finds a parent reach in river.shp (PR 2 FK).
            iriv = (record_index % max(1, record_count)) + 1
            iele = record_index + 1
            base_lon = 100.0 + 0.05 * record_index
            writer.line([[[base_lon, 30.0], [base_lon + 0.01, 30.01]]])
            writer.record(iriv, iele)
    else:
        writer.field("SEG_ID", "N")
        writer.field("ORDER", "N")
        writer.field("DOWN_ID", "N")
        writer.field("LENGTH_M", "F", decimal=3)
        lines = points or [[(100.1, 30.1), (100.5, 30.4)], [(100.5, 30.4), (100.8, 30.8)]]
        line_records = records or [(1, 1, 2, 50000.0), (2, 2, 0, 60000.0)]
        for line, record in zip(lines, line_records, strict=True):
            writer.line([[list(point) for point in line]])
            writer.record(*record)
    writer.close()
    _write_prj(base.with_suffix(".prj"), prj_text=prj_text)


def _write_prj(path: Path, *, prj_text: str | None = None) -> None:
    path.write_text(
        prj_text
        or (
            'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
            'SPHEROID["WGS_1984",6378137,298.257223563]],'
            'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]]\n'
        ),
        encoding="utf-8",
    )


def _package_manifest_for_model(
    model: dict[str, Any],
    model_id: str,
    *,
    inventory: dict[str, Any],
    package_schema_version: str = BASINS_PACKAGE_SCHEMA_VERSION,
) -> dict[str, Any]:
    version = "vbasins-test"
    package_uri = f"s3://nhms/models/{model_id}/{version}/package/"
    included_files = [
        {
            "relative_path": relative_path,
            "object_uri": package_uri + relative_path,
            "size_bytes": (Path(model["input_dir"]) / relative_path).stat().st_size,
            "sha256": checksum,
            "role": "gis" if relative_path.startswith("gis/") else "runtime_input",
        }
        for relative_path, checksum in sorted(model["checksums"].items())
    ]
    return {
        "schema_version": package_schema_version,
        "model_id": model_id,
        "version": version,
        "basin_slug": model["basin_slug"],
        "shud_input_name": model["shud_input_name"],
        "model_package_uri": package_uri,
        "manifest_uri": f"s3://nhms/models/{model_id}/{version}/manifest.json",
        "package_checksum": "package-sha-1",
        "source_inventory_checksum": _sha256_inventory_document(inventory),
        "source_inventory_schema_version": inventory["schema_version"],
        "source_path": model["source_path"],
        "resolved_source_path": model["resolved_source_path"],
        "source_is_symlink": False,
        "included_files": included_files,
        "forcing": {"policy": "excluded_by_default", "csv_count": 1},
        "calibration": {"source_count": 0, "included_count": 0},
        "created_at": "2026-05-16T00:00:00Z",
    }


def _sha256_inventory_document(inventory: dict[str, Any]) -> str:
    content = (json.dumps(inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _assert_registry_fixture_rows_absent(database_url: str, inventory_path: Path, model_id: str) -> None:
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = next(model for model in inventory["models"] if model["model_id"] == model_id)
    ids = model["suggested_ids"]
    with psycopg_connection(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM core.model_instance WHERE model_id = %s", (model_id,))
            assert cursor.fetchone()["count"] == 0
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.mesh_version WHERE mesh_version_id = %s",
                (ids["mesh_version_id"],),
            )
            assert cursor.fetchone()["count"] == 0
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.river_segment WHERE river_network_version_id = %s",
                (ids["river_network_version_id"],),
            )
            assert cursor.fetchone()["count"] == 0
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.river_network_version WHERE river_network_version_id = %s",
                (ids["river_network_version_id"],),
            )
            assert cursor.fetchone()["count"] == 0
            cursor.execute(
                "SELECT COUNT(*) AS count FROM core.basin_version WHERE basin_version_id = %s",
                (ids["basin_version_id"],),
            )
            assert cursor.fetchone()["count"] == 0
            cursor.execute("SELECT COUNT(*) AS count FROM core.basin WHERE basin_id = %s", (ids["basin_id"],))
            assert cursor.fetchone()["count"] == 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _FakeRiverSegmentCursor:
    """Minimal in-memory stand-in for core.river_segment writes.

    Routes the two read queries used by ``_ensure_output_river_segments``
    (output-row COUNT and the ordered digest SELECT) and records rows that the
    patched ``execute_values`` shim inserts. Avoids any live-DB dependency for
    local runs; real-DB coverage lives in the @integration tests.
    """

    def __init__(self, river_network_version_id: str) -> None:
        self._rnv_id = river_network_version_id
        self._rows: list[dict[str, Any]] = []
        self._last: list[dict[str, Any]] = []

    def insert_rows(self, rows: list[tuple[Any, ...]]) -> None:
        for river_segment_id, rnv_id, segment_order, properties in rows:
            self._rows.append(
                {
                    "river_segment_id": river_segment_id,
                    "river_network_version_id": rnv_id,
                    "segment_order": segment_order,
                    # store the plain dict (mirrors what RealDictCursor yields on read)
                    "properties_json": _adapted(properties),
                }
            )

    def _output_rows(self) -> list[dict[str, Any]]:
        return [
            row
            for row in self._rows
            if row["river_network_version_id"] == self._rnv_id
            and bool(_adapted(row["properties_json"]).get("shud_output_river"))
        ]

    def output_river_segments(self) -> list[dict[str, Any]]:
        return sorted(self._output_rows(), key=lambda row: row["river_segment_id"])

    def geometry_segment_count(self) -> int:
        return sum(
            1
            for row in self._rows
            if row["river_network_version_id"] == self._rnv_id
            and not bool(_adapted(row["properties_json"]).get("shud_output_river"))
        )

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> None:
        normalized = " ".join(str(statement).split())
        if "COUNT(*)" in normalized:
            self._last = [{"count": len(self._output_rows())}]
        else:
            self._last = [
                {
                    "river_segment_id": row["river_segment_id"],
                    "segment_order": row["segment_order"],
                    "properties_json": row["properties_json"],
                }
                for row in self.output_river_segments()
            ]

    def fetchone(self) -> dict[str, Any] | None:
        return self._last[0] if self._last else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._last)


def _adapted(value: Any) -> dict[str, Any]:
    adapted = getattr(value, "adapted", value)
    return adapted if isinstance(adapted, dict) else {}


def _patch_execute_values(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_execute_values(cursor: Any, _sql: str, rows: list[tuple[Any, ...]], **_kwargs: Any) -> None:
        cursor.insert_rows(list(rows))

    monkeypatch.setattr("psycopg2.extras.execute_values", fake_execute_values)


def _invoke_click(argv: list[str]) -> int:
    try:
        return _click_main(argv)
    except SystemExit as error:
        if isinstance(error.code, int):
            return error.code
        return 1


# ---------------------------------------------------------------------------
# PR 1 (issue #560): crosswalk pure-function unit tests (no production wiring)
# ---------------------------------------------------------------------------

_QHH_SAMPLE_SEG_SHP = (
    Path(__file__).parent / "fixtures" / "basins" / "qhh-sample" / "gis" / "seg.shp"
)


# ---------------------------------------------------------------------------
# PR 2 (issue #561): DB-side atomic switch tests (Section 2 of tasks.md)
# ---------------------------------------------------------------------------

_QHH_SAMPLE_DIR = Path(__file__).parent / "fixtures" / "basins" / "qhh-sample"


def _stage_qhh_sample_fixture(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    """Copy the qhh-sample shapefile + .sp.riv/.sp.rivseg into a fresh
    model directory and register it through the discovery+manifest path
    so the regular ``parse_basins_geometry`` entry point can read it.

    Returns ``(input_dir, inventory_path, manifest_path, model_id)``.

    The basin_slug is derived from tmp_path.name so every test invocation
    yields a unique model_id under the session-scoped integration DB,
    avoiding cross-test CHECKSUM_CONFLICT pollution. apply_migrations_from_zero
    only re-applies missing migrations; it does NOT truncate existing rows.
    """

    # pytest tmp_path.name shape: test_<name>0 / test_<name>1 / etc — unique per test
    basin_slug = f"qhh-sample-{tmp_path.name}".replace("_", "-").lower()
    input_name = "alias-qhh-sample"
    root = tmp_path / "basins"
    input_dir = root / basin_slug / "input" / input_name
    input_dir.mkdir(parents=True)
    # Stage every required SHUD canonical file. Empty placeholders for files
    # the parser does not read are fine -- only sp.riv/sp.rivseg/gis files
    # contribute to the geometry assertions here.
    for suffix in (
        "cfg.para",
        "cfg.calib",
        "sp.att",
        "para.soil",
        "para.geol",
        "para.lc",
        "tsd.forc",
        "tsd.lai",
        "tsd.mf",
        "tsd.rl",
    ):
        (input_dir / f"{input_name}.{suffix}").write_text(f"{suffix}\n", encoding="utf-8")
    # Real SHUD headers: discovery validates the IC header's numeric-token shape
    # against the mesh element count, so placeholder bodies would make every model
    # here invalid (#1197).
    (input_dir / f"{input_name}.cfg.ic").write_text(
        "484\t6\t38920320.000000\n1\t0.1\n", encoding="utf-8"
    )
    (input_dir / f"{input_name}.sp.mesh").write_text("484\t8\nID\tNode1\n", encoding="utf-8")
    # Bring the fixture sp.riv / sp.rivseg in under the alias name.
    shutil.copy2(_QHH_SAMPLE_DIR / "qhh.sp.riv", input_dir / f"{input_name}.sp.riv")
    shutil.copy2(_QHH_SAMPLE_DIR / "qhh.sp.rivseg", input_dir / f"{input_name}.sp.rivseg")
    # qhh-sample.sp.riv declares 1633 reaches in its header (the production
    # qhh count); rewrite the header to 5 so the discover/parser checks
    # match the 5-record river.shp subset.
    sp_riv_path = input_dir / f"{input_name}.sp.riv"
    sp_riv_text = sp_riv_path.read_text(encoding="utf-8").splitlines()
    sp_riv_text[0] = f"5 {sp_riv_text[0].split()[-1] if len(sp_riv_text[0].split()) > 1 else 6}"
    sp_riv_path.write_text("\n".join(sp_riv_text) + "\n", encoding="utf-8")
    # Same for sp.rivseg: rewrite the declared segment count to 18.
    sp_rivseg_path = input_dir / f"{input_name}.sp.rivseg"
    sp_rivseg_text = sp_rivseg_path.read_text(encoding="utf-8").splitlines()
    sp_rivseg_text[0] = f"18 {sp_rivseg_text[0].split()[-1] if len(sp_rivseg_text[0].split()) > 1 else 4}"
    sp_rivseg_path.write_text("\n".join(sp_rivseg_text) + "\n", encoding="utf-8")
    # Copy GIS layers, including river.shp's full 14-field dbf.
    gis_dst = input_dir / "gis"
    gis_dst.mkdir()
    for layer in ("river", "seg"):
        for suffix in ("shp", "shx", "dbf", "prj"):
            shutil.copy2(
                _QHH_SAMPLE_DIR / "gis" / f"{layer}.{suffix}",
                gis_dst / f"{layer}.{suffix}",
            )
    # The qhh-sample fixture has no domain.shp; synthesise one so the
    # parser can resolve the domain layer (its content is not asserted).
    _write_domain_shapefile(gis_dst / "domain")
    # forcing dir lets the discovery layer treat the model as importable.
    forcing = root / basin_slug / "forcing"
    forcing.mkdir()
    (forcing / "X000001.csv").write_text("time,value\n2026-01-01,1\n", encoding="utf-8")
    inventory = discover_basins_inventory(root)
    inventory_path = tmp_path / "inventory.json"
    write_inventory(inventory, inventory_path)
    model = inventory["models"][0]
    model_id = model["model_id"]
    manifest = _package_manifest_for_model(model, model_id, inventory=inventory)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return input_dir, inventory_path, manifest_path, model_id


def _parse_qhh_sample(tmp_path: Path) -> tuple[Any, str]:
    """Run ``parse_basins_geometry`` against the qhh-sample fixture."""

    input_dir, inventory_path, _manifest_path, model_id = _stage_qhh_sample_fixture(tmp_path)
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    model = inventory["models"][0]
    parsed = parse_basins_geometry(
        model_id=model_id,
        input_dir=input_dir,
        shud_input_name="alias-qhh-sample",
        required_files=model["required_files"],
    )
    return parsed, model_id


# ---------------------------------------------------------------------------
# Issue #575: reingest CLI seed_output toggle + extended refresh helper
# ---------------------------------------------------------------------------


def _spy_import_basin_helpers(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace every helper that ``import_basin_into_registry_core`` calls with
    a MagicMock that returns 0 (count helpers) or None (mutators). Returns the
    spy dict so the test can assert on call counts.
    """
    from unittest.mock import MagicMock

    import workers.model_registry.basins_registry_import as bri

    spies = {
        "_delete_legacy_seg_rows": MagicMock(return_value=False),
        "_refresh_parent_version_materialization": MagicMock(return_value=None),
        "_ensure_basin": MagicMock(return_value=0),
        "_ensure_basin_version": MagicMock(return_value=0),
        "_ensure_river_network": MagicMock(return_value=0),
        "_ensure_river_segments": MagicMock(return_value=0),
        "_ensure_output_river_segments": MagicMock(return_value=0),
        "_ensure_river_segment_crosswalk": MagicMock(return_value=0),
        "_ensure_mesh": MagicMock(return_value=0),
        "_ensure_model_instance": MagicMock(return_value=0),
        "_backfill_output_segment_geometry": MagicMock(return_value=None),
    }
    for name, spy in spies.items():
        monkeypatch.setattr(bri, name, spy)
    return spies
