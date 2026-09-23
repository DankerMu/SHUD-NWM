from pathlib import Path

import pytest

from packages.common.storage import (
    VALID_PREFIX_PATTERNS,
    validate_object_path,
)
from scripts import node27_raw_retention, node27_resource_governance


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


def test_raw_retention_object_store_override_precedence_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    override = tmp_path / "raw-override"
    shared.mkdir()
    override.mkdir()
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(shared))
    monkeypatch.setenv("NODE27_RAW_RETENTION_OBJECT_STORE_ROOT", str(override))

    config, blockers = node27_raw_retention.config_from_env(node27_raw_retention.build_parser().parse_args([]))

    assert blockers == []
    assert config is not None
    assert config.object_store_root == override.resolve()


def test_governance_object_store_override_precedence_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    override = tmp_path / "governance-override"
    monkeypatch.setenv("OBJECT_STORE_ROOT", str(shared))
    monkeypatch.setenv("NODE27_GOVERNANCE_OBJECT_STORE_ROOT", str(override))

    args = node27_resource_governance.build_parser().parse_args([])

    assert args.object_store_root == str(override)


# ---------------------------------------------------------------------------
# #2504 D6: the retention window has one parser shared by the retention runner
# and the display coverage refresh.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("21", 21, id="plain"),
        pytest.param("1", 1, id="minimum"),
        pytest.param(None, None, id="absent"),
        pytest.param("", None, id="empty"),
        pytest.param(" 21", None, id="leading-space"),
        pytest.param("21 ", None, id="trailing-space"),
        pytest.param("0", None, id="zero"),
        pytest.param("-1", None, id="negative"),
        pytest.param("21d", None, id="suffix"),
        pytest.param("2.5", None, id="fraction"),
    ],
)
def test_configured_retention_window_days_is_none_unless_the_runner_would_accept_it(
    raw: str | None, expected: int | None
) -> None:
    from packages.common.storage import RETENTION_WINDOW_ENV, configured_retention_window_days

    env = {} if raw is None else {RETENTION_WINDOW_ENV: raw}

    assert configured_retention_window_days(env) == expected


def test_configured_retention_window_days_has_no_default() -> None:
    """Fail-closed by contract: absent is ``None``, never
    ``DEFAULT_RETENTION_WINDOW_DAYS`` — the coverage refresh must not relax its
    guard for a window the retention runner's own env does not state (D6)."""
    from packages.common.storage import DEFAULT_RETENTION_WINDOW_DAYS, configured_retention_window_days

    assert configured_retention_window_days({}) is None
    assert configured_retention_window_days({"UNRELATED": str(DEFAULT_RETENTION_WINDOW_DAYS)}) is None
