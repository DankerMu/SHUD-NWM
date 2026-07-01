from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load_script(path: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(Path(path).stem, ROOT / path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def test_national_domain_geo_discovers_nested_basins_root_layout(tmp_path: Path) -> None:
    script = _load_script("scripts/geo/build_national_domain_geo.py")
    _touch(tmp_path / "hetianhe" / "input" / "hetian9000-2" / "gis" / "domain.shp")
    _touch(tmp_path / "zhaochen" / "BST" / "input" / "BST" / "gis" / "domain.shp")

    discovered = script._discover_basin_gis_dirs(tmp_path)

    assert discovered == [
        ("hetianhe", tmp_path / "hetianhe" / "input" / "hetian9000-2" / "gis"),
        ("zhaochen_bst", tmp_path / "zhaochen" / "BST" / "input" / "BST" / "gis"),
    ]
    assert script._named_basin_gis_dir(tmp_path, "hetianhe") == tmp_path / "hetianhe" / "input" / "hetian9000-2" / "gis"
    assert script._named_basin_gis_dir(tmp_path, "zhaochen_bst") == (
        tmp_path / "zhaochen" / "BST" / "input" / "BST" / "gis"
    )


def test_national_river_geo_discovers_nested_basins_root_layout(tmp_path: Path) -> None:
    script = _load_script("scripts/geo/build_national_river_geo.py")
    _touch(tmp_path / "qinyijiang" / "input" / "nanlin" / "gis" / "river.shp")
    _touch(tmp_path / "zhaochen" / "WEM" / "input" / "WEM" / "gis" / "river.shp")

    discovered = script._discover_basin_gis_dirs(tmp_path)

    assert discovered == [
        ("qinyijiang", tmp_path / "qinyijiang" / "input" / "nanlin" / "gis"),
        ("zhaochen_wem", tmp_path / "zhaochen" / "WEM" / "input" / "WEM" / "gis"),
    ]
    assert script._named_basin_gis_dir(tmp_path, "qinyijiang") == tmp_path / "qinyijiang" / "input" / "nanlin" / "gis"
    assert script._named_basin_gis_dir(tmp_path, "zhaochen_wem") == (
        tmp_path / "zhaochen" / "WEM" / "input" / "WEM" / "gis"
    )
