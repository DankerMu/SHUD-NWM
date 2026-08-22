from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]

# (script, shapefile basename) — the container-depth rule must behave identically in both builders.
GEO_SCRIPTS = [
    ("scripts/geo/build_national_domain_geo.py", "domain.shp"),
    ("scripts/geo/build_national_river_geo.py", "river.shp"),
]


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


@pytest.mark.parametrize(("script_path", "shp_name"), GEO_SCRIPTS)
def test_discovery_depth_one_keeps_root_directory_name(script_path: str, shp_name: str, tmp_path: Path) -> None:
    script = _load_script(script_path)
    _touch(tmp_path / "hetianhe" / "input" / "hetian9000-2" / "gis" / shp_name)

    assert script._discover_basin_gis_dirs(tmp_path) == [
        ("hetianhe", tmp_path / "hetianhe" / "input" / "hetian9000-2" / "gis"),
    ]


@pytest.mark.parametrize(("script_path", "shp_name"), GEO_SCRIPTS)
def test_discovery_keeps_zhaochen_child_name(script_path: str, shp_name: str, tmp_path: Path) -> None:
    """Regression lock: the already-published geojson ids for zhaochen must not shift."""
    script = _load_script(script_path)
    _touch(tmp_path / "zhaochen" / "BST" / "input" / "BST" / "gis" / shp_name)

    assert script._discover_basin_gis_dirs(tmp_path) == [
        ("zhaochen_bst", tmp_path / "zhaochen" / "BST" / "input" / "BST" / "gis"),
    ]
    assert script._basin_id("zhaochen_bst") == "basins_zhaochen_bst"


@pytest.mark.parametrize(("script_path", "shp_name"), GEO_SCRIPTS)
def test_discovery_folds_any_depth_two_container(script_path: str, shp_name: str, tmp_path: Path) -> None:
    """Depth-2 folding is derived from where input/ sits, not from a hard-coded container name."""
    script = _load_script(script_path)
    _touch(tmp_path / "HYS" / "BST" / "input" / "BST" / "gis" / shp_name)

    assert script._discover_basin_gis_dirs(tmp_path) == [
        ("HYS_bst", tmp_path / "HYS" / "BST" / "input" / "BST" / "gis"),
    ]
    assert script._basin_id("HYS_bst") == "basins_hys_bst"


@pytest.mark.parametrize(("script_path", "shp_name"), GEO_SCRIPTS)
def test_discovery_keeps_siblings_of_one_container_distinct(
    script_path: str, shp_name: str, tmp_path: Path
) -> None:
    """Two children of one non-zhaochen container must not collapse onto the container name.

    On collapse both entries are named ``HYS``; main() merges discovery results into a dict keyed
    by basin name, so one basin would silently overwrite the other and the published layer would
    carry one geometry under a single basin_id instead of two.
    """
    script = _load_script(script_path)
    _touch(tmp_path / "HYS" / "BST" / "input" / "BST" / "gis" / shp_name)
    _touch(tmp_path / "HYS" / "MC" / "input" / "MC" / "gis" / shp_name)

    discovered = script._discover_basin_gis_dirs(tmp_path)

    names = [name for name, _ in discovered]
    assert len(set(names)) == 2, f"sibling basins collapsed onto one name: {names}"
    assert discovered == [
        ("HYS_bst", tmp_path / "HYS" / "BST" / "input" / "BST" / "gis"),
        ("HYS_mc", tmp_path / "HYS" / "MC" / "input" / "MC" / "gis"),
    ]


@pytest.mark.parametrize(("script_path", "shp_name"), GEO_SCRIPTS)
def test_discovery_skips_depth_three_containers(script_path: str, shp_name: str, tmp_path: Path) -> None:
    """Deeper nesting has no registry identity: skip it instead of attributing it to parts[0]."""
    script = _load_script(script_path)
    _touch(tmp_path / "hetianhe" / "input" / "hetian9000-2" / "gis" / shp_name)
    _touch(tmp_path / "a" / "b" / "c" / "input" / "c" / "gis" / shp_name)

    discovered = script._discover_basin_gis_dirs(tmp_path)

    assert discovered == [
        ("hetianhe", tmp_path / "hetianhe" / "input" / "hetian9000-2" / "gis"),
    ]
    assert "a" not in [name for name, _ in discovered]
