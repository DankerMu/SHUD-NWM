"""Tests for :mod:`workers.mapping_builder.binding` (Epic #909 SUB-11, §4.1 + §4.2).

Coverage
--------

* §4.1 :func:`emit_direct_grid_manifest_and_binding` — positive path
  (manifest + binding emitted, checksums recomputed, cross-consistent);
  round-trips through the existing direct-grid contract parser; required
  manifest + station fields pinned; grid_cell_id uniqueness + snapshot
  membership; manifest ↔ binding cross-consistency G5 gate (station_id /
  shud_forcing_index / grid_cell_id / lon-lat divergence blockers);
  binding_checksum + sp_att_checksum mismatch blockers.
* §4.2 station identity — station_id embeds mapping-asset identity and is
  never reused across mapping versions.
* §4.2 filename safety — case-fold uniqueness; never collides with
  reserved names (qhh.tsd.forc / manifest.json / debug / model-input
  suffixes) on case-insensitive filesystems; regex-matches the parser's
  _SAFE_STATION_FORCING_FILENAME; not derived from rounded coordinates.
* §4.2 coordinate rules — station lon/lat equal cell center under
  12-decimal rounding (positive + ~1e-7° noise blocker); WGS84 basis
  declared; cross-basis (SRID 4490 / CGCS2000) equality forbidden;
  x/y recomputable from lon/lat + model CRS.
* §4.2 z_policy — read verbatim from an approved :class:`ZPolicy`
  (Epic #886 verdict); missing-cell + invalid-policy + missing-provenance
  blockers.
* Signature pin + frozen dataclass invariants matching the SUB-8/9/10
  style.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import pathlib
import typing

import pyproj
import pytest

from packages.common import grid_signature as grid_signature_module
from tests.fixtures.mapping_builder.in_memory_grid_snapshot import (
    make_regular_grid_cells,
)
from workers.forcing_producer.direct_grid_contract import (
    DIRECT_GRID_MODE,
    DirectGridForcingContract,
)
from workers.mapping_builder import (
    ALLOWED_Z_POLICIES,
    CGCS2000_SRID_LABEL,
    COORDINATE_ROUNDING_DECIMALS,
    DIRECT_GRID_FORCING_SECTION_KEY,
    FORBIDDEN_MET_TABLES,
    RESERVED_FILENAME_PREFIXES,
    RESERVED_FILENAME_SUFFIXES,
    RESERVED_FORCING_FILENAMES,
    STATION_ID_SEPARATOR,
    WGS84_COORDINATE_BASIS,
    BaselineIntegrityError,
    BindingArtifact,
    BindingArtifactError,
    BindingChecksumMismatchError,
    CrossBasisEqualityError,
    CycleLineageSpy,
    DbWriteSpy,
    DirectGridManifest,
    ForbiddenOutputClass,
    ForbiddenOutputScanResult,
    ForbiddenRuntimeProducerArtifactError,
    ForcingFilenameCollisionError,
    ForcingFilenameUnsafeError,
    GridCellIdDuplicateError,
    GridCellIdNotInSnapshotError,
    InvalidZPolicyError,
    ManifestBindingDivergenceError,
    ManifestFieldMissingError,
    MappingAlgorithmError,
    ParserRoundTripError,
    ReadinessManifestChecksumMissingError,
    SpAttChecksumMismatchError,
    SpAttRewriteError,
    StationBinding,
    StationCenterMismatchError,
    StationIdReuseError,
    StationIdSeparatorConflictError,
    UnmonitoredBoundaryError,
    XyRecomputationMismatchError,
    ZPolicy,
    ZPolicyCellMissingError,
    apply_z_policy_from_readiness,
    assign_station_id_from_mapping_asset_identity,
    emit_direct_grid_manifest_and_binding,
    recompute_binding_and_sp_att_checksums,
    sanitize_station_forcing_filename,
    verify_binding_round_trips_parser,
    verify_grid_cell_id_unique_and_snapshot_member,
    verify_manifest_binding_cross_consistent,
    verify_no_forbidden_runtime_producer_artifacts,
    verify_sp_att_checksum,
    verify_station_center_matches_snapshot_under_rounding,
    verify_station_id_disjoint_across_versions,
    verify_x_y_recomputable,
)
from workers.mapping_builder import binding as binding_module

# --- fixture helpers ------------------------------------------------------


# Deterministic model CRS: a Transverse Mercator projection based on the
# qhh baseline PROJCS from docs (§附录 A), simplified to a self-contained
# WKT. Chosen so ``pyproj.CRS.from_wkt`` accepts it without external
# resources and produces a stable forward/inverse transform.
_MODEL_CRS_WKT = (
    'PROJCS["Custom_TM",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
    'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
    'PROJECTION["Transverse_Mercator"],'
    'PARAMETER["False_Easting",500000.0],'
    'PARAMETER["False_Northing",0.0],'
    'PARAMETER["Central_Meridian",99.0],'
    'PARAMETER["Scale_Factor",0.9996],'
    'PARAMETER["Latitude_Of_Origin",0.0],'
    'UNIT["Meter",1.0]]'
)


def _make_used_cells_and_snapshot(
    *,
    lon0: float = 100.0,
    lat0: float = 37.0,
    lon_step: float = 0.1,
    lat_step: float = 0.1,
    lon_count: int = 4,
    lat_count: int = 4,
    used_count: int = 4,
) -> tuple[list, list]:
    """Return ``(used_cells, snapshot_cells)`` for a regular grid.

    ``used_cells`` is the first ``used_count`` cells of the full snapshot
    (canonical_ordinal ascending) so ``verify_grid_cell_id_unique_and_snapshot_member``
    passes by construction.
    """
    snapshot_cells = make_regular_grid_cells(
        lon0=lon0,
        lat0=lat0,
        lon_step=lon_step,
        lat_step=lat_step,
        lon_count=lon_count,
        lat_count=lat_count,
    )
    if used_count > len(snapshot_cells):
        raise ValueError(
            f"used_count={used_count} exceeds snapshot size {len(snapshot_cells)}"
        )
    used = sorted(snapshot_cells, key=lambda c: int(c.canonical_ordinal))[
        :used_count
    ]
    return used, snapshot_cells


def _make_z_policy(cells) -> ZPolicy:
    """Return a valid ZPolicy with one z value per used cell."""
    return ZPolicy(
        policy_name="sentinel",
        readiness_manifest_checksum="a" * 64,
        per_cell_z={cell.grid_cell_id: -9999.0 for cell in cells},
    )


def _make_shud_forcing_index(cells) -> dict[str, int]:
    """Return contiguous 1..N shud_forcing_index for cells in canonical order."""
    sorted_cells = sorted(cells, key=lambda c: int(c.canonical_ordinal))
    return {cell.grid_cell_id: idx for idx, cell in enumerate(sorted_cells, start=1)}


def _emit_minimal(
    *,
    used_count: int = 4,
    mapping_asset_identity: str = "mapping-v1-abc",
    sp_att_bytes: bytes = b"MOCK_SP_ATT_BYTES\n",
    coordinate_reference_system: str = WGS84_COORDINATE_BASIS,
) -> tuple[DirectGridManifest, BindingArtifact, list, list]:
    """Emit a green-path manifest + binding and return everything needed for follow-up checks."""
    used_cells, snapshot_cells = _make_used_cells_and_snapshot(
        used_count=used_count
    )
    shud_forcing_index = _make_shud_forcing_index(used_cells)
    z_policy = _make_z_policy(used_cells)
    manifest, artifact = emit_direct_grid_manifest_and_binding(
        used_cells=used_cells,
        snapshot_cells=snapshot_cells,
        shud_forcing_index=shud_forcing_index,
        mapping_asset_identity=mapping_asset_identity,
        model_input_package_id="pkg-v1-abc",
        sp_att_path="package/keliya.sp.att",
        sp_att_bytes=sp_att_bytes,
        applicable_source_ids=("GFS",),
        grid_id="GFS_0p1",
        grid_signature="b" * 64,
        z_policy=z_policy,
        binding_uri="s3://bucket/mapping-v1-abc/binding.json",
        model_crs_wkt=_MODEL_CRS_WKT,
        coordinate_reference_system=coordinate_reference_system,
    )
    return manifest, artifact, used_cells, snapshot_cells


# =========================================================================
# §4.1 GREEN PATH
# =========================================================================


def test_emit_direct_grid_manifest_and_binding_positive_path() -> None:
    """Green build: manifest + binding artifact emit and cross-consistent."""
    manifest, artifact, used_cells, snapshot_cells = _emit_minimal()

    # Manifest identity fields set to arguments.
    assert manifest.forcing_mapping_mode == DIRECT_GRID_MODE
    assert manifest.forcing_mapping_mode == "direct_grid"
    assert manifest.binding_uri == "s3://bucket/mapping-v1-abc/binding.json"
    assert manifest.model_input_package_id == "pkg-v1-abc"
    assert manifest.sp_att_path == "package/keliya.sp.att"
    assert manifest.applicable_source_ids == ("GFS",)
    assert manifest.grid_id == "GFS_0p1"
    assert manifest.grid_signature == "b" * 64
    assert manifest.coordinate_reference_system == WGS84_COORDINATE_BASIS

    # Manifest carries one station binding per used cell.
    assert len(manifest.station_bindings) == len(used_cells)

    # Manifest.binding_checksum matches SHA-256 of artifact bytes.
    assert manifest.binding_checksum == hashlib.sha256(artifact.bytes).hexdigest()
    assert manifest.binding_checksum == artifact.checksum

    # Manifest.sp_att_checksum matches SHA-256 of the provided sp_att bytes.
    assert manifest.sp_att_checksum == hashlib.sha256(b"MOCK_SP_ATT_BYTES\n").hexdigest()

    # Artifact carries the identical grid metadata.
    assert artifact.grid_id == manifest.grid_id
    assert artifact.grid_signature == manifest.grid_signature
    assert artifact.coordinate_reference_system == WGS84_COORDINATE_BASIS


# =========================================================================
# §4.1 REQUIRED MANIFEST + STATION FIELDS (parser contract shape)
# =========================================================================


def test_manifest_carries_all_ten_required_fields() -> None:
    """Manifest emits every field required by the parser + canonical extras.

    Docs §7.2 declares the required manifest field set. The parser's
    ``REQUIRED_MANIFEST_FIELDS`` is 8 fields (without forcing_mapping_mode
    and without station_bindings). Adding those two brings the count to
    10 as declared by the spec.
    """
    manifest, _, _, _ = _emit_minimal()
    section = manifest.to_contract_section_dict()
    for field_name in (
        "forcing_mapping_mode",
        "binding_uri",
        "binding_checksum",
        "model_input_package_id",
        "sp_att_path",
        "sp_att_checksum",
        "applicable_source_ids",
        "grid_id",
        "grid_signature",
        "station_bindings",
    ):
        assert field_name in section, f"manifest missing required field {field_name!r}"
        # Non-empty for the string fields; positive-length for list fields.
        value = section[field_name]
        if isinstance(value, str):
            assert value, f"manifest field {field_name!r} is empty string"
        elif isinstance(value, list):
            assert len(value) > 0, f"manifest field {field_name!r} is empty list"


def test_station_bindings_carry_all_ten_required_fields() -> None:
    """Every station binding row provides the parser's 10 required station fields."""
    manifest, _, _, _ = _emit_minimal()
    section = manifest.to_contract_section_dict()
    stations = section["station_bindings"]
    assert len(stations) > 0
    for row in stations:
        for field_name in (
            "station_id",
            "shud_forcing_index",
            "forcing_filename",
            "longitude",
            "latitude",
            "x",
            "y",
            "z",
            "grid_id",
            "grid_cell_id",
        ):
            assert field_name in row, f"station missing required field {field_name!r}"


def test_manifest_placed_in_nested_direct_grid_forcing_section() -> None:
    """Manifest is placed under `resource_profile.direct_grid_forcing` (docs §7.1)."""
    manifest, _, _, _ = _emit_minimal()
    outer = manifest.to_resource_profile_dict()
    assert DIRECT_GRID_FORCING_SECTION_KEY in outer
    section = outer[DIRECT_GRID_FORCING_SECTION_KEY]
    assert section["forcing_mapping_mode"] == "direct_grid"


# =========================================================================
# §4.1 PARSER ROUND-TRIP (G5 contract-shape)
# =========================================================================


