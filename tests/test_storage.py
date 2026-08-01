import shutil
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from packages.common.storage import (
    DEFAULT_RETENTION_WINDOW_DAYS,
    RETENTION_ENV_PATH_VARIABLE,
    VALID_PREFIX_PATTERNS,
    ArchiveConfigurationError,
    ArchiveIdentity,
    archive_identity_for_state_reference,
    archive_provenance_paths,
    read_retention_window_days,
    resolve_archive_root,
    resolve_archive_storage_config,
    validate_archive_configuration,
    validate_object_path,
    validate_product_archive_manifest_binding,
)
from scripts import node27_raw_retention, node27_resource_governance, node27_timeseries_retention


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


@pytest.mark.parametrize(
    ("script_name", "env", "error_fragment"),
    [
        (None, {"NHMS_ARCHIVE_ROOT": "relative/archive"}, "archive root must be absolute"),
        (
            "product_archive",
            {
                "NHMS_ARCHIVE_ROOT": "/absolute/shared",
                "NODE27_PRODUCT_ARCHIVE_ARCHIVE_ROOT": "relative/override",
            },
            "archive root must be absolute",
        ),
    ],
)
def test_resolve_archive_root_rejects_relative_shared_and_override(
    script_name: str | None,
    env: dict[str, str],
    error_fragment: str,
) -> None:
    with pytest.raises(ArchiveConfigurationError, match=error_fragment):
        resolve_archive_root(script_name, env=env)


@pytest.mark.parametrize(
    ("identity", "relative_parent"),
    [
        (
            ArchiveIdentity(
                lane="forcing",
                source="gfs",
                cycle_identity="2026071100",
                cycle_time="2026-07-11T00:00:00Z",
                basin_version_id="basin-v1",
                model_id="model-v1",
            ),
            Path("forcing/gfs/2026071100/basin-v1/model-v1"),
        ),
        (
            ArchiveIdentity(
                lane="runs",
                source="gfs",
                cycle_identity="2026071100",
                cycle_time="2026-07-11T00:00:00Z",
                run_id="run-42",
            ),
            Path("runs/gfs/2026071100/run-42"),
        ),
        (
            ArchiveIdentity(
                lane="states",
                source="gfs",
                cycle_identity="2026071100",
                cycle_time="2026-07-11T00:00:00Z",
                model_id="model-v1",
            ),
            Path("states/gfs/2026071100/model-v1"),
        ),
    ],
)
def test_archive_provenance_paths_use_canonical_lane_identity(
    tmp_path: Path,
    identity: ArchiveIdentity,
    relative_parent: Path,
) -> None:
    first = archive_provenance_paths(tmp_path / "archive", identity=identity)
    second = archive_provenance_paths(tmp_path / "archive", identity=identity)

    expected_parent = (tmp_path / "archive" / relative_parent).resolve()
    assert first == second
    assert first.archive == expected_parent / "archive.tar.zst"
    assert first.manifest == expected_parent / "manifest.json"


def test_archive_provenance_distinguishes_sources(tmp_path: Path) -> None:
    common = {
        "lane": "forcing",
        "cycle_identity": "2026071100",
        "cycle_time": "2026-07-11T00:00:00Z",
        "basin_version_id": "basin-v1",
        "model_id": "model-v1",
    }

    gfs = archive_provenance_paths(tmp_path / "archive", identity=ArchiveIdentity(source="gfs", **common))
    ifs = archive_provenance_paths(tmp_path / "archive", identity=ArchiveIdentity(source="ifs", **common))

    assert gfs != ifs
    assert "/forcing/gfs/" in gfs.archive.as_posix()
    assert "/forcing/ifs/" in ifs.archive.as_posix()


@pytest.mark.parametrize(
    ("alias", "canonical_source", "source_segment"),
    [
        ("GFS", "gfs", "gfs"),
        ("era5", "ERA5", "era5"),
        ("IfS", "IFS", "ifs"),
    ],
)
def test_archive_identity_normalizes_shared_source_aliases_and_path_segments(
    tmp_path: Path,
    alias: str,
    canonical_source: str,
    source_segment: str,
) -> None:
    identity = ArchiveIdentity(
        lane="runs",
        source=alias,
        cycle_identity="2026071100",
        cycle_time="2026-07-11T00:00:00Z",
        run_id="run-42",
    )

    paths = archive_provenance_paths(tmp_path / "archive", identity=identity)
    canonical_identity = ArchiveIdentity(
        lane="runs",
        source=canonical_source,
        cycle_identity="2026071100",
        cycle_time="2026-07-11T00:00:00Z",
        run_id="run-42",
    )
    canonical_paths = archive_provenance_paths(tmp_path / "archive", identity=canonical_identity)

    assert identity.source == canonical_source
    assert identity == canonical_identity
    assert paths == canonical_paths
    assert f"/runs/{source_segment}/2026071100/" in paths.archive.as_posix()


def test_archive_identity_rejects_unknown_source_before_root_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_resolve(*args: object, **kwargs: object) -> None:
        raise AssertionError("filesystem resolution must not happen for an unknown source")

    monkeypatch.setattr(Path, "resolve", unexpected_resolve)
    with pytest.raises(ArchiveConfigurationError, match="invalid archive source"):
        ArchiveIdentity(
            lane="runs",
            source="unknown-provider",
            cycle_identity="2026071100",
            cycle_time="2026-07-11T00:00:00Z",
            run_id="run-42",
        )


def test_legacy_unqualified_state_identity_has_deterministic_reserved_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_provider_normalization(source: str) -> str:
        raise AssertionError(f"legacy source must not use provider normalization: {source}")

    monkeypatch.setattr("packages.common.storage.normalize_source_id", unexpected_provider_normalization)
    identity = ArchiveIdentity(
        lane="states",
        source="legacy-unqualified",
        cycle_identity="2026071100",
        cycle_time="2026-07-11T00:00:00Z",
        model_id="model-v1",
    )

    paths = archive_provenance_paths(tmp_path / "archive", identity=identity)

    expected_parent = (tmp_path / "archive/states/legacy-unqualified/2026071100/model-v1").resolve()
    assert paths.archive == expected_parent / "archive.tar.zst"
    assert paths.manifest == expected_parent / "manifest.json"


