from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from packages.common.safe_fs import SafeFilesystemError, atomic_write_bytes_no_follow, ensure_directory_no_follow
from services.production_closure.object_store_validation_contracts import ProductionObjectStoreValidationError
from workers.model_registry.basins_discovery import discover_basins_inventory
from workers.model_registry.basins_geometry import RIVER_SHP_REQUIRED_DBF_FIELDS


def write_synthetic_basins_fixture(root: Path, *, containment_root: Path | None = None) -> dict[str, Any]:
    input_name = "alias-a"
    model_dir = root / "basin-a"
    input_dir = model_dir / "input" / input_name
    _safe_fixture_dir(input_dir, containment_root=containment_root)
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
        _safe_fixture_write_text(input_dir / f"{input_name}.{suffix}", f"{suffix}\n", containment_root=containment_root)
    # The IC and mesh headers must be REAL: basins discovery validates the
    # ``*.cfg.ic`` header's numeric-token shape against the ``*.sp.mesh`` element
    # count (#1197), so a placeholder body would make this synthetic model
    # unpublishable and the whole object-store validation lane unrunnable.
    _safe_fixture_write_text(
        input_dir / f"{input_name}.cfg.ic",
        "2\t6\t0.000000\n1\t0.0\t0.0\t0.0\t0.0\t0.0\n2\t0.0\t0.0\t0.0\t0.0\t0.0\n",
        containment_root=containment_root,
    )
    _safe_fixture_write_text(
        input_dir / f"{input_name}.sp.mesh",
        "2\t8\nID\tNode1\tNode2\tNode3\tNabr1\tNabr2\tNabr3\tZmax\n",
        containment_root=containment_root,
    )
    _safe_fixture_write_text(
        input_dir / f"{input_name}.sp.riv",
        "2 6\n1 0 0 0.01 100 0\n",
        containment_root=containment_root,
    )
    _safe_fixture_write_text(
        input_dir / f"{input_name}.sp.rivseg",
        "2 4\n1 1 1 100\n",
        containment_root=containment_root,
    )
    gis_dir = input_dir / "gis"
    _safe_fixture_dir(gis_dir, containment_root=containment_root)
    _write_domain_shapefile(gis_dir / "domain", containment_root=containment_root)
    _write_river_shapefile(gis_dir / "river", containment_root=containment_root)
    _write_segment_crosswalk_shapefile(gis_dir / "seg", containment_root=containment_root)
    forcing_dir = model_dir / "forcing"
    _safe_fixture_dir(forcing_dir, containment_root=containment_root)
    _safe_fixture_write_text(
        forcing_dir / "X000001.csv",
        "time,value\n2026-01-01,1\n",
        containment_root=containment_root,
    )
    return discover_basins_inventory(root)


def _safe_fixture_dir(path: Path, *, containment_root: Path | None) -> None:
    try:
        ensure_directory_no_follow(path, containment_root=containment_root)
    except SafeFilesystemError as error:
        error_code = (
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED"
            if error.kind == "io"
            else "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
        )
        raise ProductionObjectStoreValidationError(
            error_code,
            f"Failed to prepare synthetic Basins fixture directory {path}: {error}",
        ) from error


def _safe_fixture_write_bytes(path: Path, content: bytes, *, containment_root: Path | None) -> None:
    try:
        atomic_write_bytes_no_follow(path, content, containment_root=containment_root)
    except SafeFilesystemError as error:
        error_code = (
            "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED"
            if error.kind == "io"
            else "PRODUCTION_OBJECT_STORE_EVIDENCE_PATH_UNSAFE"
        )
        raise ProductionObjectStoreValidationError(
            error_code,
            f"Failed to write synthetic Basins fixture file {path}: {error}",
        ) from error


def _safe_fixture_write_text(path: Path, content: str, *, containment_root: Path | None) -> None:
    _safe_fixture_write_bytes(path, content.encode("utf-8"), containment_root=containment_root)


