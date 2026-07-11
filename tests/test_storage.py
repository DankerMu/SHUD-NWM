from pathlib import Path

import pytest

from packages.common.storage import (
    VALID_PREFIX_PATTERNS,
    ArchiveConfigurationError,
    archive_provenance_paths,
    resolve_archive_root,
    resolve_archive_storage_config,
    validate_archive_configuration,
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


def test_resolve_archive_root_shared_and_per_script_precedence(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    override = tmp_path / "override"

    assert resolve_archive_root(env={"NHMS_ARCHIVE_ROOT": str(shared)}) == shared.resolve()
    assert resolve_archive_root(
        "product_archive",
        env={
            "NHMS_ARCHIVE_ROOT": str(shared),
            "NODE27_PRODUCT_ARCHIVE_ARCHIVE_ROOT": str(override),
        },
    ) == override.resolve()


@pytest.mark.parametrize("lane", ["forcing", "runs", "states"])
def test_archive_provenance_paths_are_deterministic(tmp_path: Path, lane: str) -> None:
    first = archive_provenance_paths(
        tmp_path / "archive",
        lane=lane,
        cycle_identity="2026071100",
        scope_components=("basin-v1", "run-42"),
    )
    second = archive_provenance_paths(
        tmp_path / "archive",
        lane=lane,
        cycle_identity="2026071100",
        scope_components=("basin-v1", "run-42"),
    )

    expected_parent = (tmp_path / "archive" / lane / "2026071100" / "basin-v1" / "run-42").resolve()
    assert first == second
    assert first.archive == expected_parent / "archive.tar.zst"
    assert first.manifest == expected_parent / "manifest.json"


@pytest.mark.parametrize(
    ("lane", "cycle", "scope"),
    [
        ("raw", "2026071100", ()),
        ("forcing", "", ()),
        ("forcing", "   ", ()),
        ("forcing", ".", ()),
        ("forcing", "..", ()),
        ("runs", "2026071100", ("a/b",)),
        ("runs", "2026071100", ("a\\b",)),
        ("states", "/absolute", ()),
    ],
)
def test_archive_provenance_rejects_unsafe_identity_before_root_resolution(
    monkeypatch: pytest.MonkeyPatch,
    lane: str,
    cycle: str,
    scope: tuple[str, ...],
) -> None:
    def unexpected_resolve(*args: object, **kwargs: object) -> None:
        raise AssertionError("filesystem resolution must not happen for invalid identity")

    monkeypatch.setattr(Path, "resolve", unexpected_resolve)
    with pytest.raises(ArchiveConfigurationError, match="archive lane|unsafe archive identity"):
        archive_provenance_paths("/unused", lane=lane, cycle_identity=cycle, scope_components=scope)


@pytest.mark.parametrize("relation", ["equal", "archive-parent", "cleanup-parent"])
def test_validate_archive_configuration_rejects_all_overlap_directions(
    tmp_path: Path,
    relation: str,
) -> None:
    base = tmp_path / "data"
    archive = base if relation != "cleanup-parent" else base / "archive"
    cleanup = base if relation != "archive-parent" else base / "cleanup"

    with pytest.raises(ArchiveConfigurationError) as error:
        validate_archive_configuration(
            archive_root=archive,
            cleanup_roots={"raw-retention": cleanup, "other-cleanup": tmp_path / "other"},
        )

    message = str(error.value)
    assert "raw-retention" in message
    assert f"archive_root={archive.resolve()}" in message
    assert f"cleanup_root={cleanup.resolve()}" in message


def test_validate_archive_configuration_normalizes_aliases_and_symlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    target = home / "shared"
    target.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(ArchiveConfigurationError, match="overlaps cleanup root rotation"):
        validate_archive_configuration(
            archive_root="~/shared/../shared/archive",
            cleanup_roots={"rotation": alias},
        )


def test_archive_configuration_requires_explicit_cleanup_set(tmp_path: Path) -> None:
    with pytest.raises(ArchiveConfigurationError, match="explicitly contain every cleanup"):
        validate_archive_configuration(archive_root=tmp_path / "archive", cleanup_roots={})


def test_resolve_archive_storage_config_rejects_minimum_age_below_retention(tmp_path: Path) -> None:
    with pytest.raises(ArchiveConfigurationError, match="20 days is below DB retention 30 days"):
        resolve_archive_storage_config(
            cleanup_roots={"raw": tmp_path / "object-store"},
            env={
                "NHMS_ARCHIVE_ROOT": str(tmp_path / "archive"),
                "NHMS_ARCHIVE_MIN_AGE_DAYS": "20",
            },
        )


def test_resolve_archive_storage_config_uses_default_age(tmp_path: Path) -> None:
    config = resolve_archive_storage_config(
        cleanup_roots={"raw": tmp_path / "object-store"},
        env={"NHMS_ARCHIVE_ROOT": str(tmp_path / "archive")},
    )

    assert config.archive_min_age_days == 45
    assert config.retention_days == 30


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