@pytest.mark.parametrize(
    "identity_mapping",
    [
        {
            "lane": "forcing",
            "source": "legacy-unqualified",
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T00:00:00Z",
            "basin_version_id": "basin-v1",
            "model_id": "model-v1",
        },
        {
            "lane": "runs",
            "source": "legacy-unqualified",
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T00:00:00Z",
            "run_id": "run-42",
        },
    ],
)
def test_legacy_unqualified_source_is_forbidden_outside_states(identity_mapping: dict[str, str]) -> None:
    with pytest.raises(ArchiveConfigurationError, match="reserved for the states lane"):
        ArchiveIdentity.from_mapping(identity_mapping)


def test_legacy_unqualified_and_provider_state_paths_do_not_collide(tmp_path: Path) -> None:
    common = {
        "lane": "states",
        "cycle_identity": "2026071100",
        "cycle_time": "2026-07-11T00:00:00Z",
        "model_id": "model-v1",
    }

    legacy = archive_provenance_paths(
        tmp_path / "archive",
        identity=ArchiveIdentity(source="legacy-unqualified", **common),
    )
    providers = [
        archive_provenance_paths(tmp_path / "archive", identity=ArchiveIdentity(source=source, **common))
        for source in ("gfs", "ERA5", "IFS")
    ]

    assert all(legacy != provider for provider in providers)
    assert "/states/legacy-unqualified/" in legacy.archive.as_posix()
    assert {provider.archive.parts[-4] for provider in providers} == {"gfs", "era5", "ifs"}


@pytest.mark.parametrize("source_id", [None, ""])
def test_state_reference_factory_maps_unqualified_source_to_exact_legacy_identity_and_path(
    tmp_path: Path,
    source_id: str | None,
) -> None:
    identity = archive_identity_for_state_reference(
        source_id=source_id,
        model_id="model-v1",
        valid_time=datetime(2026, 7, 11, tzinfo=UTC),
    )

    paths = archive_provenance_paths(tmp_path / "archive", identity=identity)

    assert identity == ArchiveIdentity(
        lane="states",
        source="legacy-unqualified",
        cycle_identity="2026071100",
        cycle_time="2026-07-11T00:00:00Z",
        model_id="model-v1",
    )
    assert paths.archive == (
        tmp_path / "archive/states/legacy-unqualified/2026071100/model-v1/archive.tar.zst"
    ).resolve()


@pytest.mark.parametrize(
    ("source_alias", "canonical_source", "source_segment"),
    [("GFS", "gfs", "gfs"), ("era5", "ERA5", "era5"), ("IfS", "IFS", "ifs")],
)
def test_state_reference_factory_normalizes_provider_alias_and_lowercase_path(
    tmp_path: Path,
    source_alias: str,
    canonical_source: str,
    source_segment: str,
) -> None:
    identity = archive_identity_for_state_reference(
        source_id=source_alias,
        model_id="model-v1",
        valid_time=datetime(2026, 7, 11, tzinfo=UTC),
    )

    paths = archive_provenance_paths(tmp_path / "archive", identity=identity)

    assert identity.source == canonical_source
    assert f"/states/{source_segment}/2026071100/model-v1/" in paths.archive.as_posix()


def test_state_reference_factory_normalizes_equivalent_aware_hour_to_utc() -> None:
    utc_identity = archive_identity_for_state_reference(
        source_id="gfs",
        model_id="model-v1",
        valid_time=datetime(2026, 7, 11, tzinfo=UTC),
    )
    offset_identity = archive_identity_for_state_reference(
        source_id="gfs",
        model_id="model-v1",
        valid_time=datetime(2026, 7, 11, 8, tzinfo=timezone(timedelta(hours=8))),
    )

    assert offset_identity == utc_identity