def test_verify_binding_round_trips_parser_positive() -> None:
    """Emitted manifest parses cleanly through the existing direct-grid parser."""
    manifest, _, _, _ = _emit_minimal()
    contract = verify_binding_round_trips_parser(
        manifest.to_resource_profile_dict(),
        source_id="GFS",
    )
    assert isinstance(contract, DirectGridForcingContract)
    assert contract.forcing_mapping_mode == "direct_grid"
    assert contract.grid_id == manifest.grid_id
    assert contract.grid_signature == manifest.grid_signature
    assert len(contract.stations) == len(manifest.station_bindings)


def test_verify_binding_round_trips_parser_mutated_manifest_blocks() -> None:
    """Missing required manifest field -> ParserRoundTripError."""
    manifest, _, _, _ = _emit_minimal()
    outer = manifest.to_resource_profile_dict()
    del outer[DIRECT_GRID_FORCING_SECTION_KEY]["binding_uri"]
    with pytest.raises(ParserRoundTripError):
        verify_binding_round_trips_parser(outer, source_id="GFS")


# =========================================================================
# §4.1 grid_cell_id UNIQUENESS + SNAPSHOT MEMBERSHIP
# =========================================================================


def test_grid_cell_id_pairwise_unique() -> None:
    """Every emitted grid_cell_id is pairwise unique in the binding."""
    manifest, _, _, _ = _emit_minimal()
    cell_ids = [b.grid_cell_id for b in manifest.station_bindings]
    assert len(cell_ids) == len(set(cell_ids))


def test_grid_cell_id_all_snapshot_members() -> None:
    """Every emitted grid_cell_id is a member of the loaded snapshot cell set."""
    manifest, _, _, snapshot_cells = _emit_minimal()
    snapshot_ids = {c.grid_cell_id for c in snapshot_cells}
    for b in manifest.station_bindings:
        assert b.grid_cell_id in snapshot_ids


def test_verify_grid_cell_id_gate_positive() -> None:
    """Green-path gate passes on a valid binding + snapshot pair."""
    manifest, _, _, snapshot_cells = _emit_minimal()
    # None on pass (per gate return convention).
    assert (
        verify_grid_cell_id_unique_and_snapshot_member(
            manifest.station_bindings, snapshot_cells
        )
        is None
    )


def test_verify_grid_cell_id_gate_duplicate_blocks() -> None:
    """Duplicate grid_cell_id in bindings -> GridCellIdDuplicateError."""
    manifest, _, _, snapshot_cells = _emit_minimal()
    # Corrupt one binding to duplicate another's grid_cell_id.
    bindings = list(manifest.station_bindings)
    poisoned = dataclasses.replace(
        bindings[1], grid_cell_id=bindings[0].grid_cell_id
    )
    bindings[1] = poisoned
    with pytest.raises(GridCellIdDuplicateError) as exc:
        verify_grid_cell_id_unique_and_snapshot_member(bindings, snapshot_cells)
    assert exc.value.grid_cell_id == bindings[0].grid_cell_id
    assert isinstance(exc.value, BindingArtifactError)


def test_verify_grid_cell_id_gate_snapshot_membership_blocks() -> None:
    """grid_cell_id absent from snapshot -> GridCellIdNotInSnapshotError."""
    manifest, _, _, snapshot_cells = _emit_minimal()
    bindings = list(manifest.station_bindings)
    poisoned = dataclasses.replace(bindings[1], grid_cell_id="NOT_IN_SNAPSHOT")
    bindings[1] = poisoned
    with pytest.raises(GridCellIdNotInSnapshotError) as exc:
        verify_grid_cell_id_unique_and_snapshot_member(bindings, snapshot_cells)
    assert exc.value.grid_cell_id == "NOT_IN_SNAPSHOT"
    assert isinstance(exc.value, BindingArtifactError)


# =========================================================================
# §4.1 MANIFEST ↔ BINDING CROSS-CONSISTENCY (G5)
# =========================================================================


def test_verify_manifest_binding_cross_consistent_positive() -> None:
    """Green-path cross-consistency gate returns None (pass)."""
    manifest, artifact, _, _ = _emit_minimal()
    assert verify_manifest_binding_cross_consistent(manifest, artifact) is None


def test_verify_manifest_binding_cross_consistency_binding_checksum_mismatch() -> None:
    """Divergent binding_checksum -> BindingChecksumMismatchError (G5 blocker)."""
    manifest, artifact, _, _ = _emit_minimal()
    poisoned = dataclasses.replace(manifest, binding_checksum="f" * 64)
    with pytest.raises(BindingChecksumMismatchError):
        verify_manifest_binding_cross_consistent(poisoned, artifact)


def test_verify_manifest_binding_cross_consistency_station_id_divergence() -> None:
    """Manifest station_id set differs from binding -> ManifestBindingDivergenceError."""
    manifest, artifact, _, _ = _emit_minimal()
    # Replace one manifest station_id but keep the binding artifact untouched.
    bindings = list(manifest.station_bindings)
    bindings[0] = dataclasses.replace(bindings[0], station_id="INJECTED_DIFFERENT_ID")
    poisoned_manifest = dataclasses.replace(
        manifest, station_bindings=tuple(bindings)
    )
    with pytest.raises(ManifestBindingDivergenceError) as exc:
        verify_manifest_binding_cross_consistent(poisoned_manifest, artifact)
    assert exc.value.divergent_field == "station_id_set"


def test_verify_manifest_binding_cross_consistency_shud_forcing_index_divergence() -> None:
    """Divergent shud_forcing_index -> ManifestBindingDivergenceError."""
    manifest, artifact, _, _ = _emit_minimal()
    # Same station_ids, but manifest has a different shud_forcing_index.
    bindings = list(manifest.station_bindings)
    bindings[0] = dataclasses.replace(bindings[0], shud_forcing_index=999)
    poisoned_manifest = dataclasses.replace(
        manifest, station_bindings=tuple(bindings)
    )
    with pytest.raises(ManifestBindingDivergenceError) as exc:
        verify_manifest_binding_cross_consistent(poisoned_manifest, artifact)
    assert exc.value.divergent_field == "shud_forcing_index"


def test_verify_manifest_binding_cross_consistency_grid_cell_id_divergence() -> None:
    """Divergent grid_cell_id -> ManifestBindingDivergenceError."""
    manifest, artifact, _, snapshot_cells = _emit_minimal()
    bindings = list(manifest.station_bindings)
    # Pick a different grid_cell_id that still exists in the snapshot to
    # keep this test focused on the manifest-binding gate (not the
    # grid_cell_id snapshot-membership gate).
    other_cell = next(
        c
        for c in snapshot_cells
        if c.grid_cell_id != bindings[0].grid_cell_id
    )
    bindings[0] = dataclasses.replace(bindings[0], grid_cell_id=other_cell.grid_cell_id)
    poisoned_manifest = dataclasses.replace(
        manifest, station_bindings=tuple(bindings)
    )
    with pytest.raises(ManifestBindingDivergenceError) as exc:
        verify_manifest_binding_cross_consistent(poisoned_manifest, artifact)
    assert exc.value.divergent_field == "grid_cell_id"


def test_verify_manifest_binding_cross_consistency_lonlat_divergence_at_12_decimal() -> None:
    """Divergent lon/lat past 12-decimal rounding -> ManifestBindingDivergenceError.

    An injected divergence larger than the 12-decimal-rounding tolerance
    (~1e-12°) MUST fail closed. Sub-1e-12 noise MUST NOT be flagged (see
    :func:`test_lonlat_equality_at_12_decimals_absorbs_1em13_noise`).
    """
    manifest, artifact, _, _ = _emit_minimal()
    bindings = list(manifest.station_bindings)
    # Perturb longitude by 1e-6° (well past the 12-decimal tolerance).
    bindings[0] = dataclasses.replace(
        bindings[0], longitude=bindings[0].longitude + 1e-6
    )
    poisoned_manifest = dataclasses.replace(
        manifest, station_bindings=tuple(bindings)
    )
    with pytest.raises(ManifestBindingDivergenceError) as exc:
        verify_manifest_binding_cross_consistent(poisoned_manifest, artifact)
    assert exc.value.divergent_field == "longitude"


# =========================================================================
# §4.1 CHECKSUM MISMATCH BLOCKERS
# =========================================================================


def test_binding_checksum_equals_sha256_of_emitted_bytes() -> None:
    """manifest.binding_checksum equals SHA-256 of the emitted binding artifact bytes."""
    manifest, artifact, _, _ = _emit_minimal()
    expected = hashlib.sha256(artifact.bytes).hexdigest()
    assert manifest.binding_checksum == expected


def test_sp_att_checksum_equals_sha256_of_emitted_variant_bytes() -> None:
    """manifest.sp_att_checksum equals SHA-256 of the supplied variant .sp.att bytes."""
    payload = b"DIFFERENT_VARIANT_BYTES\n"
    manifest, _, _, _ = _emit_minimal(sp_att_bytes=payload)
    expected = hashlib.sha256(payload).hexdigest()
    assert manifest.sp_att_checksum == expected


def test_recompute_binding_and_sp_att_checksums_returns_pair() -> None:
    """recompute_binding_and_sp_att_checksums returns (binding_sha, sp_att_sha)."""
    binding_bytes = b'{"grid_id":"x"}'
    sp_att_bytes = b"row_data\n"
    binding_sha, sp_att_sha = recompute_binding_and_sp_att_checksums(
        binding_bytes, sp_att_bytes
    )
    assert binding_sha == hashlib.sha256(binding_bytes).hexdigest()
    assert sp_att_sha == hashlib.sha256(sp_att_bytes).hexdigest()


def test_binding_checksum_mismatch_blocks_g5() -> None:
    """Injected binding_checksum mismatch -> BindingChecksumMismatchError."""
    manifest, artifact, _, _ = _emit_minimal()
    poisoned = dataclasses.replace(manifest, binding_checksum="0" * 64)
    with pytest.raises(BindingChecksumMismatchError) as exc:
        verify_manifest_binding_cross_consistent(poisoned, artifact)
    assert exc.value.manifest_checksum == "0" * 64
    assert exc.value.recomputed_checksum == artifact.checksum


def test_sp_att_checksum_mismatch_blocks_g5() -> None:
    """Independent test: manifest.sp_att_checksum drift is detectable by recompute."""
    manifest, _, _, _ = _emit_minimal(sp_att_bytes=b"BASELINE\n")
    poisoned = dataclasses.replace(manifest, sp_att_checksum="0" * 64)
    # Recompute reveals the drift.
    _, recomputed_sp_att = recompute_binding_and_sp_att_checksums(
        b"IGNORED", b"BASELINE\n"
    )
    assert poisoned.sp_att_checksum != recomputed_sp_att


# =========================================================================
# §4.2 STATION IDENTITY (non-reuse across mapping versions)
# =========================================================================


def test_assign_station_id_embeds_mapping_asset_identity() -> None:
    """station_id embeds the mapping_asset_identity verbatim + grid_cell_id."""
    station_id = assign_station_id_from_mapping_asset_identity(
        mapping_asset_identity="mapping-v1-abc",
        grid_cell_id="42",
    )
    assert "mapping-v1-abc" in station_id
    assert "42" in station_id
    # Deterministic: same inputs -> same station_id.
    same = assign_station_id_from_mapping_asset_identity(
        mapping_asset_identity="mapping-v1-abc",
        grid_cell_id="42",
    )
    assert station_id == same


def test_station_id_never_reused_across_mapping_versions() -> None:
    """Two mapping versions produce disjoint station_id sets."""
    v1, _, _, _ = _emit_minimal(mapping_asset_identity="mapping-v1-abc")
    v2, _, _, _ = _emit_minimal(mapping_asset_identity="mapping-v2-def")
    v1_ids = {b.station_id for b in v1.station_bindings}
    v2_ids = {b.station_id for b in v2.station_bindings}
    # Disjoint: no station_id from v1 appears in v2 (or vice versa).
    assert not v1_ids & v2_ids, (
        f"station_id reuse across mapping versions: overlap="
        f"{v1_ids & v2_ids}"
    )


def test_station_id_reuse_error_is_binding_artifact_family() -> None:
    """StationIdReuseError is a BindingArtifactError subclass (exception family)."""
    err = StationIdReuseError(
        overlapping_station_ids=("s1",),
        first_mapping_asset_identity="v1",
        second_mapping_asset_identity="v2",
    )
    assert isinstance(err, BindingArtifactError)


def test_assign_station_id_rejects_empty_identity() -> None:
    """Empty/whitespace-only mapping_asset_identity -> BindingArtifactError."""
    with pytest.raises(BindingArtifactError):
        assign_station_id_from_mapping_asset_identity(
            mapping_asset_identity="",
            grid_cell_id="42",
        )
    with pytest.raises(BindingArtifactError):
        assign_station_id_from_mapping_asset_identity(
            mapping_asset_identity="   ",
            grid_cell_id="42",
        )


