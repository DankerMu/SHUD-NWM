from __future__ import annotations

from pathlib import Path

import pytest

from scripts.geo import build_national_domain_geo as domain_geo
from scripts.geo import build_national_river_geo as river_geo


@pytest.mark.parametrize(
    ("module", "filename"),
    ((domain_geo, "domain.shp"), (river_geo, "river.shp")),
)
def test_model_package_discovery_maps_model_id_to_basin_name(
    tmp_path: Path,
    module: object,
    filename: str,
) -> None:
    gis = tmp_path / "basins_hhe_shud" / "v1" / "package" / "gis"
    gis.mkdir(parents=True)
    stem = Path(filename).stem
    for suffix in (".shp", ".shx", ".dbf", ".prj"):
        (gis / f"{stem}{suffix}").write_bytes(f"shape-{suffix}".encode())

    discovered = module._discover_model_package_gis_dirs(tmp_path)  # type: ignore[attr-defined]

    assert discovered == [("hhe", gis)]


@pytest.mark.parametrize(
    ("module", "filename"),
    ((domain_geo, "domain.shp"), (river_geo, "river.shp")),
)
def test_model_package_resolution_accepts_identical_source_variants(
    tmp_path: Path,
    module: object,
    filename: str,
) -> None:
    stem = Path(filename).stem
    expected = None
    for version in ("v1", "v2"):
        gis = tmp_path / "basins_hhe_shud" / version / "package" / "gis"
        gis.mkdir(parents=True)
        for suffix in (".shp", ".shx", ".dbf", ".prj"):
            (gis / f"{stem}{suffix}").write_bytes(f"shape-{suffix}".encode())
        expected = expected or gis

    assert module._named_model_package_gis_dir(tmp_path, "hhe") == expected  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("module", "filename"),
    ((domain_geo, "domain.shp"), (river_geo, "river.shp")),
)
def test_model_package_resolution_refuses_distinct_versions(
    tmp_path: Path,
    module: object,
    filename: str,
) -> None:
    stem = Path(filename).stem
    for version in ("v1", "v2"):
        gis = tmp_path / "basins_hhe_shud" / version / "package" / "gis"
        gis.mkdir(parents=True)
        for suffix in (".shp", ".shx", ".dbf", ".prj"):
            payload = f"{version}-{suffix}" if suffix == ".shp" else f"shape-{suffix}"
            (gis / f"{stem}{suffix}").write_bytes(payload.encode())

    with pytest.raises(ValueError, match="ambiguous model packages for hhe"):
        module._named_model_package_gis_dir(tmp_path, "hhe")  # type: ignore[attr-defined]