@pytest.mark.parametrize(
    ("source_id", "model_id", "valid_time", "error_fragment"),
    [
        ("gfs", "model-v1", "2026-07-11T00:00:00Z", "must be a datetime"),
        ("gfs", "model-v1", datetime(2026, 7, 11), "timezone-aware"),
        ("gfs", "model-v1", datetime(2026, 7, 11, 0, 1, tzinfo=UTC), "UTC hourly instant"),
        ("unknown-provider", "model-v1", datetime(2026, 7, 11, tzinfo=UTC), "invalid archive source"),
        (" ", "model-v1", datetime(2026, 7, 11, tzinfo=UTC), "unsafe archive identity component"),
        (
            "legacy-unqualified",
            "model-v1",
            datetime(2026, 7, 11, tzinfo=UTC),
            "derived only from source_id None or an empty string",
        ),
        ("gfs", "../unsafe", datetime(2026, 7, 11, tzinfo=UTC), "unsafe archive identity component"),
    ],
)
def test_state_reference_factory_rejects_invalid_time_source_or_model(
    source_id: str | None,
    model_id: str,
    valid_time: object,
    error_fragment: str,
) -> None:
    with pytest.raises(ArchiveConfigurationError, match=error_fragment):
        archive_identity_for_state_reference(
            source_id=source_id,
            model_id=model_id,
            valid_time=valid_time,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("source_id", [None, "", "GFS", "era5", "IfS"])
def test_state_reference_factory_round_trips_through_strict_manifest_binding(
    tmp_path: Path,
    source_id: str | None,
) -> None:
    identity = archive_identity_for_state_reference(
        source_id=source_id,
        model_id="model-v1",
        valid_time=datetime(2026, 7, 11, tzinfo=UTC),
    )
    paths = archive_provenance_paths(tmp_path / "archive", identity=identity)
    root = (tmp_path / "archive").resolve()
    manifest = _product_manifest(
        {
            "lane": identity.lane,
            "source": identity.source,
            "cycle_identity": identity.cycle_identity,
            "cycle_time": identity.cycle_time,
            "model_id": identity.model_id or "",
        },
        paths.archive.relative_to(root).as_posix(),
        paths.manifest.relative_to(root).as_posix(),
    )

    bound = validate_product_archive_manifest_binding(root, manifest)

    assert bound == paths


def test_state_reference_factory_keeps_legacy_and_provider_namespaces_disjoint(tmp_path: Path) -> None:
    common = {"model_id": "model-v1", "valid_time": datetime(2026, 7, 11, tzinfo=UTC)}
    legacy = archive_provenance_paths(
        tmp_path / "archive",
        identity=archive_identity_for_state_reference(source_id=None, **common),
    )
    provider = archive_provenance_paths(
        tmp_path / "archive",
        identity=archive_identity_for_state_reference(source_id="gfs", **common),
    )

    assert legacy != provider
    assert "/states/legacy-unqualified/" in legacy.archive.as_posix()
    assert "/states/gfs/" in provider.archive.as_posix()


@pytest.mark.parametrize(
    "identity_mapping",
    [
        {
            "lane": "raw",
            "source": "gfs",
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T00:00:00Z",
        },
        {
            "lane": "forcing",
            "source": "",
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T00:00:00Z",
            "basin_version_id": "basin-v1",
            "model_id": "model-v1",
        },
        {
            "lane": "forcing",
            "source": "gfs/../../ifs",
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T00:00:00Z",
            "basin_version_id": "basin-v1",
            "model_id": "model-v1",
        },
        {
            "lane": "forcing",
            "source": "gfs",
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T00:00:00Z",
            "model_id": "model-v1",
        },
        {
            "lane": "runs",
            "source": "gfs",
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T00:00:00Z",
            "model_id": "model-v1",
        },
        {
            "lane": "states",
            "source": "gfs",
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T00:00:00Z",
            "model_id": "model-v1",
            "run_id": "run-42",
        },
    ],
)
def test_archive_identity_rejects_unsafe_missing_or_cross_lane_fields_before_root_resolution(
    monkeypatch: pytest.MonkeyPatch,
    identity_mapping: dict[str, str],
) -> None:
    def unexpected_resolve(*args: object, **kwargs: object) -> None:
        raise AssertionError("filesystem resolution must not happen for invalid identity")

    monkeypatch.setattr(Path, "resolve", unexpected_resolve)
    with pytest.raises(ArchiveConfigurationError):
        identity = ArchiveIdentity.from_mapping(identity_mapping)
        archive_provenance_paths("/unused", identity=identity)


@pytest.mark.parametrize(
    "cycle_fields",
    [
        {},
        {"cycle_time": "not-a-time"},
        {"cycle_time": "2026-07-11T08:00:00+08:00"},
        {"cycle_time": "2026-07-11T06:00:00Z"},
    ],
)
def test_archive_identity_rejects_missing_invalid_non_utc_or_mismatched_cycle_time_before_root_resolution(
    monkeypatch: pytest.MonkeyPatch,
    cycle_fields: dict[str, str],
) -> None:
    identity_mapping = {
        "lane": "runs",
        "source": "gfs",
        "cycle_identity": "2026071100",
        "run_id": "run-42",
        **cycle_fields,
    }

    def unexpected_resolve(*args: object, **kwargs: object) -> None:
        raise AssertionError("filesystem resolution must not happen for invalid time identity")

    monkeypatch.setattr(Path, "resolve", unexpected_resolve)
    with pytest.raises(ArchiveConfigurationError, match="cycle_time"):
        identity = ArchiveIdentity.from_mapping(identity_mapping)
        archive_provenance_paths("/unused", identity=identity)


def _product_manifest(identity: dict[str, str], archive_path: str, manifest_path: str) -> dict[str, object]:
    return {
        "identity": identity,
        "archive": {"path": archive_path, "manifest_path": manifest_path},
    }


def test_product_manifest_binding_accepts_canonical_identity_and_siblings(tmp_path: Path) -> None:
    relative_parent = "forcing/gfs/2026071100/basin-v1/model-v1"
    manifest = _product_manifest(
        {
            "lane": "forcing",
            "source": "gfs",
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T00:00:00Z",
            "basin_version_id": "basin-v1",
            "model_id": "model-v1",
        },
        f"{relative_parent}/archive.tar.zst",
        f"{relative_parent}/manifest.json",
    )

    paths = validate_product_archive_manifest_binding(tmp_path / "archive", manifest)

    assert paths.archive == (tmp_path / "archive" / relative_parent / "archive.tar.zst").resolve()
    assert paths.manifest == (tmp_path / "archive" / relative_parent / "manifest.json").resolve()


@pytest.mark.parametrize("source", ["gfs", "ERA5", "IFS"])
def test_product_manifest_binding_accepts_each_canonical_source_id(tmp_path: Path, source: str) -> None:
    source_segment = source.lower()
    relative_parent = f"runs/{source_segment}/2026071100/run-42"
    manifest = _product_manifest(
        {
            "lane": "runs",
            "source": source,
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T00:00:00Z",
            "run_id": "run-42",
        },
        f"{relative_parent}/archive.tar.zst",
        f"{relative_parent}/manifest.json",
    )

    paths = validate_product_archive_manifest_binding(tmp_path / "archive", manifest)

    assert paths.archive == (tmp_path / "archive" / relative_parent / "archive.tar.zst").resolve()


@pytest.mark.parametrize("source", ["GFS", "era5", "ifs", "IfS", "unknown-provider"])
def test_product_manifest_binding_rejects_alias_or_unknown_source_id(tmp_path: Path, source: str) -> None:
    source_segment = source.lower()
    relative_parent = f"runs/{source_segment}/2026071100/run-42"
    manifest = _product_manifest(
        {
            "lane": "runs",
            "source": source,
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T00:00:00Z",
            "run_id": "run-42",
        },
        f"{relative_parent}/archive.tar.zst",
        f"{relative_parent}/manifest.json",
    )

    with pytest.raises(ArchiveConfigurationError, match="product archive manifest"):
        validate_product_archive_manifest_binding(tmp_path / "archive", manifest)


def test_product_manifest_binding_accepts_canonical_legacy_unqualified_state(tmp_path: Path) -> None:
    relative_parent = "states/legacy-unqualified/2026071100/model-v1"
    manifest = _product_manifest(
        {
            "lane": "states",
            "source": "legacy-unqualified",
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T00:00:00Z",
            "model_id": "model-v1",
        },
        f"{relative_parent}/archive.tar.zst",
        f"{relative_parent}/manifest.json",
    )

    paths = validate_product_archive_manifest_binding(tmp_path / "archive", manifest)

    assert paths.archive == (tmp_path / "archive" / relative_parent / "archive.tar.zst").resolve()


@pytest.mark.parametrize(
    ("identity", "relative_parent"),
    [
        (
            {
                "lane": "forcing",
                "source": "legacy-unqualified",
                "cycle_identity": "2026071100",
                "cycle_time": "2026-07-11T00:00:00Z",
                "basin_version_id": "basin-v1",
                "model_id": "model-v1",
            },
            "forcing/legacy-unqualified/2026071100/basin-v1/model-v1",
        ),
        (
            {
                "lane": "runs",
                "source": "legacy-unqualified",
                "cycle_identity": "2026071100",
                "cycle_time": "2026-07-11T00:00:00Z",
                "run_id": "run-42",
            },
            "runs/legacy-unqualified/2026071100/run-42",
        ),
    ],
)
def test_product_manifest_binding_rejects_legacy_unqualified_non_state_lane(
    tmp_path: Path,
    identity: dict[str, str],
    relative_parent: str,
) -> None:
    manifest = _product_manifest(
        identity,
        f"{relative_parent}/archive.tar.zst",
        f"{relative_parent}/manifest.json",
    )

    with pytest.raises(ArchiveConfigurationError, match="reserved for the states lane"):
        validate_product_archive_manifest_binding(tmp_path / "archive", manifest)


@pytest.mark.parametrize(
    ("source", "path_source"),
    [("legacy-unqualified", "gfs"), ("gfs", "legacy-unqualified")],
)
def test_state_manifest_binding_rejects_legacy_provider_inference_drift(
    tmp_path: Path,
    source: str,
    path_source: str,
) -> None:
    relative_parent = f"states/{path_source}/2026071100/model-v1"
    manifest = _product_manifest(
        {
            "lane": "states",
            "source": source,
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T00:00:00Z",
            "model_id": "model-v1",
        },
        f"{relative_parent}/archive.tar.zst",
        f"{relative_parent}/manifest.json",
    )

    with pytest.raises(ArchiveConfigurationError, match="canonical identity"):
        validate_product_archive_manifest_binding(tmp_path / "archive", manifest)


def test_product_manifest_binding_rejects_drifting_cycle_time_identity(tmp_path: Path) -> None:
    relative_parent = "runs/gfs/2026071100/run-42"
    manifest = _product_manifest(
        {
            "lane": "runs",
            "source": "gfs",
            "cycle_identity": "2026071100",
            "cycle_time": "2026-07-11T06:00:00Z",
            "run_id": "run-42",
        },
        f"{relative_parent}/archive.tar.zst",
        f"{relative_parent}/manifest.json",
    )

    with pytest.raises(ArchiveConfigurationError, match="cycle_time does not match cycle_identity"):
        validate_product_archive_manifest_binding(tmp_path / "archive", manifest)


@pytest.mark.parametrize("mismatch", ["identity", "archive-path", "manifest-sibling"])
def test_product_manifest_binding_rejects_identity_path_or_sibling_mismatch(
    tmp_path: Path,
    mismatch: str,
) -> None:
    identity = {
        "lane": "runs",
        "source": "gfs",
        "cycle_identity": "2026071100",
        "cycle_time": "2026-07-11T00:00:00Z",
        "run_id": "run-42",
    }
    archive_path = "runs/gfs/2026071100/run-42/archive.tar.zst"
    manifest_path = "runs/gfs/2026071100/run-42/manifest.json"
    if mismatch == "identity":
        identity["run_id"] = "run-43"
    elif mismatch == "archive-path":
        archive_path = "runs/gfs/2026071100/run-43/archive.tar.zst"
    else:
        manifest_path = "runs/gfs/2026071100/run-43/manifest.json"

    with pytest.raises(ArchiveConfigurationError, match="canonical identity|canonical archive sibling"):
        validate_product_archive_manifest_binding(
            tmp_path / "archive",
            _product_manifest(identity, archive_path, manifest_path),
        )


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
            retention_days=14,
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
            retention_days=14,
        )


