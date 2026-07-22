from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from packages.common.storage import (
    VALID_PREFIX_PATTERNS,
    ArchiveConfigurationError,
    ArchiveIdentity,
    archive_identity_for_state_reference,
    archive_provenance_paths,
    resolve_archive_root,
    resolve_archive_storage_config,
    validate_archive_configuration,
    validate_object_path,
    validate_product_archive_manifest_binding,
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


def test_validate_archive_configuration_rejects_relative_cleanup_root(tmp_path: Path) -> None:
    with pytest.raises(ArchiveConfigurationError, match="cleanup root raw-retention must be absolute"):
        validate_archive_configuration(
            archive_root=tmp_path / "archive",
            cleanup_roots={"raw-retention": "relative/object-store"},
        )


def test_validate_archive_configuration_rejects_relative_archive_root(tmp_path: Path) -> None:
    with pytest.raises(ArchiveConfigurationError, match="archive root must be absolute"):
        validate_archive_configuration(
            archive_root="relative/archive",
            cleanup_roots={"raw-retention": tmp_path / "object-store"},
        )


def test_validate_archive_configuration_canonicalizes_absolute_dotdot_path(tmp_path: Path) -> None:
    config = validate_archive_configuration(
        archive_root=tmp_path / "archive-parent" / ".." / "archive",
        cleanup_roots={"raw-retention": tmp_path / "object-store"},
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
        validate_archive_configuration(archive_root=tmp_path / "archive", cleanup_roots={})


def test_resolve_archive_storage_config_rejects_minimum_age_below_retention(tmp_path: Path) -> None:
    with pytest.raises(ArchiveConfigurationError, match="13 days is below DB retention 14 days"):
        resolve_archive_storage_config(
            cleanup_roots={"raw": tmp_path / "object-store"},
            env={
                "NHMS_ARCHIVE_ROOT": str(tmp_path / "archive"),
                "NHMS_ARCHIVE_MIN_AGE_DAYS": "13",
            },
        )


def test_resolve_archive_storage_config_uses_default_age(tmp_path: Path) -> None:
    config = resolve_archive_storage_config(
        cleanup_roots={"raw": tmp_path / "object-store"},
        env={"NHMS_ARCHIVE_ROOT": str(tmp_path / "archive")},
    )

    assert config.archive_min_age_days == 14
    assert config.retention_days == 14


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