def _write_domain_shapefile(base: Path, *, containment_root: Path | None = None) -> None:
    import shapefile

    target_base = base
    if containment_root is not None:
        temp_dir = tempfile.TemporaryDirectory(prefix="nhms-synthetic-basins-shp-")
        target_base = Path(temp_dir.name) / base.name
    else:
        temp_dir = None
    writer = shapefile.Writer(str(target_base), shapeType=shapefile.POLYGON)
    writer.field("ID", "N")
    writer.poly([[[100.0, 30.0], [101.0, 30.0], [101.0, 31.0], [100.0, 31.0], [100.0, 30.0]]])
    writer.record(1)
    writer.close()
    _write_wgs84_prj(base.with_suffix(".prj"), containment_root=containment_root)
    if temp_dir is not None:
        try:
            _copy_fixture_shapefile_outputs(target_base, base, containment_root=containment_root)
        finally:
            temp_dir.cleanup()


def _write_river_shapefile(base: Path, *, containment_root: Path | None = None) -> None:
    import shapefile

    target_base = base
    if containment_root is not None:
        temp_dir = tempfile.TemporaryDirectory(prefix="nhms-synthetic-basins-shp-")
        target_base = Path(temp_dir.name) / base.name
    else:
        temp_dir = None
    writer = shapefile.Writer(str(target_base), shapeType=shapefile.POLYLINE)
    for name in RIVER_SHP_REQUIRED_DBF_FIELDS:
        if name in {"Index", "Down", "Type", "BC"}:
            writer.field(name, "N")
        else:
            writer.field(name, "F", decimal=6)
    writer.line([[[100.1, 30.1], [100.5, 30.4]]])
    writer.record(1, 2, 1, 0.001, 50_000.0, 0, 2.5, 0.5, 30.0, 1.1, 0.035, 0.2, 0.00001, 1.0)
    writer.line([[[100.5, 30.4], [100.8, 30.8]]])
    writer.record(2, 0, 1, 0.001, 60_000.0, 0, 2.8, 0.5, 32.0, 1.1, 0.035, 0.2, 0.00001, 1.0)
    writer.close()
    _write_wgs84_prj(base.with_suffix(".prj"), containment_root=containment_root)
    if temp_dir is not None:
        try:
            _copy_fixture_shapefile_outputs(target_base, base, containment_root=containment_root)
        finally:
            temp_dir.cleanup()


def _write_segment_crosswalk_shapefile(base: Path, *, containment_root: Path | None = None) -> None:
    import shapefile

    target_base = base
    if containment_root is not None:
        temp_dir = tempfile.TemporaryDirectory(prefix="nhms-synthetic-basins-shp-")
        target_base = Path(temp_dir.name) / base.name
    else:
        temp_dir = None
    writer = shapefile.Writer(str(target_base), shapeType=shapefile.POLYLINE)
    writer.field("iRiv", "N")
    writer.field("iEle", "N")
    writer.field("Length", "F", decimal=3)
    writer.line([[[100.1, 30.1], [100.5, 30.4]]])
    writer.record(1, 1, 100.0)
    writer.line([[[100.5, 30.4], [100.8, 30.8]]])
    writer.record(2, 2, 120.0)
    writer.close()
    _write_wgs84_prj(base.with_suffix(".prj"), containment_root=containment_root)
    if temp_dir is not None:
        try:
            _copy_fixture_shapefile_outputs(target_base, base, containment_root=containment_root)
        finally:
            temp_dir.cleanup()


def _write_wgs84_prj(path: Path, *, containment_root: Path | None = None) -> None:
    _safe_fixture_write_text(
        path,
        'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
        'SPHEROID["WGS_1984",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["Degree",0.0174532925199433]]\n',
        containment_root=containment_root,
    )


def _copy_fixture_shapefile_outputs(source_base: Path, target_base: Path, *, containment_root: Path) -> None:
    for suffix in (".shp", ".shx", ".dbf"):
        source_path = source_base.with_suffix(suffix)
        target_path = target_base.with_suffix(suffix)
        try:
            content = source_path.read_bytes()
        except OSError as error:
            raise ProductionObjectStoreValidationError(
                "PRODUCTION_OBJECT_STORE_EVIDENCE_WRITE_FAILED",
                f"Failed to read temporary synthetic Basins shapefile output {source_path}: {error}",
            ) from error
        _safe_fixture_write_bytes(target_path, content, containment_root=containment_root)