def test_validate_archive_configuration_rejects_relative_cleanup_root(tmp_path: Path) -> None:
    with pytest.raises(ArchiveConfigurationError, match="cleanup root raw-retention must be absolute"):
        validate_archive_configuration(
            archive_root=tmp_path / "archive",
            cleanup_roots={"raw-retention": "relative/object-store"},
            retention_days=14,
        )


def test_validate_archive_configuration_rejects_relative_archive_root(tmp_path: Path) -> None:
    with pytest.raises(ArchiveConfigurationError, match="archive root must be absolute"):
        validate_archive_configuration(
            archive_root="relative/archive",
            cleanup_roots={"raw-retention": tmp_path / "object-store"},
            retention_days=14,
        )


def test_validate_archive_configuration_canonicalizes_absolute_dotdot_path(tmp_path: Path) -> None:
    config = validate_archive_configuration(
        archive_root=tmp_path / "archive-parent" / ".." / "archive",
        cleanup_roots={"raw-retention": tmp_path / "object-store"},
        retention_days=14,
    )

    assert config.archive_root == (tmp_path / "archive").resolve()


def test_archive_provenance_rejects_relative_root_before_lookup() -> None:
    identity = ArchiveIdentity(
        lane="runs",
        source="gfs",
        cycle_identity="2026071100",
        cycle_time="2026-07-11T00:00:00Z",
        run_id="run-42",
    )

    with pytest.raises(ArchiveConfigurationError, match="archive root must be absolute"):
        archive_provenance_paths("relative/archive", identity=identity)


def test_archive_configuration_requires_explicit_cleanup_set(tmp_path: Path) -> None:
    with pytest.raises(ArchiveConfigurationError, match="explicitly contain every cleanup"):
        validate_archive_configuration(
            archive_root=tmp_path / "archive", cleanup_roots={}, retention_days=14
        )


def test_resolve_archive_storage_config_rejects_minimum_age_below_retention(tmp_path: Path) -> None:
    with pytest.raises(ArchiveConfigurationError, match="20 days is below DB retention 21 days"):
        resolve_archive_storage_config(
            cleanup_roots={"raw": tmp_path / "object-store"},
            retention_days=21,
            env={
                "NHMS_ARCHIVE_ROOT": str(tmp_path / "archive"),
                "NHMS_ARCHIVE_MIN_AGE_DAYS": "20",
            },
        )


def test_resolve_archive_storage_config_uses_default_age(tmp_path: Path) -> None:
    config = resolve_archive_storage_config(
        cleanup_roots={"raw": tmp_path / "object-store"},
        retention_days=14,
        env={"NHMS_ARCHIVE_ROOT": str(tmp_path / "archive")},
    )

    assert config.archive_min_age_days == 14
    assert config.retention_days == 14


def test_resolve_archive_storage_config_requires_explicit_retention_days(tmp_path: Path) -> None:
    """No defaulted guard input: a caller that forgets the live window cannot run."""
    with pytest.raises(TypeError, match="retention_days"):
        resolve_archive_storage_config(  # type: ignore[call-arg]
            cleanup_roots={"raw": tmp_path / "object-store"},
            env={"NHMS_ARCHIVE_ROOT": str(tmp_path / "archive")},
        )


def test_validate_archive_configuration_requires_explicit_retention_days(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="retention_days"):
        validate_archive_configuration(  # type: ignore[call-arg]
            archive_root=tmp_path / "archive",
            cleanup_roots={"raw": tmp_path / "object-store"},
        )