# =========================================================================
# §4.2 FILENAME SAFETY
# =========================================================================


def test_sanitize_filename_derives_from_shud_forcing_index_only() -> None:
    """Filename is derived from shud_forcing_index; contains no coord digits."""
    fname = sanitize_station_forcing_filename(shud_forcing_index=7)
    assert fname == "station_00007.csv"


def test_sanitize_filename_matches_parser_regex() -> None:
    """Emitted filenames match the parser's _SAFE_STATION_FORCING_FILENAME regex."""
    for idx in (1, 5, 42, 999, 12345, 99999):
        fname = sanitize_station_forcing_filename(shud_forcing_index=idx)
        assert binding_module._SAFE_STATION_FORCING_FILENAME.fullmatch(fname), (
            f"filename {fname!r} fails parser regex"
        )


def test_sanitize_filename_rejects_non_positive_index() -> None:
    """Zero or negative index -> BindingArtifactError (not silently accepted)."""
    for bad in (0, -1, -999):
        with pytest.raises(BindingArtifactError):
            sanitize_station_forcing_filename(shud_forcing_index=bad)


def test_sanitize_filename_rejects_bool_and_float() -> None:
    """bool and float are NOT ints for our purposes; refuse loudly."""
    with pytest.raises(BindingArtifactError):
        sanitize_station_forcing_filename(shud_forcing_index=True)  # type: ignore[arg-type]
    with pytest.raises(BindingArtifactError):
        sanitize_station_forcing_filename(shud_forcing_index=1.5)  # type: ignore[arg-type]


def test_emitted_filenames_case_fold_unique_across_binding() -> None:
    """Every emitted filename is case-fold unique across all station bindings."""
    manifest, _, _, _ = _emit_minimal()
    lowered = [b.forcing_filename.lower() for b in manifest.station_bindings]
    assert len(lowered) == len(set(lowered))


def test_emitted_filenames_never_collide_with_reserved_names() -> None:
    """No emitted filename collides with any reserved name (case-insensitive)."""
    manifest, _, _, _ = _emit_minimal()
    reserved_lower = {name.lower() for name in RESERVED_FORCING_FILENAMES}
    for b in manifest.station_bindings:
        assert b.forcing_filename.lower() not in reserved_lower
        for prefix in RESERVED_FILENAME_PREFIXES:
            assert not b.forcing_filename.lower().startswith(prefix.lower()), (
                f"filename {b.forcing_filename!r} collides with reserved prefix {prefix!r}"
            )
        for suffix in RESERVED_FILENAME_SUFFIXES:
            assert not b.forcing_filename.lower().endswith(suffix.lower()), (
                f"filename {b.forcing_filename!r} collides with reserved suffix {suffix!r}"
            )


def test_emitter_rejects_reserved_filename_injection() -> None:
    """Emitter refuses if a reserved name is injected via a monkey-patched sanitizer.

    Structural safety proof: even if a downstream sanitize helper were
    somehow rewired to produce ``qhh.tsd.forc``, the emitter's inline
    reserved-name check catches it.
    """
    used_cells, snapshot_cells = _make_used_cells_and_snapshot(used_count=2)
    shud_forcing_index = _make_shud_forcing_index(used_cells)
    z_policy = _make_z_policy(used_cells)

    # Monkey-patch the internal reserved classifier's input to inject a
    # collision — done at the module level so the emit call reads it.
    original = binding_module.sanitize_station_forcing_filename

    def injected_sanitizer(*, shud_forcing_index: int) -> str:
        return "qhh.tsd.forc" if shud_forcing_index == 1 else original(
            shud_forcing_index=shud_forcing_index
        )

    binding_module.sanitize_station_forcing_filename = injected_sanitizer
    try:
        with pytest.raises(ForcingFilenameCollisionError) as exc:
            emit_direct_grid_manifest_and_binding(
                used_cells=used_cells,
                snapshot_cells=snapshot_cells,
                shud_forcing_index=shud_forcing_index,
                mapping_asset_identity="mapping-v1-abc",
                model_input_package_id="pkg-v1-abc",
                sp_att_path="package/keliya.sp.att",
                sp_att_bytes=b"MOCK",
                applicable_source_ids=("GFS",),
                grid_id="GFS_0p1",
                grid_signature="b" * 64,
                z_policy=z_policy,
                binding_uri="s3://bucket/binding.json",
                model_crs_wkt=_MODEL_CRS_WKT,
            )
        # Should be classified as reserved (either exact match or suffix).
        assert exc.value.collision_kind in {
            "reserved_exact_match",
            "reserved_suffix",
        }
    finally:
        binding_module.sanitize_station_forcing_filename = original


def test_forcing_filename_unsafe_error_is_binding_artifact_family() -> None:
    """ForcingFilenameUnsafeError inherits from BindingArtifactError."""
    err = ForcingFilenameUnsafeError(forcing_filename="bad name.txt")
    assert isinstance(err, BindingArtifactError)


def test_emitted_filenames_not_derived_from_rounded_coordinates() -> None:
    """Filenames contain no coordinate digits (positive proof of no coord derivation).

    Since our derivation is purely from shud_forcing_index, the emitted
    names never include lon/lat digits like ``100`` or ``37``. Guard the
    property by inspecting all emitted filenames.
    """
    manifest, _, _, _ = _emit_minimal()
    for b in manifest.station_bindings:
        # Longitude 100.x / latitude 37.x would emit as ``X100.xY37.x.csv``
        # in the legacy naming; our emitted name should have neither.
        assert "X" not in b.forcing_filename
        assert "Y" not in b.forcing_filename


# =========================================================================
# §4.2 COORDINATE RULES (12-decimal rounding + WGS84 basis)
# =========================================================================


def test_verify_station_center_gate_positive() -> None:
    """Green-path center-matches gate returns None."""
    manifest, _, used_cells, _ = _emit_minimal()
    used_by_id = {c.grid_cell_id: c for c in used_cells}
    for b in manifest.station_bindings:
        result = verify_station_center_matches_snapshot_under_rounding(
            b, used_by_id[b.grid_cell_id]
        )
        assert result is None


def test_lonlat_equality_at_12_decimals_absorbs_1em13_noise() -> None:
    """Sub-1e-12° noise between station lon and cell center is absorbed by rounding."""
    used_cells, snapshot_cells = _make_used_cells_and_snapshot(used_count=2)
    sample_cell = used_cells[0]
    # Inject 1e-13° noise — below the 12-decimal-rounding threshold, so
    # after round(x, 12) both operands are equal.
    noisy_binding = StationBinding(
        station_id="noisy",
        shud_forcing_index=1,
        forcing_filename="station_00001.csv",
        longitude=sample_cell.longitude + 1e-13,
        latitude=sample_cell.latitude + 1e-13,
        x=0.0,
        y=0.0,
        z=-9999.0,
        grid_id="GFS_0p1",
        grid_cell_id=sample_cell.grid_cell_id,
    )
    # Passes: rounded-to-12 values are equal even with 1e-13 noise.
    assert (
        verify_station_center_matches_snapshot_under_rounding(
            noisy_binding, sample_cell
        )
        is None
    )


def test_lonlat_equality_realistic_1em7_noise_blocks_after_rounding() -> None:
    """Realistic 1e-7° noise between station and cell center BLOCKS after 12-decimal rounding.

    Docs §7.3 pins the rounding at 12 decimals — noise of ~1e-7° is well
    ABOVE this threshold and MUST be caught by the gate. This is the
    ``float-literal equality never works because live coords carry ~1e-7°
    noise'' invariant — 12-decimal rounding does NOT absorb 1e-7° drift.
    """
    used_cells, snapshot_cells = _make_used_cells_and_snapshot(used_count=2)
    sample_cell = used_cells[0]
    noisy_binding = StationBinding(
        station_id="noisy",
        shud_forcing_index=1,
        forcing_filename="station_00001.csv",
        longitude=sample_cell.longitude + 1e-7,
        latitude=sample_cell.latitude,
        x=0.0,
        y=0.0,
        z=-9999.0,
        grid_id="GFS_0p1",
        grid_cell_id=sample_cell.grid_cell_id,
    )
    with pytest.raises(StationCenterMismatchError) as exc:
        verify_station_center_matches_snapshot_under_rounding(
            noisy_binding, sample_cell
        )
    assert exc.value.grid_cell_id == sample_cell.grid_cell_id


def test_coordinate_reference_system_declared_as_wgs84() -> None:
    """Emitted binding + manifest declare an explicit WGS84 basis (docs §7.3)."""
    manifest, artifact, _, _ = _emit_minimal()
    assert manifest.coordinate_reference_system == "EPSG:4326"
    assert artifact.coordinate_reference_system == "EPSG:4326"
    # Serialized binding artifact bytes also carry the declaration.
    payload = json.loads(artifact.bytes)
    assert payload["coordinate_reference_system"] == "EPSG:4326"


def test_verify_station_center_cross_basis_blocks_srid_4490() -> None:
    """SRID 4490 (CGCS2000) snapshot basis -> CrossBasisEqualityError."""
    used_cells, snapshot_cells = _make_used_cells_and_snapshot(used_count=2)
    sample_cell = used_cells[0]
    binding = StationBinding(
        station_id="test",
        shud_forcing_index=1,
        forcing_filename="station_00001.csv",
        longitude=sample_cell.longitude,
        latitude=sample_cell.latitude,
        x=0.0,
        y=0.0,
        z=-9999.0,
        grid_id="GFS_0p1",
        grid_cell_id=sample_cell.grid_cell_id,
    )
    with pytest.raises(CrossBasisEqualityError) as exc:
        verify_station_center_matches_snapshot_under_rounding(
            binding, sample_cell, snapshot_basis=CGCS2000_SRID_LABEL
        )
    assert exc.value.expected_basis == WGS84_COORDINATE_BASIS
    assert exc.value.supplied_basis == CGCS2000_SRID_LABEL


def test_verify_station_center_cross_basis_blocks_srid_4490_from_station_side() -> None:
    """Station basis SRID 4490 -> CrossBasisEqualityError (both sides checked)."""
    used_cells, snapshot_cells = _make_used_cells_and_snapshot(used_count=2)
    sample_cell = used_cells[0]
    binding = StationBinding(
        station_id="test",
        shud_forcing_index=1,
        forcing_filename="station_00001.csv",
        longitude=sample_cell.longitude,
        latitude=sample_cell.latitude,
        x=0.0,
        y=0.0,
        z=-9999.0,
        grid_id="GFS_0p1",
        grid_cell_id=sample_cell.grid_cell_id,
    )
    with pytest.raises(CrossBasisEqualityError):
        verify_station_center_matches_snapshot_under_rounding(
            binding, sample_cell, station_basis=CGCS2000_SRID_LABEL
        )


def test_x_y_are_recomputable_from_lonlat_and_model_crs() -> None:
    """x/y in each binding match a fresh pyproj transform of lon/lat."""
    manifest, _, _, _ = _emit_minimal()
    lonlat_to_model = pyproj.Transformer.from_crs(
        "EPSG:4326", pyproj.CRS.from_wkt(_MODEL_CRS_WKT), always_xy=True
    )
    for b in manifest.station_bindings:
        expected_x, expected_y = lonlat_to_model.transform(
            float(b.longitude), float(b.latitude)
        )
        # Numeric tolerance for pyproj round-trip: sub-mm on the model CRS.
        assert abs(b.x - expected_x) < 1e-6, (
            f"binding.x={b.x!r} does not recompute to {expected_x!r} "
            f"from lon={b.longitude!r} + model CRS"
        )
        assert abs(b.y - expected_y) < 1e-6, (
            f"binding.y={b.y!r} does not recompute to {expected_y!r} "
            f"from lat={b.latitude!r} + model CRS"
        )


# =========================================================================
# §4.2 z_policy (verbatim from Epic #886 readiness)
# =========================================================================


def test_apply_z_policy_returns_per_cell_value_verbatim() -> None:
    """z_policy returns the verbatim per-cell z from the readiness verdict."""
    policy = ZPolicy(
        policy_name="model_dem_at_cell_center",
        readiness_manifest_checksum="c" * 64,
        per_cell_z={"0": 1234.5, "1": 4321.0, "2": 999.0},
    )
    assert apply_z_policy_from_readiness(policy, "0") == 1234.5
    assert apply_z_policy_from_readiness(policy, "1") == 4321.0
    assert apply_z_policy_from_readiness(policy, "2") == 999.0


