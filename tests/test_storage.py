import pytest

from packages.common.storage import VALID_PREFIX_PATTERNS, validate_object_path


@pytest.mark.parametrize(
    ("path", "category", "expected_components"),
    [
        (
            "raw/gfs/2026050100/gfs_t2m.grib2",
            "raw",
            {"source": "gfs", "cycle_time": "2026050100"},
        ),
        (
            "canonical/gfs/2026050100/t2m/data.nc",
            "canonical",
            {"source": "gfs", "cycle_time": "2026050100", "variable": "t2m"},
        ),
        (
            "forcing/gfs/2026050100/yangtze_v2026_01/yangtze_shud_v12/forcing.tar.gz",
            "forcing",
            {
                "source": "gfs",
                "cycle_time": "2026050100",
                "basin_version_id": "yangtze_v2026_01",
                "model_id": "yangtze_shud_v12",
            },
        ),
        (
            "models/yangtze_shud_v12/model_package.tar.gz",
            "models",
            {"model_id": "yangtze_shud_v12"},
        ),
        (
            "states/yangtze_shud_v12/2026050100/state.ic",
            "states",
            {"model_id": "yangtze_shud_v12", "valid_time": "2026050100"},
        ),
        (
            "runs/fcst_gfs_2026050100_yangtze_shud_v12/input/manifest.json",
            "runs",
            {"run_id": "fcst_gfs_2026050100_yangtze_shud_v12", "sub_prefix": "input"},
        ),
        (
            "runs/fcst_gfs_2026050100_yangtze_shud_v12/output/rivqdown.csv",
            "runs",
            {"run_id": "fcst_gfs_2026050100_yangtze_shud_v12", "sub_prefix": "output"},
        ),
        (
            "runs/fcst_gfs_2026050100_yangtze_shud_v12/logs/run.log",
            "runs",
            {"run_id": "fcst_gfs_2026050100_yangtze_shud_v12", "sub_prefix": "logs"},
        ),
        (
            "tiles/hydro/run123/tile.pbf",
            "tiles",
            {"tile_type": "hydro", "run_id": "run123"},
        ),
    ],
)
def test_validate_object_path_happy_paths(
    path: str,
    category: str,
    expected_components: dict[str, str],
) -> None:
    result = validate_object_path(path)

    assert result.valid is True
    assert result.category == category
    assert result.error is None
    assert result.components == expected_components


@pytest.mark.parametrize(
    "path",
    [
        "s3://nhms/raw/gfs/2026050100/file.grib2",
        "s3://other-bucket/raw/gfs/2026050100/file.grib2",
    ],
)
def test_validate_object_path_accepts_s3_uris(path: str) -> None:
    result = validate_object_path(path)

    assert result.valid is True
    assert result.category == "raw"
    assert result.components == {"source": "gfs", "cycle_time": "2026050100"}


@pytest.mark.parametrize(
    "path",
    [
        "data/gfs/something.grib2",
        "invalid/path",
        "forcing/gfs/file.tar.gz",
        "",
        "/",
    ],
)
def test_validate_object_path_errors(path: str) -> None:
    result = validate_object_path(path)

    assert result.valid is False
    assert result.category is None
    assert result.components == {}
    assert result.error is not None
    assert "Valid prefixes:" in result.error
    for pattern in VALID_PREFIX_PATTERNS:
        assert pattern.display in result.error


def test_validate_object_path_unknown_prefix_error_is_descriptive() -> None:
    result = validate_object_path("data/gfs/something.grib2")

    assert result.error is not None
    assert "Unrecognized object path prefix" in result.error