# ---------------------------------------------------------------------------
# #1227 — the min-age guard compares against the LIVE DB retention window.
# Helper seam: `read_retention_window_days` extracts one variable from the
# deployed retention env file with shell-source lexical semantics.
# ---------------------------------------------------------------------------

_WINDOW_VAR = "NODE27_TIMESERIES_RETENTION_WINDOW_DAYS"
_RETENTION_ENV_VAR = RETENTION_ENV_PATH_VARIABLE
_REPO_ROOT = Path(__file__).resolve().parents[1]

# The runner-equivalent default applies ONLY to a file that is recognizably the
# deployed retention env (#1227 design D1 round-1 amendment): at least one
# NODE27_TIMESERIES_RETENTION_* assignment accepted. This sibling is a real
# variable from infra/env/node27-timeseries-retention.example.
_SIBLING = "NODE27_TIMESERIES_RETENTION_PER_TICK_BOUND=5"


def _retention_env(tmp_path: Path, body: str, *, name: str = "retention.env") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


_LEXICAL_ROWS = [
    pytest.param(f"{_WINDOW_VAR}=21\n", 21, id="plain"),
    pytest.param(
        f"DATABASE_URL=postgresql://x\n{_SIBLING}\n",
        14,
        id="missing-assignment-uses-runner-default",
    ),
    pytest.param(f"{_WINDOW_VAR}=\n", 14, id="empty-value-uses-runner-default"),
    pytest.param(f'{_WINDOW_VAR}=""\n', 14, id="quoted-empty-uses-runner-default"),
    pytest.param(f"{_SIBLING}\n{_WINDOW_VAR}=\n", 14, id="sibling-plus-empty-value-defaults"),
    pytest.param(f"export {_WINDOW_VAR}=21\n", 21, id="export-prefix"),
    pytest.param(f'{_WINDOW_VAR}="21"\n', 21, id="double-quoted"),
    pytest.param(f"{_WINDOW_VAR}='21'\n", 21, id="single-quoted"),
    pytest.param(f"{_WINDOW_VAR}=21   # trailing comment\n", 21, id="trailing-comment"),
    pytest.param(f"   {_WINDOW_VAR}=21   \n", 21, id="surrounding-whitespace"),
    pytest.param(f"# {_WINDOW_VAR}=99\n{_WINDOW_VAR}=21\n", 21, id="full-line-comment-ignored"),
    pytest.param(
        f"# {_WINDOW_VAR}=99\n{_SIBLING}\n",
        14,
        id="only-commented-assignment-is-unassigned",
    ),
    pytest.param(f"{_WINDOW_VAR}_OLD=99\n{_WINDOW_VAR}=21\n", 21, id="near-name-decoy-ignored"),
    pytest.param(f"{_WINDOW_VAR}_OLD=99\n", 14, id="decoy-alone-is-unassigned"),
    pytest.param(f"{_WINDOW_VAR}=14\n{_WINDOW_VAR}=21\n", 21, id="last-assignment-wins"),
    pytest.param(f"{_WINDOW_VAR}=21\n{_WINDOW_VAR}=30\n", 30, id="last-assignment-wins-again"),
]


@pytest.mark.parametrize(("body", "expected"), _LEXICAL_ROWS)
def test_read_retention_window_days_lexical_forms_and_runner_defaults(
    tmp_path: Path, body: str, expected: int
) -> None:
    """Runner-equivalent resolution + the exact lexical forms pinned by design D1."""
    assert read_retention_window_days(_retention_env(tmp_path, body)) == expected


def test_missing_or_empty_assignment_resolves_to_the_shared_runner_default() -> None:
    """The default is the runner's live-effective value, not a comparison fallback."""
    assert DEFAULT_RETENTION_WINDOW_DAYS == 14


_PRESENT_INVALID_ROWS = [
    pytest.param(f"{_WINDOW_VAR}=not-an-int\n", "must be an integer", id="non-integer"),
    pytest.param(f"{_WINDOW_VAR}=0\n", "must be positive", id="zero"),
    pytest.param(f"{_WINDOW_VAR}=-1\n", "must be positive", id="negative"),
    pytest.param(f"{_WINDOW_VAR}=21.5\n", "must be an integer", id="float"),
    pytest.param(f'{_WINDOW_VAR}=" 21 "\n', "whitespace", id="quoted-whitespace-padding"),
    pytest.param(f"{_WINDOW_VAR}=$OTHER\n", "must be an integer", id="interpolation-refused"),
]


@pytest.mark.parametrize(("body", "match"), _PRESENT_INVALID_ROWS)
def test_read_retention_window_days_refuses_present_invalid_values(
    tmp_path: Path, body: str, match: str
) -> None:
    with pytest.raises(ArchiveConfigurationError, match=match):
        read_retention_window_days(_retention_env(tmp_path, body))


# #1230 closed-world grammar: the single refusal fragment every non-conforming
# LINE now produces, regardless of which shell form produced it.
_GRAMMAR_REFUSAL = "not a supported assignment"
# The second layer, reachable only from CONFORMING lines after #1230 (design D2).
_MENTION_REFUSAL = "cannot accept as an"