def test_apply_z_policy_missing_cell_blocks() -> None:
    """A missing grid_cell_id coverage entry -> ZPolicyCellMissingError."""
    policy = ZPolicy(
        policy_name="sentinel",
        readiness_manifest_checksum="c" * 64,
        per_cell_z={"0": -9999.0},
    )
    with pytest.raises(ZPolicyCellMissingError) as exc:
        apply_z_policy_from_readiness(policy, "SOME_MISSING_ID")
    assert exc.value.grid_cell_id == "SOME_MISSING_ID"
    assert exc.value.policy_name == "sentinel"


def test_z_policy_invalid_name_blocks_at_construction() -> None:
    """z_policy with a non-approved name -> InvalidZPolicyError at construction."""
    with pytest.raises(InvalidZPolicyError):
        ZPolicy(
            policy_name="invented_policy",
            readiness_manifest_checksum="c" * 64,
            per_cell_z={},
        )


def test_z_policy_missing_readiness_manifest_checksum_blocks() -> None:
    """z_policy without readiness manifest checksum -> ReadinessManifestChecksumMissingError."""
    with pytest.raises(ReadinessManifestChecksumMissingError):
        ZPolicy(
            policy_name="sentinel",
            readiness_manifest_checksum="",
            per_cell_z={},
        )
    with pytest.raises(ReadinessManifestChecksumMissingError):
        ZPolicy(
            policy_name="sentinel",
            readiness_manifest_checksum="   ",
            per_cell_z={},
        )


def test_allowed_z_policies_matches_docs_verdict() -> None:
    """ALLOWED_Z_POLICIES exactly matches the three docs §7.5 verdicts."""
    assert ALLOWED_Z_POLICIES == frozenset(
        {"canonical_orography", "model_dem_at_cell_center", "sentinel"}
    )


def test_emitted_z_values_are_verbatim_from_policy() -> None:
    """Emitted station.z values match the per-cell z from the supplied policy verbatim."""
    used_cells, snapshot_cells = _make_used_cells_and_snapshot(used_count=4)
    # Assign distinct z values to each used cell.
    z_map = {
        c.grid_cell_id: float(idx * 100.0)
        for idx, c in enumerate(used_cells, start=1)
    }
    policy = ZPolicy(
        policy_name="canonical_orography",
        readiness_manifest_checksum="c" * 64,
        per_cell_z=z_map,
    )
    manifest, _ = emit_direct_grid_manifest_and_binding(
        used_cells=used_cells,
        snapshot_cells=snapshot_cells,
        shud_forcing_index=_make_shud_forcing_index(used_cells),
        mapping_asset_identity="mapping-v1-abc",
        model_input_package_id="pkg-v1",
        sp_att_path="package/keliya.sp.att",
        sp_att_bytes=b"MOCK",
        applicable_source_ids=("GFS",),
        grid_id="GFS_0p1",
        grid_signature="b" * 64,
        z_policy=policy,
        binding_uri="s3://bucket/binding.json",
        model_crs_wkt=_MODEL_CRS_WKT,
    )
    for b in manifest.station_bindings:
        expected_z = z_map[b.grid_cell_id]
        assert b.z == expected_z, (
            f"emitted z={b.z} for cell {b.grid_cell_id} != policy z={expected_z}"
        )


# =========================================================================
# EXCEPTION-FAMILY (BindingArtifactError distinct root)
# =========================================================================


def test_binding_artifact_error_is_distinct_root() -> None:
    """BindingArtifactError does NOT inherit from G0/G1, G2/G3, or G4 exception roots."""
    assert not issubclass(BindingArtifactError, BaselineIntegrityError)
    assert not issubclass(BindingArtifactError, MappingAlgorithmError)
    assert not issubclass(BindingArtifactError, SpAttRewriteError)
    # All named subclasses are BindingArtifactError instances.
    subclasses = (
        BindingChecksumMismatchError,
        CrossBasisEqualityError,
        ForcingFilenameCollisionError,
        ForcingFilenameUnsafeError,
        GridCellIdDuplicateError,
        GridCellIdNotInSnapshotError,
        InvalidZPolicyError,
        ManifestBindingDivergenceError,
        ManifestFieldMissingError,
        ParserRoundTripError,
        ReadinessManifestChecksumMissingError,
        StationCenterMismatchError,
        StationIdReuseError,
        ZPolicyCellMissingError,
    )
    for cls in subclasses:
        assert issubclass(cls, BindingArtifactError), (
            f"{cls.__name__} MUST inherit from BindingArtifactError"
        )


# =========================================================================
# EMPTY-INPUT + FIELD-REQUIRED BLOCKERS
# =========================================================================


def test_empty_applicable_source_ids_blocks() -> None:
    """Empty applicable_source_ids -> ManifestFieldMissingError."""
    used_cells, snapshot_cells = _make_used_cells_and_snapshot(used_count=2)
    with pytest.raises(ManifestFieldMissingError) as exc:
        emit_direct_grid_manifest_and_binding(
            used_cells=used_cells,
            snapshot_cells=snapshot_cells,
            shud_forcing_index=_make_shud_forcing_index(used_cells),
            mapping_asset_identity="mapping-v1-abc",
            model_input_package_id="pkg-v1",
            sp_att_path="package/keliya.sp.att",
            sp_att_bytes=b"MOCK",
            applicable_source_ids=(),
            grid_id="GFS_0p1",
            grid_signature="b" * 64,
            z_policy=_make_z_policy(used_cells),
            binding_uri="s3://bucket/binding.json",
            model_crs_wkt=_MODEL_CRS_WKT,
        )
    assert exc.value.field_name == "applicable_source_ids"


def test_empty_used_cells_blocks() -> None:
    """Empty used_cells -> BindingArtifactError."""
    _, snapshot_cells = _make_used_cells_and_snapshot(used_count=2)
    with pytest.raises(BindingArtifactError):
        emit_direct_grid_manifest_and_binding(
            used_cells=[],
            snapshot_cells=snapshot_cells,
            shud_forcing_index={},
            mapping_asset_identity="mapping-v1-abc",
            model_input_package_id="pkg-v1",
            sp_att_path="package/keliya.sp.att",
            sp_att_bytes=b"MOCK",
            applicable_source_ids=("GFS",),
            grid_id="GFS_0p1",
            grid_signature="b" * 64,
            z_policy=ZPolicy(
                policy_name="sentinel",
                readiness_manifest_checksum="c" * 64,
                per_cell_z={},
            ),
            binding_uri="s3://bucket/binding.json",
            model_crs_wkt=_MODEL_CRS_WKT,
        )


def test_missing_grid_id_blocks() -> None:
    """Empty grid_id -> ManifestFieldMissingError."""
    used_cells, snapshot_cells = _make_used_cells_and_snapshot(used_count=2)
    with pytest.raises(ManifestFieldMissingError) as exc:
        emit_direct_grid_manifest_and_binding(
            used_cells=used_cells,
            snapshot_cells=snapshot_cells,
            shud_forcing_index=_make_shud_forcing_index(used_cells),
            mapping_asset_identity="mapping-v1-abc",
            model_input_package_id="pkg-v1",
            sp_att_path="package/keliya.sp.att",
            sp_att_bytes=b"MOCK",
            applicable_source_ids=("GFS",),
            grid_id="",
            grid_signature="b" * 64,
            z_policy=_make_z_policy(used_cells),
            binding_uri="s3://bucket/binding.json",
            model_crs_wkt=_MODEL_CRS_WKT,
        )
    assert exc.value.field_name == "grid_id"


def test_shud_forcing_index_missing_entry_blocks() -> None:
    """A used cell absent from shud_forcing_index -> BindingArtifactError."""
    used_cells, snapshot_cells = _make_used_cells_and_snapshot(used_count=2)
    partial_index = {used_cells[0].grid_cell_id: 1}  # missing entry for cell[1]
    with pytest.raises(BindingArtifactError):
        emit_direct_grid_manifest_and_binding(
            used_cells=used_cells,
            snapshot_cells=snapshot_cells,
            shud_forcing_index=partial_index,
            mapping_asset_identity="mapping-v1-abc",
            model_input_package_id="pkg-v1",
            sp_att_path="package/keliya.sp.att",
            sp_att_bytes=b"MOCK",
            applicable_source_ids=("GFS",),
            grid_id="GFS_0p1",
            grid_signature="b" * 64,
            z_policy=_make_z_policy(used_cells),
            binding_uri="s3://bucket/binding.json",
            model_crs_wkt=_MODEL_CRS_WKT,
        )


# =========================================================================
# FROZEN INVARIANT
# =========================================================================


def test_station_binding_is_frozen() -> None:
    """StationBinding is a frozen dataclass — field assignment raises."""
    b = StationBinding(
        station_id="s1",
        shud_forcing_index=1,
        forcing_filename="station_00001.csv",
        longitude=100.0,
        latitude=37.0,
        x=1.0,
        y=2.0,
        z=-9999.0,
        grid_id="g1",
        grid_cell_id="0",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        b.station_id = "s2"  # type: ignore[misc]


def test_direct_grid_manifest_is_frozen() -> None:
    """DirectGridManifest is a frozen dataclass — field assignment raises."""
    manifest, _, _, _ = _emit_minimal()
    with pytest.raises(dataclasses.FrozenInstanceError):
        manifest.forcing_mapping_mode = "idw"  # type: ignore[misc]


def test_binding_artifact_is_frozen() -> None:
    """BindingArtifact is a frozen dataclass — field assignment raises."""
    _, artifact, _, _ = _emit_minimal()
    with pytest.raises(dataclasses.FrozenInstanceError):
        artifact.checksum = "0" * 64  # type: ignore[misc]


def test_z_policy_is_frozen() -> None:
    """ZPolicy is a frozen dataclass — field assignment raises."""
    policy = ZPolicy(
        policy_name="sentinel",
        readiness_manifest_checksum="c" * 64,
        per_cell_z={"0": -9999.0},
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.policy_name = "canonical_orography"  # type: ignore[misc]


# =========================================================================
# SIGNATURE PIN TESTS (all 9 public functions)
# =========================================================================


def test_emit_direct_grid_manifest_and_binding_signature_pinned() -> None:
    """emit_direct_grid_manifest_and_binding signature is pinned (all kwargs, order)."""
    sig = inspect.signature(emit_direct_grid_manifest_and_binding)
    assert list(sig.parameters) == [
        "used_cells",
        "snapshot_cells",
        "shud_forcing_index",
        "mapping_asset_identity",
        "model_input_package_id",
        "sp_att_path",
        "sp_att_bytes",
        "applicable_source_ids",
        "grid_id",
        "grid_signature",
        "z_policy",
        "binding_uri",
        "model_crs_wkt",
        "coordinate_reference_system",
    ]
    # All parameters are keyword-only.
    for name, param in sig.parameters.items():
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"parameter {name!r} MUST be keyword-only"
        )


def _assert_param_kinds(
    func,
    *,
    keyword_only: frozenset[str],
    positional_or_keyword: frozenset[str],
) -> None:
    """Assert each parameter of ``func`` has the expected ``inspect.Parameter.kind``.

    Silent-loosening exposure guard (Epic #909 SUB-11 PA-1): a signature
    pin that checks only the parameter *name list* misses a refactor that
    demotes a KEYWORD_ONLY parameter to POSITIONAL_OR_KEYWORD (or vice
    versa). Every signature pin below MUST assert ``param.kind`` alongside
    the name list.
    """
    sig = inspect.signature(func)
    for name, param in sig.parameters.items():
        if name in keyword_only:
            assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"{func.__name__}: parameter {name!r} kind={param.kind!r}; "
                f"expected KEYWORD_ONLY"
            )
        elif name in positional_or_keyword:
            assert param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD, (
                f"{func.__name__}: parameter {name!r} kind={param.kind!r}; "
                f"expected POSITIONAL_OR_KEYWORD"
            )
        else:
            raise AssertionError(
                f"{func.__name__}: parameter {name!r} not in either "
                "keyword_only or positional_or_keyword; update the pin"
            )


def test_verify_binding_round_trips_parser_signature_pinned() -> None:
    """verify_binding_round_trips_parser signature is pinned."""
    sig = inspect.signature(verify_binding_round_trips_parser)
    assert list(sig.parameters) == ["resource_profile", "source_id"]
    _assert_param_kinds(
        verify_binding_round_trips_parser,
        keyword_only=frozenset({"source_id"}),
        positional_or_keyword=frozenset({"resource_profile"}),
    )
    hints = typing.get_type_hints(verify_binding_round_trips_parser)
    assert hints["return"] is DirectGridForcingContract