_UNSUPPORTED_SHAPE_ROWS = [
    # Round-1 narrowing (#1229 review A1): bash reads `VAR= 21` as an
    # assignment prefix plus the command `21`, so the runner sees the
    # variable UNSET. Accepting 21 would validate against a window the
    # runner never uses.
    pytest.param(f"{_WINDOW_VAR}= 21\n", "assignment is malformed", id="unquoted-leading-whitespace"),
    # Same narrowing: previously read as an empty value defaulting to 14;
    # bash also leaves the variable unset here, so refusing is fail-closed.
    pytest.param(
        f"{_WINDOW_VAR}= # comment\n",
        "assignment is malformed",
        id="empty-value-then-comment-is-malformed",
    ),
    # `#` opens a comment only AFTER whitespace: bash exports `#21`.
    pytest.param(f"{_WINDOW_VAR}=#21\n", "must be an integer", id="hash-first-character-is-a-value"),
    pytest.param(f"{_SIBLING}\r\n{_WINDOW_VAR}=21\r\n", "non-newline line breaks", id="crlf-content"),
    pytest.param(f"{_SIBLING}\n{_WINDOW_VAR}=21\v", "non-newline line breaks", id="vertical-tab-content"),
    # Unsupported assignment shapes: refused because the LINE is outside the
    # closed-world grammar (#1230) — before that these were caught one layer
    # later, by the `NAME=` mention detector. The inputs and their fail-closed
    # direction are unchanged; only the refusing layer moved.
    pytest.param(f"{_SIBLING}\nreadonly {_WINDOW_VAR}=21\n", _GRAMMAR_REFUSAL, id="readonly-prefix"),
    pytest.param(f"{_SIBLING}\ndeclare -i {_WINDOW_VAR}=21\n", _GRAMMAR_REFUSAL, id="declare-prefix"),
    pytest.param(f'{_SIBLING}\n"{_WINDOW_VAR}=21"\n', _GRAMMAR_REFUSAL, id="truncated-quoted-edit"),
    # Round-2 fail-open closure (#1229 review C2): the refusal is PER LINE.
    # `VAR=14` + `readonly VAR=30` is exported as 30 by `set -a; . file`,
    # so the round-1 "refuse only when nothing was assigned" gate returned the
    # stale 14 — a fail-open against a LARGER live window.
    pytest.param(
        f"{_WINDOW_VAR}=14\nreadonly {_WINDOW_VAR}=30\n",
        _GRAMMAR_REFUSAL,
        id="mixed-plain-then-readonly",
    ),
    # Reverse order: bash fails to re-assign the readonly variable and `. file`
    # exits non-zero, so the runner never starts — refusing is right either way.
    pytest.param(
        f"readonly {_WINDOW_VAR}=30\n{_WINDOW_VAR}=14\n",
        _GRAMMAR_REFUSAL,
        id="mixed-readonly-then-plain",
    ),
    pytest.param(
        f"{_WINDOW_VAR}=14\ndeclare -i {_WINDOW_VAR}=30\n",
        _GRAMMAR_REFUSAL,
        id="mixed-plain-then-declare",
    ),
    # #1230: eight shell forms that export the window WITHOUT the literal
    # `NAME=` substring, so the open-world mention detector let them through and
    # the helper answered with the runner-equivalent default 14 while
    # `set -a; . file` exported a LARGER window (issue table, 8/8 differentially
    # reproduced). The closed-world grammar refuses each at the offending line —
    # including the nested source lines, which no variable-name detector can see.
    pytest.param(f"{_SIBLING}\n{_WINDOW_VAR}+=21\n", _GRAMMAR_REFUSAL, id="append-assignment"),
    pytest.param(f"{_WINDOW_VAR}=14\n{_WINDOW_VAR}+=7\n", _GRAMMAR_REFUSAL, id="plain-then-append"),
    pytest.param(f"{_SIBLING}\n: ${{{_WINDOW_VAR}:=21}}\n", _GRAMMAR_REFUSAL, id="default-expansion"),
    pytest.param(f"{_SIBLING}\n. other.env\n", _GRAMMAR_REFUSAL, id="nested-dot-source"),
    pytest.param(f"{_SIBLING}\nsource other.env\n", _GRAMMAR_REFUSAL, id="nested-source-keyword"),
    pytest.param(f"{_SIBLING}\nprintf -v {_WINDOW_VAR} 21\n", _GRAMMAR_REFUSAL, id="printf-v-assignment"),
    pytest.param(f"{_SIBLING}\nread {_WINDOW_VAR} <<< 21\n", _GRAMMAR_REFUSAL, id="read-here-string"),
    pytest.param(f"{_SIBLING}\neval '{_WINDOW_VAR}'=21\n", _GRAMMAR_REFUSAL, id="eval-quoted-name"),
    # #1230 design D5(a1): a quoted value spanning lines closes on a bare `"`
    # line, which the grammar refuses. bash keeps the window line INSIDE the
    # other variable's string (runner runs its default 14) while the
    # line-oriented extractor read it as an assignment — refusing is the
    # over-strict, fail-closed side of that class (was a strict-xfail
    # differential row before the grammar landed).
    pytest.param(
        f'{_SIBLING}\nOTHER="\n{_WINDOW_VAR}=21\n"\n',
        _GRAMMAR_REFUSAL,
        id="multi-line-quoted-closing-quote-refused",
    ),
    # #1230 design D2: after the grammar, the `NAME=` mention layer is reachable
    # from two CONFORMING shapes — a value embedding the name, and a key that
    # merely ends with it. Both refuse (over-strict for the decoy, fail-closed);
    # without these rows the mention branch loses its last direct coverage.
    pytest.param(f"{_SIBLING}\nX={_WINDOW_VAR}=21\n", _MENTION_REFUSAL, id="mention-embedded-in-value"),
    pytest.param(f"{_SIBLING}\nOLD_{_WINDOW_VAR}=99\n", _MENTION_REFUSAL, id="mention-key-suffix-decoy"),
    # Wrong file entirely: no retention-family assignment at all.
    pytest.param(
        "DATABASE_URL=postgresql://x\nNHMS_ARCHIVE_MIN_AGE_DAYS=14\n",
        "does not look like the deployed retention env",
        id="wrong-file-has-no-retention-family",
    ),
    pytest.param("", "does not look like the deployed retention env", id="empty-file-mirrors-dev-null"),
    # Round-2 C1: the archive-side POINTER variable shares the retention prefix
    # but is never consumed by the runner, so it must not grant recognition —
    # otherwise pointing the guard at an archive env defaults it to 14.
    pytest.param(
        f"NHMS_ARCHIVE_MIN_AGE_DAYS=14\n{_RETENTION_ENV_VAR}=/home/nwm/x.env\n",
        "does not look like the deployed retention env",
        id="pointer-variable-alone-is-not-the-retention-env",
    ),
]


@pytest.mark.parametrize(("body", "match"), _UNSUPPORTED_SHAPE_ROWS)
def test_read_retention_window_days_refuses_unsupported_shapes(
    tmp_path: Path, body: str, match: str
) -> None:
    """Fail-direction hardening: every divergence from `set -a; . file` refuses."""
    path = _retention_env(tmp_path, body)

    with pytest.raises(ArchiveConfigurationError, match=match) as error:
        read_retention_window_days(path)

    assert str(path) in str(error.value)