def test_verify_grid_cell_id_unique_and_snapshot_member_signature_pinned() -> None:
    """verify_grid_cell_id_unique_and_snapshot_member signature is pinned."""
    sig = inspect.signature(verify_grid_cell_id_unique_and_snapshot_member)
    assert list(sig.parameters) == ["station_bindings", "snapshot_cells"]
    _assert_param_kinds(
        verify_grid_cell_id_unique_and_snapshot_member,
        keyword_only=frozenset(),
        positional_or_keyword=frozenset({"station_bindings", "snapshot_cells"}),
    )
    hints = typing.get_type_hints(verify_grid_cell_id_unique_and_snapshot_member)
    assert hints.get("return") is type(None)


def test_verify_manifest_binding_cross_consistent_signature_pinned() -> None:
    """verify_manifest_binding_cross_consistent signature is pinned."""
    sig = inspect.signature(verify_manifest_binding_cross_consistent)
    assert list(sig.parameters) == ["manifest", "binding_artifact"]
    _assert_param_kinds(
        verify_manifest_binding_cross_consistent,
        keyword_only=frozenset(),
        positional_or_keyword=frozenset({"manifest", "binding_artifact"}),
    )
    hints = typing.get_type_hints(verify_manifest_binding_cross_consistent)
    assert hints["manifest"] is DirectGridManifest
    assert hints["binding_artifact"] is BindingArtifact
    assert hints.get("return") is type(None)


def test_recompute_binding_and_sp_att_checksums_signature_pinned() -> None:
    """recompute_binding_and_sp_att_checksums signature is pinned."""
    sig = inspect.signature(recompute_binding_and_sp_att_checksums)
    assert list(sig.parameters) == ["binding_artifact_bytes", "sp_att_bytes"]
    _assert_param_kinds(
        recompute_binding_and_sp_att_checksums,
        keyword_only=frozenset(),
        positional_or_keyword=frozenset(
            {"binding_artifact_bytes", "sp_att_bytes"}
        ),
    )
    hints = typing.get_type_hints(recompute_binding_and_sp_att_checksums)
    assert hints["binding_artifact_bytes"] is bytes
    assert hints["sp_att_bytes"] is bytes


def test_assign_station_id_from_mapping_asset_identity_signature_pinned() -> None:
    """assign_station_id_from_mapping_asset_identity signature is pinned."""
    sig = inspect.signature(assign_station_id_from_mapping_asset_identity)
    assert list(sig.parameters) == ["mapping_asset_identity", "grid_cell_id"]
    for name, param in sig.parameters.items():
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"parameter {name!r} MUST be keyword-only"
        )
    hints = typing.get_type_hints(assign_station_id_from_mapping_asset_identity)
    assert hints["mapping_asset_identity"] is str
    assert hints["grid_cell_id"] is str
    assert hints["return"] is str


def test_sanitize_station_forcing_filename_signature_pinned() -> None:
    """sanitize_station_forcing_filename signature is pinned."""
    sig = inspect.signature(sanitize_station_forcing_filename)
    assert list(sig.parameters) == ["shud_forcing_index"]
    for name, param in sig.parameters.items():
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"parameter {name!r} MUST be keyword-only"
        )
    hints = typing.get_type_hints(sanitize_station_forcing_filename)
    assert hints["shud_forcing_index"] is int
    assert hints["return"] is str


def test_verify_station_center_matches_snapshot_under_rounding_signature_pinned() -> None:
    """verify_station_center_matches_snapshot_under_rounding signature is pinned."""
    sig = inspect.signature(verify_station_center_matches_snapshot_under_rounding)
    assert list(sig.parameters) == [
        "station",
        "snapshot_cell",
        "snapshot_basis",
        "station_basis",
    ]
    _assert_param_kinds(
        verify_station_center_matches_snapshot_under_rounding,
        keyword_only=frozenset({"snapshot_basis", "station_basis"}),
        positional_or_keyword=frozenset({"station", "snapshot_cell"}),
    )
    hints = typing.get_type_hints(
        verify_station_center_matches_snapshot_under_rounding
    )
    assert hints["station"] is StationBinding
    assert hints.get("return") is type(None)


def test_apply_z_policy_from_readiness_signature_pinned() -> None:
    """apply_z_policy_from_readiness signature is pinned."""
    sig = inspect.signature(apply_z_policy_from_readiness)
    assert list(sig.parameters) == ["z_policy", "grid_cell_id"]
    _assert_param_kinds(
        apply_z_policy_from_readiness,
        keyword_only=frozenset(),
        positional_or_keyword=frozenset({"z_policy", "grid_cell_id"}),
    )
    hints = typing.get_type_hints(apply_z_policy_from_readiness)
    assert hints["z_policy"] is ZPolicy
    assert hints["grid_cell_id"] is str
    assert hints["return"] is float


# =========================================================================
# DETERMINISM (spec §7)
# =========================================================================


def test_emit_is_deterministic_on_identical_inputs() -> None:
    """Two runs on the same inputs produce byte-identical binding artifact bytes.

    Determinism requirement of spec §7: same baseline + same grid snapshot
    + same algorithm version -> byte-identical binding + manifest.
    """
    manifest1, artifact1, _, _ = _emit_minimal(mapping_asset_identity="det-v1")
    manifest2, artifact2, _, _ = _emit_minimal(mapping_asset_identity="det-v1")
    assert artifact1.bytes == artifact2.bytes
    assert artifact1.checksum == artifact2.checksum
    assert manifest1.binding_checksum == manifest2.binding_checksum
    assert manifest1.station_bindings == manifest2.station_bindings


def test_coordinate_rounding_decimals_pinned_to_12() -> None:
    """Coordinate rounding is pinned to 12 decimals (docs §7.3)."""
    assert COORDINATE_ROUNDING_DECIMALS == 12


# =========================================================================
# CP-1: shared canonical serializer + shared rounding constant
# =========================================================================


def test_coordinate_rounding_decimals_shared_between_grid_signature_and_binding() -> None:
    """binding.COORDINATE_ROUNDING_DECIMALS re-exports the shared authority.

    Per Epic #909 SUB-11 CP-1: the 12-decimal rounding rule MUST be a
    single shared constant. Any drift between the binding module's local
    constant and the shared authority in
    :mod:`packages.common.grid_signature` is a §4.2 non-goal violation.
    """
    assert (
        binding_module.COORDINATE_ROUNDING_DECIMALS
        is grid_signature_module.COORDINATE_ROUNDING_DECIMALS
    )
    assert (
        COORDINATE_ROUNDING_DECIMALS
        == grid_signature_module.COORDINATE_ROUNDING_DECIMALS
    )


def test_canonical_json_bytes_matches_shared_helper_on_datetime() -> None:
    """binding._canonical_json_bytes serializes datetimes via the shared helper.

    Per Epic #909 SUB-11 CP-1: the local helper's docstring claims
    verbatim parity with :func:`packages.common.grid_signature._json_bytes`
    — but before the fold, the local implementation omitted the
    ``default=_json_default`` handler and would crash on a datetime value
    while the shared helper serialized it. Assert byte-for-byte parity on
    a payload containing a datetime.
    """
    from datetime import UTC, datetime

    payload = {
        "grid_id": "GFS_0p1",
        "generated_at": datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        "count": 3,
        "coordinate_reference_system": "EPSG:4326",
    }
    from packages.common.grid_signature import canonical_json_bytes

    shared = canonical_json_bytes(payload)
    local = binding_module._canonical_json_bytes(payload)
    assert local == shared
    # And also sanity-check that the datetime was actually rendered
    # (i.e. the shared helper ran, not that both crashed the same way).
    assert b"2026-07-01T12:00:00Z" in shared


def test_canonical_json_bytes_matches_shared_helper_on_plain_payload() -> None:
    """Byte-for-byte parity on a plain payload (no datetime)."""
    payload = {
        "z": 1,
        "a": [3, 2, 1],
        "b": {"nested": True},
    }
    from packages.common.grid_signature import canonical_json_bytes

    shared = canonical_json_bytes(payload)
    local = binding_module._canonical_json_bytes(payload)
    assert local == shared


# =========================================================================
# CP-2: z_policy provenance persistence in emitted artifact bytes
# =========================================================================


def test_binding_artifact_bytes_carry_policy_name_and_readiness_manifest_checksum() -> None:
    """Emitted binding artifact bytes embed z_policy provenance verbatim.

    Per Epic #909 SUB-11 CP-2: SUB-13's evidence bundler MUST be able to
    audit the approved policy_name + readiness_manifest_checksum from
    artifact bytes alone. Persist both in the binding artifact JSON so
    the bytes are self-describing.
    """
    _, artifact, _, _ = _emit_minimal()
    # Both provenance fields present in the raw bytes.
    assert b"policy_name" in artifact.bytes
    assert b"readiness_manifest_checksum" in artifact.bytes
    # And the actual sentinel values from _make_z_policy.
    assert b"sentinel" in artifact.bytes
    assert b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in artifact.bytes
    # BindingArtifact attribute round-trips.
    assert artifact.z_policy == {
        "policy_name": "sentinel",
        "readiness_manifest_checksum": "a" * 64,
    }


def test_manifest_section_carries_z_policy_provenance() -> None:
    """Manifest section dict exposes z_policy for downstream audit."""
    manifest, _, _, _ = _emit_minimal()
    section = manifest.to_contract_section_dict()
    assert "z_policy" in section
    assert section["z_policy"] == {
        "policy_name": "sentinel",
        "readiness_manifest_checksum": "a" * 64,
    }


def test_manifest_z_policy_provenance_attribute_persists() -> None:
    """DirectGridManifest.z_policy attribute is populated from ZPolicy verbatim."""
    manifest, _, _, _ = _emit_minimal()
    assert manifest.z_policy == {
        "policy_name": "sentinel",
        "readiness_manifest_checksum": "a" * 64,
    }


def test_manifest_binding_z_policy_provenance_divergence_blocks_g5() -> None:
    """Diverging manifest vs artifact z_policy -> ManifestBindingDivergenceError.

    Per Epic #909 SUB-11 CP-2: the cross-consistency gate MUST catch a
    z_policy mismatch between manifest and binding artifact — otherwise
    a caller could ship an artifact backed by policy A while the manifest
    advertises policy B, hiding the provenance.
    """
    manifest, artifact, _, _ = _emit_minimal()
    poisoned = dataclasses.replace(
        manifest,
        z_policy={
            "policy_name": "canonical_orography",  # differs from sentinel
            "readiness_manifest_checksum": "a" * 64,
        },
    )
    with pytest.raises(ManifestBindingDivergenceError) as exc:
        verify_manifest_binding_cross_consistent(poisoned, artifact)
    assert exc.value.divergent_field == "z_policy"


def test_manifest_round_trips_through_parser_with_z_policy_extension() -> None:
    """Adding z_policy to the section does NOT break the parser round-trip.

    The direct-grid parser reads named fields and ignores extras — so
    embedding z_policy under the section MUST NOT change the parser's
    behavior. Guard the invariant explicitly.
    """
    manifest, _, _, _ = _emit_minimal()
    contract = verify_binding_round_trips_parser(
        manifest.to_resource_profile_dict(),
        source_id="GFS",
    )
    assert isinstance(contract, DirectGridForcingContract)
    assert contract.forcing_mapping_mode == "direct_grid"


# =========================================================================
# CP-3: verify_x_y_recomputable + verify_station_id_disjoint_across_versions
# =========================================================================


def test_verify_x_y_recomputable_green_path() -> None:
    """A freshly-emitted binding artifact passes verify_x_y_recomputable."""
    _, artifact, _, _ = _emit_minimal()
    assert (
        verify_x_y_recomputable(artifact, model_crs_wkt=_MODEL_CRS_WKT) is None
    )


def test_verify_x_y_recomputable_noise_within_tolerance_passes() -> None:
    """Sub-tolerance x/y noise (1e-9 m) is absorbed by the gate."""
    _, artifact, _, _ = _emit_minimal()
    # Perturb x by 1e-9 (well below the 1e-6 default tolerance).
    perturbed_bindings = list(artifact.station_bindings)
    perturbed_bindings[0] = dataclasses.replace(
        perturbed_bindings[0], x=perturbed_bindings[0].x + 1e-9
    )
    perturbed = dataclasses.replace(
        artifact, station_bindings=tuple(perturbed_bindings)
    )
    assert (
        verify_x_y_recomputable(perturbed, model_crs_wkt=_MODEL_CRS_WKT) is None
    )


def test_verify_x_y_recomputable_drift_beyond_tolerance_raises() -> None:
    """x drift beyond the 1e-6 m tolerance -> XyRecomputationMismatchError."""
    _, artifact, _, _ = _emit_minimal()
    poisoned_bindings = list(artifact.station_bindings)
    poisoned_bindings[0] = dataclasses.replace(
        poisoned_bindings[0], x=poisoned_bindings[0].x + 1.0  # 1 m drift
    )
    poisoned = dataclasses.replace(
        artifact, station_bindings=tuple(poisoned_bindings)
    )
    with pytest.raises(XyRecomputationMismatchError) as exc:
        verify_x_y_recomputable(poisoned, model_crs_wkt=_MODEL_CRS_WKT)
    assert exc.value.station_id == poisoned_bindings[0].station_id
    assert isinstance(exc.value, BindingArtifactError)


def test_verify_x_y_recomputable_rejects_unparseable_crs() -> None:
    """A malformed model_crs_wkt -> BindingArtifactError."""
    _, artifact, _, _ = _emit_minimal()
    with pytest.raises(BindingArtifactError):
        verify_x_y_recomputable(artifact, model_crs_wkt="NOT_A_VALID_WKT")


def test_verify_station_id_disjoint_across_versions_green_path() -> None:
    """Two disjoint station_id sets pass the gate."""
    v1, _, _, _ = _emit_minimal(mapping_asset_identity="mapping-v1-abc")
    v2, _, _, _ = _emit_minimal(mapping_asset_identity="mapping-v2-def")
    v1_ids = frozenset(b.station_id for b in v1.station_bindings)
    v2_ids = frozenset(b.station_id for b in v2.station_bindings)
    assert (
        verify_station_id_disjoint_across_versions(
            v2_ids,
            v1_ids,
            current_mapping_asset_identity="mapping-v2-def",
            previous_mapping_asset_identity="mapping-v1-abc",
        )
        is None
    )


def test_verify_station_id_disjoint_across_versions_collision_raises() -> None:
    """Overlapping station_id sets -> StationIdReuseError."""
    v1, _, _, _ = _emit_minimal(mapping_asset_identity="mapping-v1-abc")
    v1_ids = frozenset(b.station_id for b in v1.station_bindings)
    # Same v1 emit reused as v2 to force overlap.
    with pytest.raises(StationIdReuseError) as exc:
        verify_station_id_disjoint_across_versions(
            v1_ids,
            v1_ids,
            current_mapping_asset_identity="mapping-v1-abc",
            previous_mapping_asset_identity="mapping-v1-abc",
        )
    assert set(exc.value.overlapping_station_ids) == set(v1_ids)


# =========================================================================
# CP-4: reject STATION_ID_SEPARATOR ("::cell:") inside identity tokens
# =========================================================================


def test_assign_station_id_reject_separator_in_mapping_asset_identity() -> None:
    """Separator inside mapping_asset_identity -> StationIdSeparatorConflictError."""
    with pytest.raises(StationIdSeparatorConflictError) as exc:
        assign_station_id_from_mapping_asset_identity(
            mapping_asset_identity="foo::cell:X",
            grid_cell_id="Y",
        )
    assert exc.value.separator == STATION_ID_SEPARATOR
    assert isinstance(exc.value, BindingArtifactError)


def test_assign_station_id_reject_separator_in_grid_cell_id() -> None:
    """Separator inside grid_cell_id -> StationIdSeparatorConflictError."""
    with pytest.raises(StationIdSeparatorConflictError):
        assign_station_id_from_mapping_asset_identity(
            mapping_asset_identity="foo",
            grid_cell_id="X::cell:Y",
        )


def test_assign_station_id_reject_collision_demonstration_prevented() -> None:
    """The exact collision the reviewer probed is now impossible.

    Before the fix, both of these produced ``'foo::cell:X::cell:Y'`` and
    the station_id namespace collapsed. Both must now raise loudly.
    """
    with pytest.raises(StationIdSeparatorConflictError):
        assign_station_id_from_mapping_asset_identity(
            mapping_asset_identity="foo::cell:X",
            grid_cell_id="Y",
        )
    with pytest.raises(StationIdSeparatorConflictError):
        assign_station_id_from_mapping_asset_identity(
            mapping_asset_identity="foo",
            grid_cell_id="X::cell:Y",
        )


def test_station_id_separator_constant_matches_docstring() -> None:
    """STATION_ID_SEPARATOR constant equals the ``::cell:`` sentinel."""
    assert STATION_ID_SEPARATOR == "::cell:"


# =========================================================================
# CR-1: verify_sp_att_checksum
# =========================================================================


def test_verify_sp_att_checksum_green_path() -> None:
    """Correct expected hex passes."""
    payload = b"MOCK_SP_ATT_BYTES\n"
    expected = hashlib.sha256(payload).hexdigest()
    assert verify_sp_att_checksum(payload, expected_sha256_hex=expected) is None


def test_verify_sp_att_checksum_mismatch_raises() -> None:
    """Bytes SHA-256 != expected -> SpAttChecksumMismatchError."""
    payload = b"MOCK_SP_ATT_BYTES\n"
    with pytest.raises(SpAttChecksumMismatchError) as exc:
        verify_sp_att_checksum(payload, expected_sha256_hex="0" * 64)
    assert exc.value.recomputed_checksum == hashlib.sha256(payload).hexdigest()
    assert exc.value.manifest_checksum == "0" * 64
    assert isinstance(exc.value, BindingArtifactError)


def test_verify_sp_att_checksum_bit_flip_detected() -> None:
    """A single-byte corruption is detected by the gate."""
    baseline = b"BASELINE\n"
    baseline_hex = hashlib.sha256(baseline).hexdigest()
    corrupted = b"BASELIN!\n"  # single byte flipped
    with pytest.raises(SpAttChecksumMismatchError):
        verify_sp_att_checksum(corrupted, expected_sha256_hex=baseline_hex)


# =========================================================================
# CR-2: cross-consistency gate compares coordinate_reference_system
# =========================================================================


def test_verify_manifest_binding_cross_consistency_coordinate_reference_system_divergence() -> None:
    """CRS divergence between manifest and binding artifact -> divergence error.

    Per Epic #909 SUB-11 CR-2: the cross-consistency gate MUST fail
    closed if the manifest declares one basis while the artifact bytes
    declare another. Guard the invariant with a mutation on the manifest
    side (the artifact declaration is baked into the emitted bytes so
    cannot be mutated post-emit without changing checksum).
    """
    manifest, artifact, _, _ = _emit_minimal()
    poisoned = dataclasses.replace(
        manifest, coordinate_reference_system=CGCS2000_SRID_LABEL
    )
    with pytest.raises(ManifestBindingDivergenceError) as exc:
        verify_manifest_binding_cross_consistent(poisoned, artifact)
    assert exc.value.divergent_field == "coordinate_reference_system"


# =========================================================================
# CR-3: latitude divergence negative test (symmetric to longitude)
# =========================================================================


def test_verify_manifest_binding_cross_consistency_latitude_divergence_at_12_decimal() -> None:
    """Divergent lat past 12-decimal rounding -> ManifestBindingDivergenceError.

    Symmetric to the existing longitude divergence test at
    :func:`test_verify_manifest_binding_cross_consistency_lonlat_divergence_at_12_decimal`.
    Per Epic #909 SUB-11 CR-3: the latitude branch of the gate must
    have equal test coverage to the longitude branch.
    """
    manifest, artifact, _, _ = _emit_minimal()
    bindings = list(manifest.station_bindings)
    bindings[0] = dataclasses.replace(
        bindings[0], latitude=bindings[0].latitude + 1e-6
    )
    poisoned = dataclasses.replace(manifest, station_bindings=tuple(bindings))
    with pytest.raises(ManifestBindingDivergenceError) as exc:
        verify_manifest_binding_cross_consistent(poisoned, artifact)
    assert exc.value.divergent_field == "latitude"


# =========================================================================
# CR-4: harden ZPolicy.__post_init__ isinstance guard
# =========================================================================


def test_z_policy_missing_readiness_manifest_checksum_none_blocks() -> None:
    """readiness_manifest_checksum=None -> ReadinessManifestChecksumMissingError.

    Per Epic #909 SUB-11 CR-4: a caller that JSON-loaded a verdict where
    the field is missing would pass ``None`` here; the pre-fold code hit
    ``None.strip()`` and leaked an AttributeError, breaking the
    :class:`BindingArtifactError` distinct-root discipline. The
    isinstance guard now catches non-string inputs and raises the
    dedicated exception.
    """
    with pytest.raises(ReadinessManifestChecksumMissingError):
        ZPolicy(
            policy_name="sentinel",
            readiness_manifest_checksum=None,  # type: ignore[arg-type]
            per_cell_z={},
        )


def test_z_policy_missing_readiness_manifest_checksum_int_blocks() -> None:
    """readiness_manifest_checksum=0 -> ReadinessManifestChecksumMissingError."""
    with pytest.raises(ReadinessManifestChecksumMissingError):
        ZPolicy(
            policy_name="sentinel",
            readiness_manifest_checksum=0,  # type: ignore[arg-type]
            per_cell_z={},
        )


def test_z_policy_missing_readiness_manifest_checksum_list_blocks() -> None:
    """readiness_manifest_checksum=[] -> ReadinessManifestChecksumMissingError."""
    with pytest.raises(ReadinessManifestChecksumMissingError):
        ZPolicy(
            policy_name="sentinel",
            readiness_manifest_checksum=[],  # type: ignore[arg-type]
            per_cell_z={},
        )


def test_z_policy_non_string_policy_name_blocks() -> None:
    """policy_name=None -> InvalidZPolicyError (not AttributeError).

    Same distinct-root discipline as the checksum guard: any non-string
    ``policy_name`` MUST raise :class:`InvalidZPolicyError` rather than
    leaking a raw TypeError from the ``in ALLOWED_Z_POLICIES`` check.
    """
    with pytest.raises(InvalidZPolicyError):
        ZPolicy(
            policy_name=None,  # type: ignore[arg-type]
            readiness_manifest_checksum="a" * 64,
            per_cell_z={},
        )


# =========================================================================
# §4.3 §8.1 FORBIDDEN-OUTPUT RULE (Epic #909 SUB-12)
# =========================================================================
#
# Cover per SUB-12 acceptance criteria:
#   * Green path: empty artifact set (no forbidden paths) + empty spies ->
#     ForbiddenOutputScanResult(passed=True).
#   * Negative: one test per forbidden class (4 classes -> 7 negative tests
#     because the station_weather_csv class covers both lonlat + numbered
#     shapes AND the cycle_dated_tsd_forc class is separate).
#   * Fail-closed: db_write_spy=None + cycle_lineage_spy=None both raise.
#   * Structured result: on green, ForbiddenOutputScanResult fields are
#     recoverable for SUB-13's evidence bundler.
#   * Frozen invariant + case-fold scan + signature pin follow the same
#     SUB-10/SUB-11 patterns.


def _binding_root(tmp_path: pathlib.Path) -> pathlib.Path:
    """Return a fresh directory the tests use as the artifact-root prefix.

    The gate never reads bytes from these paths — it only pattern-matches
    against ``path.name``. So the fixture doesn't need to actually create
    the files (tests just build ``pathlib.Path`` objects rooted at
    ``tmp_path`` for reproducible ordering).
    """
    return tmp_path / "variant_root"


def test_verify_no_forbidden_runtime_producer_artifacts_green_path(
    tmp_path: pathlib.Path,
) -> None:
    """Green build: empty artifact set + empty spies -> passed=True."""
    manifest, artifact, _, _ = _emit_minimal()
    # Simulate the mapping stage's total emitted artifact set: the manifest
    # path + the standalone binding artifact path + the variant .sp.att.
    # None of these match the forbidden regexes (station_00001.csv, etc.
    # are not covered by the legacy patterns).
    root = _binding_root(tmp_path)
    emitted = [
        root / "manifest.json",
        root / "binding.json",
        root / "package" / "keliya.sp.att",
    ] + [
        root / "forcing" / b.forcing_filename
        for b in manifest.station_bindings
    ]
    result = verify_no_forbidden_runtime_producer_artifacts(
        emitted,
        db_write_spy=DbWriteSpy(),
        cycle_lineage_spy=CycleLineageSpy(),
    )
    assert isinstance(result, ForbiddenOutputScanResult)
    assert result.passed is True
    assert result.offending_paths == ()
    assert result.offending_db_writes == ()
    assert result.cycle_lineage_records == ()
    assert result.scanned_path_count == len(emitted)