@pytest.mark.parametrize(
    ("body", "offending_line"),
    [
        pytest.param(f"{_SIBLING}\n{_WINDOW_VAR}+=21\n", f"{_WINDOW_VAR}+=21", id="append-assignment"),
        pytest.param(f"{_SIBLING}\n. other.env\n", ". other.env", id="nested-dot-source"),
        pytest.param(
            f"{_SIBLING}\nprintf -v {_WINDOW_VAR} 21\n",
            f"printf -v {_WINDOW_VAR} 21",
            id="printf-v-assignment",
        ),
        # Two offending lines: the FIRST in file order must be the one named,
        # so the message is deterministic for an operator diffing the file.
        pytest.param(
            f"{_SIBLING}\n. first.env\nsource second.env\n",
            ". first.env",
            id="first-offending-line-in-file-order",
        ),
    ],
)
def test_grammar_refusal_names_the_offending_line(
    tmp_path: Path, body: str, offending_line: str
) -> None:
    """#1230 acceptance item 1: the operator gets the path AND the exact line."""
    path = _retention_env(tmp_path, body)

    with pytest.raises(ArchiveConfigurationError, match=_GRAMMAR_REFUSAL) as error:
        read_retention_window_days(path)

    message = str(error.value)
    assert str(path) in message
    assert repr(offending_line) in message


@pytest.mark.parametrize(
    ("body", "offending_line"),
    [
        pytest.param(f"{_SIBLING}\nX={_WINDOW_VAR}=21\n", f"X={_WINDOW_VAR}=21", id="embedded-in-value"),
        pytest.param(
            f"{_SIBLING}\nOLD_{_WINDOW_VAR}=99\n",
            f"OLD_{_WINDOW_VAR}=99",
            id="key-suffix-decoy",
        ),
    ],
)
def test_mention_refusal_names_the_offending_line(
    tmp_path: Path, body: str, offending_line: str
) -> None:
    """#1230 acceptance item 3: the mention layer localizes its refusal too."""
    path = _retention_env(tmp_path, body)

    with pytest.raises(ArchiveConfigurationError, match=_MENTION_REFUSAL) as error:
        read_retention_window_days(path)

    message = str(error.value)
    assert str(path) in message
    assert repr(offending_line) in message


def test_shipped_env_templates_never_hit_the_grammar_refusal(tmp_path: Path) -> None:
    """#1230 design D3: zero grammar-class false refusals on the shipped templates.

    Bound to BEHAVIOR, not to a re-implementation of the grammar: every
    `infra/env/*.example` goes through the public helper, and the only allowed
    outcomes are a positive window (the retention template) or a refusal that
    is NOT the closed-world grammar refusal (archive and unrelated templates
    refuse as "does not look like the deployed retention env"). A future
    template line that stops conforming turns this red.
    """
    templates = sorted((_REPO_ROOT / "infra/env").glob("*.example"))
    assert len(templates) >= 15

    for template in templates:
        path = tmp_path / f"{template.name}.env"
        path.write_bytes(template.read_bytes())
        try:
            window = read_retention_window_days(path)
        except ArchiveConfigurationError as error:
            assert _GRAMMAR_REFUSAL not in str(error), f"{template.name}: {error}"
            continue
        assert isinstance(window, int) and window > 0, f"{template.name} resolved to {window!r}"


@pytest.mark.parametrize(
    "example",
    [
        "infra/env/node27-product-archive.example",
        "infra/env/node27-storage-inventory-audit.example",
    ],
)
def test_read_retention_window_days_refuses_the_shipped_archive_envs(tmp_path: Path, example: str) -> None:
    """Round-2 C1 lock: the ACTUAL bytes of both archive templates must refuse.

    They carry `NODE27_TIMESERIES_RETENTION_ENV` (the pointer at the retention
    env), which shares the retention prefix. Counting it as retention-family
    recognition made a self- or sibling-pointing misconfiguration default
    silently to 14. Reading the repo files (not a hand-copied body) means any
    future retention-prefixed line added to these templates turns this red.
    """
    body = (_REPO_ROOT / example).read_bytes()
    path = tmp_path / "archive.env"
    path.write_bytes(body)

    assert f"{_RETENTION_ENV_VAR}=".encode() in body
    with pytest.raises(ArchiveConfigurationError, match="does not look like the deployed retention env"):
        read_retention_window_days(path)


def test_read_retention_window_days_accepts_the_shipped_retention_env(tmp_path: Path) -> None:
    """The counterpart lock: the real retention template still parses to 14."""
    path = tmp_path / "retention.env"
    path.write_bytes((_REPO_ROOT / "infra/env/node27-timeseries-retention.example").read_bytes())

    assert read_retention_window_days(path) == 14


# ---------------------------------------------------------------------------
# 6.2 invariant audit (#1229 round-2, design D5(d)): the parser-fail-direction
# class repeated across two review rounds, so the invariant is made executable
# instead of re-audited by eye. For every corpus body the helper must EITHER
# refuse OR return exactly the window the retention RUNNER would actually run
# with — a different number, or any number where the runner refuses / never
# starts, fails. Fail-closed narrowings (the helper refusing where the runner
# runs) are legitimate by design and deliberately NOT asserted against.
# ---------------------------------------------------------------------------

_BASH = shutil.which("bash")
_UNSET_SENTINEL = "__UNSET__"

# Residual class tripwire (#1230 design D5(a2)): a quoted value spanning lines
# whose EVERY line happens to fullmatch the grammar. `OTHER="` takes the bare
# quote as its value, the inner window line is read as the last assignment
# (helper 7) while bash keeps it inside OTHER's string and exports the earlier
# 30 — still FAIL-OPEN, not closable by a line grammar (it needs unbalanced
# quote tracking). Pinned strict-xfail so the day that lands shows up as XPASS.
# The (a1) sibling — a closing bare `"` line — is now a plain grammar refusal
# row in `_UNSUPPORTED_SHAPE_ROWS`.
_MULTILINE_QUOTED_ALL_CONFORMING_BODY = f'{_WINDOW_VAR}=30\nOTHER="\n{_WINDOW_VAR}=7\nX=y"\n'

# Second residual class tripwire (#1230 design D5(b)): the grammar is LINE-level,
# so a fully CONFORMING `KEY=VALUE` line can still assign the WINDOW variable
# from inside its VALUE via a shell expansion. `X=${WINDOW:=21}` carries no
# literal `WINDOW=` substring, so it slips the grammar AND the mention layer:
# the helper sees only the sibling and answers the runner-equivalent 14 while
# `set -a; . file` exports 21 — FAIL-OPEN. `X=$((WINDOW+=7))` is the arithmetic
# sibling of the same family. Closing this needs expansion-aware value scanning,
# out of scope here; pinned strict-xfail so the day that lands shows up as XPASS.
_VALUE_LEVEL_EXPANSION_BODY = f"{_SIBLING}\nX=${{{_WINDOW_VAR}:=21}}\n"