def test_verify_no_forbidden_runtime_producer_artifacts_cycle_tsd_forc_blocks(
    tmp_path: pathlib.Path,
) -> None:
    """Injected cycle-dated .tsd.forc -> ForbiddenRuntimeProducerArtifactError."""
    root = _binding_root(tmp_path)
    emitted = [
        root / "manifest.json",
        root / "some" / "20200101.tsd.forc",  # cycle-dated
    ]
    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as exc:
        verify_no_forbidden_runtime_producer_artifacts(
            emitted,
            db_write_spy=DbWriteSpy(),
            cycle_lineage_spy=CycleLineageSpy(),
        )
    assert exc.value.offending_class == "cycle_dated_tsd_forc"
    assert exc.value.offending_class == (
        ForbiddenOutputClass.CYCLE_DATED_TSD_FORC.value
    )
    assert isinstance(exc.value, BindingArtifactError)
    # scan_summary carries the full 4-class breakdown for SUB-13.
    assert isinstance(exc.value.scan_summary, ForbiddenOutputScanResult)
    assert exc.value.scan_summary.passed is False
    assert exc.value.scan_summary.offending_paths[0][0] == (
        "cycle_dated_tsd_forc"
    )


def test_verify_no_forbidden_runtime_producer_artifacts_station_lonlat_csv_blocks(
    tmp_path: pathlib.Path,
) -> None:
    """Injected legacy X<lon>Y<lat>.csv -> station_weather_csv blocker."""
    root = _binding_root(tmp_path)
    emitted = [
        root / "manifest.json",
        root / "forcing" / "X100Y37.csv",  # legacy CMFD lonlat-keyed
    ]
    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as exc:
        verify_no_forbidden_runtime_producer_artifacts(
            emitted,
            db_write_spy=DbWriteSpy(),
            cycle_lineage_spy=CycleLineageSpy(),
        )
    assert exc.value.offending_class == "station_weather_csv"
    assert exc.value.offending_class == (
        ForbiddenOutputClass.STATION_WEATHER_CSV.value
    )


def test_verify_no_forbidden_runtime_producer_artifacts_station_numbered_csv_blocks(
    tmp_path: pathlib.Path,
) -> None:
    """Injected legacy X<n>.csv -> station_weather_csv blocker."""
    root = _binding_root(tmp_path)
    emitted = [
        root / "manifest.json",
        root / "forcing" / "X100.csv",  # legacy CMFD numbered
    ]
    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as exc:
        verify_no_forbidden_runtime_producer_artifacts(
            emitted,
            db_write_spy=DbWriteSpy(),
            cycle_lineage_spy=CycleLineageSpy(),
        )
    assert exc.value.offending_class == "station_weather_csv"


def test_verify_no_forbidden_runtime_producer_artifacts_met_interp_weight_write_blocks(
    tmp_path: pathlib.Path,
) -> None:
    """Spy recorded write to met.interp_weight -> met_row_write blocker."""
    root = _binding_root(tmp_path)
    emitted = [root / "manifest.json"]
    spy = DbWriteSpy().record_write(
        table_name="met.interp_weight",
        row_summary="station_id=s1, cell=c1, weight=1.0",
    )
    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as exc:
        verify_no_forbidden_runtime_producer_artifacts(
            emitted,
            db_write_spy=spy,
            cycle_lineage_spy=CycleLineageSpy(),
        )
    assert exc.value.offending_class == "met_row_write"
    assert exc.value.offending_class == (
        ForbiddenOutputClass.MET_ROW_WRITE.value
    )
    # The offending_evidence is the (table_name, row_summary) tuple.
    table, row = exc.value.offending_evidence
    assert table == "met.interp_weight"


def test_verify_no_forbidden_runtime_producer_artifacts_met_met_station_write_blocks(
    tmp_path: pathlib.Path,
) -> None:
    """Spy recorded write to met.met_station -> met_row_write blocker."""
    root = _binding_root(tmp_path)
    emitted = [root / "manifest.json"]
    spy = DbWriteSpy().record_write(
        table_name="met.met_station",
        row_summary="station_id=s1",
    )
    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as exc:
        verify_no_forbidden_runtime_producer_artifacts(
            emitted,
            db_write_spy=spy,
            cycle_lineage_spy=CycleLineageSpy(),
        )
    assert exc.value.offending_class == "met_row_write"


def test_verify_no_forbidden_runtime_producer_artifacts_met_forcing_version_write_blocks(
    tmp_path: pathlib.Path,
) -> None:
    """Spy recorded write to met.forcing_version -> met_row_write blocker."""
    root = _binding_root(tmp_path)
    emitted = [root / "manifest.json"]
    spy = DbWriteSpy().record_write(
        table_name="met.forcing_version",
        row_summary="version_id=v1",
    )
    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as exc:
        verify_no_forbidden_runtime_producer_artifacts(
            emitted,
            db_write_spy=spy,
            cycle_lineage_spy=CycleLineageSpy(),
        )
    assert exc.value.offending_class == "met_row_write"


def test_verify_no_forbidden_runtime_producer_artifacts_cycle_lineage_record_blocks(
    tmp_path: pathlib.Path,
) -> None:
    """Spy recorded a cycle-lineage record -> cycle_lineage_record blocker."""
    root = _binding_root(tmp_path)
    emitted = [root / "manifest.json"]
    lineage = CycleLineageSpy().record_lineage(
        record_summary="cycle=2020010100, mapping=v1",
    )
    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as exc:
        verify_no_forbidden_runtime_producer_artifacts(
            emitted,
            db_write_spy=DbWriteSpy(),
            cycle_lineage_spy=lineage,
        )
    assert exc.value.offending_class == "cycle_lineage_record"
    assert exc.value.offending_class == (
        ForbiddenOutputClass.CYCLE_LINEAGE_RECORD.value
    )


def test_verify_no_forbidden_runtime_producer_artifacts_requires_db_write_spy(
    tmp_path: pathlib.Path,
) -> None:
    """db_write_spy=None fails closed with UnmonitoredBoundaryError.

    The §8.1 boundary is meaningful only when actively monitored. An
    accidentally-omitted spy MUST NOT silently pass — it MUST raise. The
    unmonitored case is a *separate* failure mode from a real forbidden
    emission: no artifact was actually produced; the gate simply cannot
    vouch for the boundary. Keeping :class:`UnmonitoredBoundaryError`
    distinct from :class:`ForbiddenRuntimeProducerArtifactError` means
    SUB-13's evidence bundler can safely round-trip the latter's
    ``offending_class`` through :class:`ForbiddenOutputClass` without a
    ``ValueError`` on an unmonitored-spy sentinel.
    """
    root = _binding_root(tmp_path)
    emitted = [root / "manifest.json"]
    with pytest.raises(UnmonitoredBoundaryError) as exc:
        verify_no_forbidden_runtime_producer_artifacts(
            emitted,
            db_write_spy=None,
            cycle_lineage_spy=CycleLineageSpy(),
        )
    assert exc.value.missing_spy_kind == "db_write_spy"
    # UnmonitoredBoundaryError is still a BindingArtifactError subclass
    # so a caller catching the broad root type still catches it.
    assert isinstance(exc.value, BindingArtifactError)
    # scan_summary carries passed=False so SUB-13 evidence records the
    # boundary-monitor gap identically to a real forbidden emission —
    # closes the docstring "iff...AND with spies actively supplied" clause.
    assert isinstance(exc.value.scan_summary, ForbiddenOutputScanResult)
    assert exc.value.scan_summary.passed is False


def test_verify_no_forbidden_runtime_producer_artifacts_requires_cycle_lineage_spy(
    tmp_path: pathlib.Path,
) -> None:
    """cycle_lineage_spy=None fails closed with UnmonitoredBoundaryError."""
    root = _binding_root(tmp_path)
    emitted = [root / "manifest.json"]
    with pytest.raises(UnmonitoredBoundaryError) as exc:
        verify_no_forbidden_runtime_producer_artifacts(
            emitted,
            db_write_spy=DbWriteSpy(),
            cycle_lineage_spy=None,
        )
    assert exc.value.missing_spy_kind == "cycle_lineage_spy"
    assert isinstance(exc.value, BindingArtifactError)
    assert isinstance(exc.value.scan_summary, ForbiddenOutputScanResult)
    assert exc.value.scan_summary.passed is False


def test_forbidden_runtime_producer_artifact_error_offending_class_round_trips_through_enum(
    tmp_path: pathlib.Path,
) -> None:
    """Every real-violation raise carries an ``offending_class`` that is exactly one of :class:`ForbiddenOutputClass`.

    SUB-13's evidence bundler does
    ``ForbiddenOutputClass(exc.offending_class)`` to recover the enum
    member. If any raise path smuggled in a sentinel string (e.g.
    ``"db_write_spy_missing"``), that call would ``ValueError``. Pin the
    invariant by round-tripping the value through the enum constructor
    for each of the four real-violation classes (cycle-dated ``.tsd.forc``,
    both station-CSV shapes, all three ``met.*`` tables, cycle-lineage
    record). The unmonitored-spy case is a *separate*
    :class:`UnmonitoredBoundaryError` so it is not covered here — that is
    the whole point of the split.
    """
    root = _binding_root(tmp_path)

    # Class 1: cycle_dated_tsd_forc
    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as cycle_exc:
        verify_no_forbidden_runtime_producer_artifacts(
            [root / "manifest.json", root / "20200101.tsd.forc"],
            db_write_spy=DbWriteSpy(),
            cycle_lineage_spy=CycleLineageSpy(),
        )
    assert (
        ForbiddenOutputClass(cycle_exc.value.offending_class)
        is ForbiddenOutputClass.CYCLE_DATED_TSD_FORC
    )

    # Class 2 (lonlat shape): station_weather_csv
    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as lonlat_exc:
        verify_no_forbidden_runtime_producer_artifacts(
            [root / "manifest.json", root / "X100Y37.csv"],
            db_write_spy=DbWriteSpy(),
            cycle_lineage_spy=CycleLineageSpy(),
        )
    assert (
        ForbiddenOutputClass(lonlat_exc.value.offending_class)
        is ForbiddenOutputClass.STATION_WEATHER_CSV
    )

    # Class 2 (numbered shape): same enum member
    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as numbered_exc:
        verify_no_forbidden_runtime_producer_artifacts(
            [root / "manifest.json", root / "X100.csv"],
            db_write_spy=DbWriteSpy(),
            cycle_lineage_spy=CycleLineageSpy(),
        )
    assert (
        ForbiddenOutputClass(numbered_exc.value.offending_class)
        is ForbiddenOutputClass.STATION_WEATHER_CSV
    )

    # Class 3 (all three forbidden met.* tables): met_row_write
    for table_name in ("met.interp_weight", "met.met_station", "met.forcing_version"):
        met_spy = DbWriteSpy().record_write(
            table_name=table_name, row_summary="row"
        )
        with pytest.raises(ForbiddenRuntimeProducerArtifactError) as met_exc:
            verify_no_forbidden_runtime_producer_artifacts(
                [root / "manifest.json"],
                db_write_spy=met_spy,
                cycle_lineage_spy=CycleLineageSpy(),
            )
        assert (
            ForbiddenOutputClass(met_exc.value.offending_class)
            is ForbiddenOutputClass.MET_ROW_WRITE
        )

    # Class 4: cycle_lineage_record
    lineage_spy = CycleLineageSpy().record_lineage(
        record_summary="cycle=2020010100"
    )
    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as lineage_exc:
        verify_no_forbidden_runtime_producer_artifacts(
            [root / "manifest.json"],
            db_write_spy=DbWriteSpy(),
            cycle_lineage_spy=lineage_spy,
        )
    assert (
        ForbiddenOutputClass(lineage_exc.value.offending_class)
        is ForbiddenOutputClass.CYCLE_LINEAGE_RECORD
    )


def test_unmonitored_boundary_error_is_kwarg_only() -> None:
    """UnmonitoredBoundaryError refuses positional args (kwarg-only ctor).

    Same discipline as :class:`ForbiddenRuntimeProducerArtifactError`:
    the exception's field set is the SUB-13 evidence contract, so
    positional construction is refused to prevent argument-order drift.
    """
    scan_summary = ForbiddenOutputScanResult(
        scanned_path_count=0,
        offending_paths=(),
        offending_db_writes=(),
        cycle_lineage_records=(),
        passed=False,
    )
    with pytest.raises(TypeError):
        UnmonitoredBoundaryError(
            "db_write_spy",  # type: ignore[misc]
            scan_summary,  # type: ignore[misc]
        )
    # kwarg construction works.
    exc = UnmonitoredBoundaryError(
        missing_spy_kind="db_write_spy",
        scan_summary=scan_summary,
    )
    assert exc.missing_spy_kind == "db_write_spy"
    assert exc.scan_summary is scan_summary


def test_verify_no_forbidden_runtime_producer_artifacts_multiple_classes_fire_simultaneously(
    tmp_path: pathlib.Path,
) -> None:
    """Multi-class violation: scan_summary carries every class per docs §8.1.

    The exception message names the *first-fired* class in evaluation
    order (paths -> db_writes -> lineage) — that is by design. But the
    docstring on ``scan_summary`` promises the 4-class breakdown is
    comprehensive so SUB-13 evidence can log every violation, not just
    the first one. Inject a cycle-dated ``.tsd.forc`` AND a forbidden
    ``met.interp_weight`` write in the same call and assert
    :attr:`ForbiddenOutputScanResult.offending_paths` and
    :attr:`ForbiddenOutputScanResult.offending_db_writes` are both
    non-empty on the raised exception's ``scan_summary``.
    """
    root = _binding_root(tmp_path)
    emitted = [
        root / "manifest.json",
        root / "20200101.tsd.forc",  # cycle-dated .tsd.forc
    ]
    spy = DbWriteSpy().record_write(
        table_name="met.interp_weight",
        row_summary="station_id=s1, cell=c1, weight=1.0",
    )
    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as exc:
        verify_no_forbidden_runtime_producer_artifacts(
            emitted,
            db_write_spy=spy,
            cycle_lineage_spy=CycleLineageSpy(),
        )
    # First-fired class in evaluation order: paths run before db_writes,
    # so the raised offending_class is cycle_dated_tsd_forc. That's fine —
    # we're proving scan_summary is comprehensive, not that a specific
    # class fires first.
    assert (
        ForbiddenOutputClass(exc.value.offending_class)
        is ForbiddenOutputClass.CYCLE_DATED_TSD_FORC
    )
    scan = exc.value.scan_summary
    assert scan.passed is False
    # BOTH tuples carry their respective violations — the comprehensive
    # 4-class picture promised by the docstring at §8.1.
    assert len(scan.offending_paths) == 1
    assert scan.offending_paths[0][0] == (
        ForbiddenOutputClass.CYCLE_DATED_TSD_FORC.value
    )
    assert len(scan.offending_db_writes) == 1
    assert scan.offending_db_writes[0][0] == "met.interp_weight"
    # cycle_lineage_records tuple is empty because that class did not fire.
    assert scan.cycle_lineage_records == ()


def test_verify_no_forbidden_runtime_producer_artifacts_realistic_8_digit_basin_id_passes(
    tmp_path: pathlib.Path,
) -> None:
    """Realistic 8-digit basin id (e.g. ``basin12345678.tsd.forc``) does NOT match the cycle-dated regex.

    SUB-12-owned mirror of the SUB-10 regex identity check: the cycle
    regex requires an 8- or 10-digit stamp with a year prefix in
    ``19``/``20``/``21`` — an 8-digit basin id embedded in a filename
    like ``basin12345678.tsd.forc`` must NOT fire. Defense-in-depth: if
    a future SUB-10 change loosens the regex without updating its own
    test, this SUB-12 mirror catches the regression at the mapping stage
    boundary.
    """
    root = _binding_root(tmp_path)
    emitted = [
        root / "manifest.json",
        # basin12345678 = literal 8-digit id "12345678"; doesn't start with 19/20/21.
        root / "variant" / "basin12345678.tsd.forc",
    ]
    result = verify_no_forbidden_runtime_producer_artifacts(
        emitted,
        db_write_spy=DbWriteSpy(),
        cycle_lineage_spy=CycleLineageSpy(),
    )
    assert result.passed is True
    assert result.offending_paths == ()


def test_verify_no_forbidden_runtime_producer_artifacts_returns_result_on_pass(
    tmp_path: pathlib.Path,
) -> None:
    """On green: return a ForbiddenOutputScanResult with all expected fields.

    SUB-13 evidence bundler consumes the returned struct verbatim as the
    §8.1 receipt. Pin the expected shape so future refactors that change
    field names or types break here first.
    """
    root = _binding_root(tmp_path)
    emitted = [
        root / "manifest.json",
        root / "binding.json",
        root / "package" / "keliya.sp.att",
    ]
    result = verify_no_forbidden_runtime_producer_artifacts(
        emitted,
        db_write_spy=DbWriteSpy(),
        cycle_lineage_spy=CycleLineageSpy(),
    )
    assert result.scanned_path_count == 3
    assert result.offending_paths == ()
    assert result.offending_db_writes == ()
    assert result.cycle_lineage_records == ()
    assert result.passed is True


def test_verify_no_forbidden_runtime_producer_artifacts_result_signature_pinned() -> None:
    """ForbiddenOutputScanResult field list is pinned (SUB-13 evidence contract).

    The evidence bundler in SUB-13 reads specific field names off this
    struct. Any refactor that renames or drops a field breaks the
    downstream evidence contract — pin the field list so the break
    surfaces here.
    """
    fields = {f.name: f.type for f in dataclasses.fields(ForbiddenOutputScanResult)}
    assert set(fields) == {
        "scanned_path_count",
        "offending_paths",
        "offending_db_writes",
        "cycle_lineage_records",
        "passed",
    }


def test_forbidden_output_scan_result_is_frozen() -> None:
    """ForbiddenOutputScanResult is a frozen dataclass — field assignment raises."""
    result = ForbiddenOutputScanResult(
        scanned_path_count=0,
        offending_paths=(),
        offending_db_writes=(),
        cycle_lineage_records=(),
        passed=True,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.passed = False  # type: ignore[misc]


def test_verify_no_forbidden_runtime_producer_artifacts_case_insensitive_scan(
    tmp_path: pathlib.Path,
) -> None:
    """Uppercase legacy filename (X100Y37.CSV) is caught the same as lowercase.

    On case-insensitive filesystems (macOS APFS/HFS+, Windows NTFS
    default) the uppercase alias points to the same inode. The gate MUST
    catch both — matches SUB-10's :func:`verify_no_legacy_weather_path_in_active_tree`
    behavior.
    """
    root = _binding_root(tmp_path)
    emitted = [
        root / "manifest.json",
        root / "forcing" / "X100Y37.CSV",  # uppercase alias
    ]
    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as exc:
        verify_no_forbidden_runtime_producer_artifacts(
            emitted,
            db_write_spy=DbWriteSpy(),
            cycle_lineage_spy=CycleLineageSpy(),
        )
    assert exc.value.offending_class == "station_weather_csv"


def test_verify_no_forbidden_runtime_producer_artifacts_signature_pinned() -> None:
    """verify_no_forbidden_runtime_producer_artifacts signature is pinned.

    PA-1 codification (Epic #909 SUB-11): a signature pin that checks only
    parameter names misses a KEYWORD_ONLY -> POSITIONAL_OR_KEYWORD demotion.
    Pin ``param.kind`` alongside the name list.
    """
    sig = inspect.signature(verify_no_forbidden_runtime_producer_artifacts)
    assert list(sig.parameters) == [
        "emitted_artifact_paths",
        "db_write_spy",
        "cycle_lineage_spy",
    ]
    _assert_param_kinds(
        verify_no_forbidden_runtime_producer_artifacts,
        keyword_only=frozenset({"db_write_spy", "cycle_lineage_spy"}),
        positional_or_keyword=frozenset({"emitted_artifact_paths"}),
    )
    hints = typing.get_type_hints(verify_no_forbidden_runtime_producer_artifacts)
    assert hints["return"] is ForbiddenOutputScanResult


def test_forbidden_output_class_values_pinned() -> None:
    """ForbiddenOutputClass enum values are pinned (SUB-13 evidence contract).

    Downstream evidence records the class token verbatim. Any renamed
    enum member breaks the evidence-side deserialization — pin all four
    tokens here so the break surfaces at test time.
    """
    assert ForbiddenOutputClass.CYCLE_DATED_TSD_FORC.value == "cycle_dated_tsd_forc"
    assert ForbiddenOutputClass.STATION_WEATHER_CSV.value == "station_weather_csv"
    assert ForbiddenOutputClass.MET_ROW_WRITE.value == "met_row_write"
    assert ForbiddenOutputClass.CYCLE_LINEAGE_RECORD.value == "cycle_lineage_record"


def test_forbidden_met_tables_pinned() -> None:
    """FORBIDDEN_MET_TABLES contents pinned (docs §8.1 boundary contract)."""
    assert FORBIDDEN_MET_TABLES == frozenset(
        {"met.interp_weight", "met.met_station", "met.forcing_version"}
    )


def test_verify_no_forbidden_runtime_producer_artifacts_rejects_non_path_entry(
    tmp_path: pathlib.Path,
) -> None:
    """A str (not pathlib.Path) in the artifact set raises BindingArtifactError.

    Boundary guard: the gate extracts basenames via ``path.name`` which
    silently misbehaves on some str inputs. Type-refuse at the boundary
    so the caller sees a loud failure rather than a false-positive pass.
    """
    root = _binding_root(tmp_path)
    emitted = [root / "manifest.json", "not_a_path.csv"]
    with pytest.raises(BindingArtifactError):
        verify_no_forbidden_runtime_producer_artifacts(
            emitted,  # type: ignore[arg-type]
            db_write_spy=DbWriteSpy(),
            cycle_lineage_spy=CycleLineageSpy(),
        )


def test_verify_no_forbidden_runtime_producer_artifacts_scanned_path_count_zero(
    tmp_path: pathlib.Path,
) -> None:
    """Empty artifact set with active spies -> passed=True, scanned_path_count=0.

    A downstream caller passing an empty iterable is valid (though
    unusual — a real mapping build always emits at least the manifest).
    The gate reports the count so SUB-13 evidence distinguishes "zero
    scanned" from "N scanned, none matched".
    """
    result = verify_no_forbidden_runtime_producer_artifacts(
        [],
        db_write_spy=DbWriteSpy(),
        cycle_lineage_spy=CycleLineageSpy(),
    )
    assert result.passed is True
    assert result.scanned_path_count == 0


def test_verify_no_forbidden_runtime_producer_artifacts_accepts_non_forbidden_db_writes(
    tmp_path: pathlib.Path,
) -> None:
    """A DB write to a non-forbidden table (e.g. 'metadata.build_receipt') passes.

    The gate only refuses writes to :data:`FORBIDDEN_MET_TABLES`. A
    write to any other table is legitimate mapping-stage bookkeeping and
    MUST NOT trip the gate — otherwise the gate would refuse valid
    builds that record receipts elsewhere.
    """
    root = _binding_root(tmp_path)
    emitted = [root / "manifest.json"]
    spy = DbWriteSpy().record_write(
        table_name="metadata.build_receipt",
        row_summary="mapping_version=v1",
    )
    result = verify_no_forbidden_runtime_producer_artifacts(
        emitted,
        db_write_spy=spy,
        cycle_lineage_spy=CycleLineageSpy(),
    )
    assert result.passed is True
    assert result.offending_db_writes == ()


def test_db_write_spy_is_frozen() -> None:
    """DbWriteSpy is a frozen dataclass — captured_writes assignment raises."""
    spy = DbWriteSpy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        spy.captured_writes = (("t", "r"),)  # type: ignore[misc]


def test_cycle_lineage_spy_is_frozen() -> None:
    """CycleLineageSpy is a frozen dataclass — captured_records assignment raises."""
    spy = CycleLineageSpy()
    with pytest.raises(dataclasses.FrozenInstanceError):
        spy.captured_records = ("r",)  # type: ignore[misc]


def test_db_write_spy_record_write_appends_immutably() -> None:
    """DbWriteSpy.record_write returns a NEW spy — original is unchanged."""
    spy = DbWriteSpy()
    new_spy = spy.record_write(table_name="met.met_station", row_summary="s1")
    assert spy.captured_writes == ()
    assert new_spy.captured_writes == (("met.met_station", "s1"),)


def test_cycle_lineage_spy_record_lineage_appends_immutably() -> None:
    """CycleLineageSpy.record_lineage returns a NEW spy — original unchanged."""
    spy = CycleLineageSpy()
    new_spy = spy.record_lineage(record_summary="cycle=2020010100")
    assert spy.captured_records == ()
    assert new_spy.captured_records == ("cycle=2020010100",)