def _differential_corpus() -> list[Any]:
    rows: list[Any] = [
        pytest.param(param.values[0], id=param.id)
        for param in (*_LEXICAL_ROWS, *_PRESENT_INVALID_ROWS, *_UNSUPPORTED_SHAPE_ROWS)
    ]
    rows.append(
        pytest.param(
            _MULTILINE_QUOTED_ALL_CONFORMING_BODY,
            id="multi-line-quoted-all-conforming-still-diverges",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "#1230 design D5(a2) recorded residual: every line of a multi-line quoted "
                    "value conforms to the grammar, so the helper reads the inner assignment "
                    "while bash keeps it inside the outer string"
                ),
            ),
        )
    )
    rows.append(
        pytest.param(
            _VALUE_LEVEL_EXPANSION_BODY,
            id="value-level-expansion-still-diverges",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "#1230 design D5(b) recorded residual: value-level shell expansion on a "
                    "CONFORMING line assigns the window variable itself, invisible to a "
                    "line-level grammar and to the `NAME=` mention layer"
                ),
            ),
        )
    )
    return rows


def _runner_effective_window(env_file: Path) -> int | None:
    """Return the window the retention runner would run with, or None if it cannot.

    None means the runner never gets a window: either the wrapper's
    `set -a; . <env>` fails (ENV_FILE_SOURCE_FAILED) or the runner's strict
    parse refuses the exported value.
    """
    script = (
        'set -a; . "$1" 2>/dev/null; rc=$?; '
        f'printf %s "${{{_WINDOW_VAR}-{_UNSET_SENTINEL}}}"; exit "$rc"'
    )
    # Bytes, not text mode: universal-newline translation would hide the `\r`
    # of a CRLF value, which is exactly what the runner's strict parse rejects.
    completed = subprocess.run(
        [str(_BASH), "-c", script, "bash", str(env_file)],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        return None
    exported = completed.stdout.decode("utf-8", errors="surrogateescape")
    raw = None if exported == _UNSET_SENTINEL else exported
    try:
        return node27_timeseries_retention._optional_positive_int(
            raw,
            name=_WINDOW_VAR,
            default=node27_timeseries_retention._DEFAULT_WINDOW_DAYS,
        )
    except node27_timeseries_retention.RetentionConfigError:
        return None


@pytest.mark.skipif(_BASH is None, reason="differential oracle needs a real bash to source env files")
@pytest.mark.parametrize("body", _differential_corpus())
def test_helper_never_returns_a_window_the_runner_would_not_use(tmp_path: Path, body: str) -> None:
    """Differential oracle against `bash -c 'set -a; . file'` + the runner's parse."""
    path = _retention_env(tmp_path, body)
    runner_window = _runner_effective_window(path)

    try:
        helper_window = read_retention_window_days(path)
    except ArchiveConfigurationError:
        return  # Fail-closed narrowing: allowed, and not asserted against.

    assert runner_window is not None, (
        f"helper returned {helper_window} but the runner would refuse or never start on {body!r}"
    )
    assert helper_window == runner_window, (
        f"helper returned {helper_window} but the runner would run with {runner_window} on {body!r}"
    )


def test_read_retention_window_days_refuses_unset_path() -> None:
    with pytest.raises(ArchiveConfigurationError, match="NODE27_TIMESERIES_RETENTION_ENV must be set"):
        read_retention_window_days(None)


def test_read_retention_window_days_refuses_empty_path() -> None:
    with pytest.raises(ArchiveConfigurationError, match="NODE27_TIMESERIES_RETENTION_ENV must be set"):
        read_retention_window_days("   ")


def test_read_retention_window_days_refuses_relative_path_by_absoluteness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative path that EXISTS and parses must still refuse, naming absoluteness."""
    _retention_env(tmp_path, f"{_WINDOW_VAR}=21\n")
    monkeypatch.chdir(tmp_path)
    assert Path("retention.env").is_file()

    with pytest.raises(ArchiveConfigurationError, match="must be an absolute path: retention.env"):
        read_retention_window_days("retention.env")


def test_read_retention_window_days_refuses_missing_file_without_fallback(tmp_path: Path) -> None:
    """Missing FILE is NOT the missing-ASSIGNMENT case: no constant fallback."""
    with pytest.raises(ArchiveConfigurationError, match="retention env file is unreadable"):
        read_retention_window_days(tmp_path / "absent.env")


def test_read_retention_window_days_refuses_directory_source(tmp_path: Path) -> None:
    directory = tmp_path / "retention.env"
    directory.mkdir()
    with pytest.raises(ArchiveConfigurationError, match="retention env file is unreadable"):
        read_retention_window_days(directory)


def test_live_drifted_pair_refuses_at_the_single_comparison_site(tmp_path: Path) -> None:
    window = read_retention_window_days(_retention_env(tmp_path, f"{_WINDOW_VAR}=21\n"))

    with pytest.raises(ArchiveConfigurationError) as error:
        validate_archive_configuration(
            archive_root=tmp_path / "archive",
            cleanup_roots={"object_store_root": tmp_path / "object-store"},
            archive_min_age_days=14,
            retention_days=window,
        )

    message = str(error.value)
    assert "14" in message and "21" in message


@pytest.mark.parametrize(("min_age", "window", "accepted"), [(21, 21, True), (30, 21, True), (20, 21, False)])
def test_min_age_boundary_against_live_window(
    tmp_path: Path, min_age: int, window: int, accepted: bool
) -> None:
    def _validate() -> None:
        validate_archive_configuration(
            archive_root=tmp_path / "archive",
            cleanup_roots={"object_store_root": tmp_path / "object-store"},
            archive_min_age_days=min_age,
            retention_days=window,
        )

    if accepted:
        _validate()
        return
    with pytest.raises(ArchiveConfigurationError, match=f"{min_age} days is below DB retention {window} days"):
        _validate()


@pytest.mark.parametrize(
    "example",
    ["infra/env/node27-product-archive.example", "infra/env/node27-storage-inventory-audit.example"],
)
def test_archive_env_examples_declare_the_live_window_source(example: str) -> None:
    """Row (g): the invariant bound is no longer stated as a '14-day' literal."""
    text = (Path(__file__).resolve().parents[1] / example).read_text(encoding="utf-8")

    assert "NODE27_TIMESERIES_RETENTION_ENV=/home/nwm/NWM/infra/env/node27-timeseries-retention.env" in text
    assert "NHMS_ARCHIVE_MIN_AGE_DAYS=14" in text
    assert "14-day" not in text
    assert ">= 14 d" not in text


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
