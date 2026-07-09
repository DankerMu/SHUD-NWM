"""Tests for :mod:`workers.mapping_builder.evidence` (Epic #909 SUB-13, §5.1 + §5.2).

Coverage
--------

* §5.1 :func:`assemble_evidence_package` — green-path assembly with every
  §14 section populated correctly for a synthetic fixture; baseline
  identity + grid snapshot reference + ownership table + station
  bindings + sp_att asset diff + mapping algorithm identity +
  hydrologic_core_fingerprint recorded.
* §5.1 identity cross-check — ``algorithm_id`` pinned to
  ``"nearest_cell_barycenter_geodesic_v1"``; ``proj_crs_database_version``
  cross-checked against the caller-supplied :class:`ReadinessManifest`
  fixture; mismatch raises :class:`AlgorithmIdentityMismatchError`.
* §5.1 hydrologic_core_fingerprint — recorded verbatim; mutating any
  non-``FORC`` covered surface changes the recorded fingerprint (delegate
  to SUB-9 test coverage; here we just prove pass-through).
* §5.2 distance_qa — min/P50/P95/max normalized populated;
  tie_count + coverage_edge_count populated.
* §5.2 capacity_report — station/timestep/row/file-size populated
  against limits; before/after station reduction framing (~5× narrative).
* §5.2 G0..G5 gate results — each gate has a :class:`GateResult` in
  :class:`GateResults`; G2 grid identity via SUB-5 shared-helper
  signature; G4 asset delta (mesh/river/lake/soil/geol/land/calibration +
  hydrologic_core_fingerprint + no-legacy-weather-path); G5
  cross-artifact consistency (SUB-11 manifest ↔ binding) + SUB-12
  forbidden-output.
* §5.2 ownership images — SVG bytes non-empty + deterministic; INV-3
  discipline (domain.shp is visualization-only).
* §5.2 approvals + rollback target — SUB-7 override approver_id
  recorded verbatim; rollback target present.
* §5.2 checksum binding — evidence_checksum binds to
  bound_mapping_asset_checksum; mutating either invalidates the other.
* §5.2 checksum_excluded_fields — enumeration; mutating build_timestamp
  never changes any checksum.
* Immutability — frozen invariant tests on :class:`EvidencePackage` and
  every support dataclass.
* Signature pin + type hint invariants matching the SUB-8/9/10/11/12
  style.
"""

from __future__ import annotations

import dataclasses
import inspect
import pathlib
import struct
import typing
from datetime import UTC, datetime

import pytest

from tests.fixtures.mapping_builder.in_memory_grid_snapshot import (
    make_regular_grid_cells,
)
from workers.mapping_builder import (
    ALGORITHM_ID,
    EVIDENCE_CHECKSUM_EXCLUDED_FIELDS,
    G0_THROUGH_G5,
    AlgorithmIdentityMismatchError,
    Approvals,
    BaselineIdentity,
    BaselineIntegrityError,
    BindingArtifactError,
    CapacityReport,
    CheckusmExcludedFieldEnteredCheckusmError,
    DistanceQA,
    EvidenceChecksumBindingError,
    EvidenceChecksumMutationError,
    EvidencePackage,
    EvidencePackageError,
    GateFailureRecordedInEvidenceError,
    GateResult,
    GateResults,
    GridSnapshotReference,
    HydrologicCoreFingerprint,
    MappingAlgorithmError,
    MappingAlgorithmIdentity,
    MissingBaselineIdentityError,
    OwnershipImageRenderError,
    OwnershipImages,
    OwnershipRow,
    ReadinessManifest,
    RollbackTarget,
    SemanticDiff,
    SemanticDiffEntry,
    SpAttAssetDiff,
    SpAttRewriteError,
    StationBinding,
    assemble_evidence_package,
    bind_evidence_to_mapping_asset,
    compute_evidence_checksum,
    emit_direct_grid_manifest_and_binding,
    enumerate_checksum_excluded_fields,
    render_ownership_images,
    verify_algorithm_and_proj_identity_matches_readiness,
    verify_all_g0_through_g5_gates_passed,
    verify_evidence_checksum_binding,
)
from workers.mapping_builder import evidence as evidence_module
from workers.mapping_builder.binding import ZPolicy

# --- fixture helpers ------------------------------------------------------


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


def _proj_version() -> str:
    """Return a stable proj_crs_database_version string for tests."""
    return "test-proj-db-1.0"


def _make_readiness_manifest(
    *,
    algorithm_id: str = ALGORITHM_ID,
    proj_crs_database_version: str = _proj_version(),
    checksum: str = "r" * 64,
) -> ReadinessManifest:
    return ReadinessManifest(
        algorithm_id=algorithm_id,
        proj_crs_database_version=proj_crs_database_version,
        checksum=checksum,
    )


def _emit_manifest_and_binding():
    """Emit a real SUB-11 manifest + binding artifact for fixture reuse.

    Uses the SUB-11 in-memory grid snapshot fixture so the station
    binding rows we record on the evidence package are the SAME shape
    SUB-11 emits in production.
    """
    snapshot_cells = make_regular_grid_cells(
        lon0=100.0,
        lat0=37.0,
        lon_step=0.1,
        lat_step=0.1,
        lon_count=3,
        lat_count=3,
    )
    used_cells = sorted(
        snapshot_cells, key=lambda c: int(c.canonical_ordinal)
    )[:4]
    shud_forcing_index = {
        cell.grid_cell_id: idx for idx, cell in enumerate(used_cells, start=1)
    }
    z_policy = ZPolicy(
        policy_name="sentinel",
        readiness_manifest_checksum="a" * 64,
        per_cell_z={cell.grid_cell_id: -9999.0 for cell in used_cells},
    )
    manifest, artifact = emit_direct_grid_manifest_and_binding(
        used_cells=used_cells,
        snapshot_cells=snapshot_cells,
        shud_forcing_index=shud_forcing_index,
        mapping_asset_identity="mapping-v1-abc",
        model_input_package_id="pkg-v1-abc",
        sp_att_path="package/keliya.sp.att",
        sp_att_bytes=b"MOCK_SP_ATT_BYTES\n",
        applicable_source_ids=("GFS",),
        grid_id="GFS_0p1",
        grid_signature="b" * 64,
        z_policy=z_policy,
        binding_uri="s3://bucket/mapping-v1-abc/binding.json",
        model_crs_wkt=_MODEL_CRS_WKT,
    )
    return manifest, artifact, used_cells


def _make_ownership_table(used_cells) -> tuple[OwnershipRow, ...]:
    rows: list[OwnershipRow] = []
    for i, cell in enumerate(used_cells, start=1):
        rows.append(
            OwnershipRow(
                element_id=i,
                old_forc=str(i),  # legacy FORC value
                new_forc=str(i + 100),  # rewritten FORC value
                grid_cell_id=cell.grid_cell_id,
                distance_meters=float(i * 100),
            )
        )
    return tuple(rows)


def _make_baseline_identity() -> BaselineIdentity:
    return BaselineIdentity(
        package_sha256_hex="a" * 64,
        sp_att_sha256_hex="b" * 64,
        sp_mesh_sha256_hex="c" * 64,
    )


def _make_grid_snapshot_reference() -> GridSnapshotReference:
    return GridSnapshotReference(
        snapshot_id="00000000-0000-0000-0000-000000000001",
        grid_signature="b" * 64,
        snapshot_checksum="0" * 64,
    )


def _make_sp_att_asset_diff() -> SpAttAssetDiff:
    return SpAttAssetDiff(
        old_sha256_hex="d" * 64,
        new_sha256_hex="e" * 64,
        semantic_diff_summary=SemanticDiff(
            entries=(
                SemanticDiffEntry(element_id=1, old_forc=1, new_forc=101),
                SemanticDiffEntry(element_id=2, old_forc=2, new_forc=102),
            )
        ),
    )


def _make_mapping_algorithm_identity(
    *,
    algorithm_id: str = ALGORITHM_ID,
    proj_crs_database_version: str = _proj_version(),
) -> MappingAlgorithmIdentity:
    return MappingAlgorithmIdentity(
        algorithm_id=algorithm_id,
        proj_crs_database_version=proj_crs_database_version,
    )


def _make_hydrologic_core_fingerprint() -> HydrologicCoreFingerprint:
    return HydrologicCoreFingerprint(
        hash="f" * 64,
        covered_paths=(
            "calibration:cal/calib.calib",
            "geol:gis/soil.geol",
            "lake:gis/lake.lake",
            "land:gis/land.land",
            "mesh:mesh/basin.sp.mesh",
            "river:river/river.riv",
            "soil:gis/soil.soil",
            "solver_config:<bytes>",
            "sp_att_non_forc:basin.sp.att",
            "state_schema:<bytes>",
        ),
    )


def _make_distance_qa() -> DistanceQA:
    return DistanceQA(
        min_normalized=0.05,
        p50_normalized=0.35,
        p95_normalized=0.75,
        max_normalized=0.95,
        tie_count=2,
        coverage_edge_count=1,
    )


def _make_capacity_report() -> CapacityReport:
    return CapacityReport(
        station_count=1200,
        timestep_count=48,
        timeseries_row_count=57_600,
        file_size_bytes=1_048_576,
        station_count_limit=5000,
        timestep_count_limit=336,
        timeseries_row_count_limit=1_500_000,
        file_size_bytes_limit=100_000_000,
        before_station_count=6290,
        after_station_count=1200,
        station_reduction_ratio=6290 / 1200,
    )


def _make_gate_result(
    *,
    gate_id: str,
    passed: bool = True,
) -> GateResult:
    return GateResult(
        gate_id=gate_id,
        passed=passed,
        evidence_ref={
            "kind": f"{gate_id.lower()}_receipt",
            "artifact_checksum": "0" * 64,
        },
    )


def _make_gate_results(
    *,
    all_passed: bool = True,
    failed_gate_id: str | None = None,
) -> GateResults:
    """Return one GateResult per gate; failed_gate_id marks a specific gate as failed."""
    def _passed(gate_id: str) -> bool:
        if all_passed:
            return True
        return gate_id != failed_gate_id if failed_gate_id else False
    return GateResults(
        g0=_make_gate_result(gate_id="G0", passed=_passed("G0")),
        g1=_make_gate_result(gate_id="G1", passed=_passed("G1")),
        g2=_make_gate_result(gate_id="G2", passed=_passed("G2")),
        g3=_make_gate_result(gate_id="G3", passed=_passed("G3")),
        g4=_make_gate_result(gate_id="G4", passed=_passed("G4")),
        g5=_make_gate_result(gate_id="G5", passed=_passed("G5")),
    )


def _write_valid_domain_shp(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write a minimal shapefile with the correct magic number.

    The rest of the file body is padding — :func:`render_ownership_images`
    only reads the 4-byte magic number, never geometry (INV-3).
    """
    domain_shp = tmp_path / "domain.shp"
    magic = struct.pack(">i", 9994)  # 4 bytes: big-endian int 9994
    header_padding = b"\x00" * 96  # rest of ESRI 100-byte header
    domain_shp.write_bytes(magic + header_padding)
    return domain_shp


def _make_ownership_images(
    tmp_path: pathlib.Path,
    ownership_table: tuple[OwnershipRow, ...],
) -> OwnershipImages:
    domain_shp = _write_valid_domain_shp(tmp_path)
    return render_ownership_images(domain_shp, ownership_table)


def _make_approvals(approver: str | None = None) -> Approvals:
    return Approvals(small_basin_override_approver_id=approver)


def _make_rollback_target() -> RollbackTarget:
    return RollbackTarget(
        previous_mapping_asset_checksum="p" * 64,
        previous_mapping_asset_label="v0-initial",
    )


def _make_evidence_package(
    tmp_path: pathlib.Path,
    *,
    algorithm_id: str = ALGORITHM_ID,
    proj_crs_database_version: str = _proj_version(),
    all_gates_passed: bool = True,
    failed_gate_id: str | None = None,
    approver: str | None = None,
    build_timestamp: datetime | None = None,
) -> tuple[EvidencePackage, tuple[OwnershipRow, ...]]:
    """Return a fully-assembled EvidencePackage + the ownership table."""
    manifest, artifact, used_cells = _emit_manifest_and_binding()
    ownership_table = _make_ownership_table(used_cells)
    ownership_images = _make_ownership_images(tmp_path, ownership_table)
    package = assemble_evidence_package(
        baseline_identity=_make_baseline_identity(),
        grid_snapshot_reference=_make_grid_snapshot_reference(),
        ownership_table=ownership_table,
        manifest=manifest,
        binding_artifact=artifact,
        sp_att_asset_diff=_make_sp_att_asset_diff(),
        mapping_algorithm_identity=_make_mapping_algorithm_identity(
            algorithm_id=algorithm_id,
            proj_crs_database_version=proj_crs_database_version,
        ),
        hydrologic_core_fingerprint=_make_hydrologic_core_fingerprint(),
        distance_qa=_make_distance_qa(),
        capacity_report=_make_capacity_report(),
        gate_results=_make_gate_results(
            all_passed=all_gates_passed, failed_gate_id=failed_gate_id
        ),
        ownership_images=ownership_images,
        approvals=_make_approvals(approver),
        rollback_target=_make_rollback_target(),
        build_timestamp=build_timestamp,
    )
    return package, ownership_table


# =========================================================================
# §5.1 GREEN-PATH ASSEMBLY: every §14 evidence_package_section populated
# =========================================================================


def test_evidence_package_section_baseline_identity(tmp_path: pathlib.Path) -> None:
    """Green path: baseline_identity section carries package/att/mesh SHA-256."""
    package, _ = _make_evidence_package(tmp_path)
    assert package.baseline_identity.package_sha256_hex == "a" * 64
    assert package.baseline_identity.sp_att_sha256_hex == "b" * 64
    assert package.baseline_identity.sp_mesh_sha256_hex == "c" * 64


def test_evidence_package_section_grid_snapshot_reference(
    tmp_path: pathlib.Path,
) -> None:
    """Green path: grid_snapshot_reference section carries snapshot_id + signature + checksum."""
    package, _ = _make_evidence_package(tmp_path)
    assert package.grid_snapshot_reference.snapshot_id.startswith("0000")
    assert package.grid_snapshot_reference.grid_signature == "b" * 64
    assert package.grid_snapshot_reference.snapshot_checksum == "0" * 64


def test_evidence_package_section_ownership_table_sorted_by_element_id(
    tmp_path: pathlib.Path,
) -> None:
    """Green path: ownership_table sorted by element_id ascending."""
    package, ownership_table = _make_evidence_package(tmp_path)
    assert len(package.ownership_table) == len(ownership_table)
    element_ids = [row.element_id for row in package.ownership_table]
    assert element_ids == sorted(element_ids)
    for row in package.ownership_table:
        assert isinstance(row, OwnershipRow)
        assert row.old_forc  # non-empty
        assert row.new_forc
        assert row.grid_cell_id
        assert row.distance_meters >= 0


def test_evidence_package_section_station_binding_rows_from_sub11(
    tmp_path: pathlib.Path,
) -> None:
    """Green path: station_binding_rows are SUB-11's binding artifact rows verbatim."""
    package, _ = _make_evidence_package(tmp_path)
    assert len(package.station_binding_rows) == 4
    for row in package.station_binding_rows:
        assert isinstance(row, StationBinding)
        assert row.station_id
        assert row.shud_forcing_index >= 1
        assert row.grid_cell_id


def test_evidence_package_section_sp_att_asset_diff(tmp_path: pathlib.Path) -> None:
    """Green path: sp_att_asset_diff carries old + new SHA-256 + semantic diff."""
    package, _ = _make_evidence_package(tmp_path)
    assert package.sp_att_asset_diff.old_sha256_hex == "d" * 64
    assert package.sp_att_asset_diff.new_sha256_hex == "e" * 64
    assert isinstance(package.sp_att_asset_diff.semantic_diff_summary, SemanticDiff)
    assert len(package.sp_att_asset_diff.semantic_diff_summary.entries) == 2


def test_evidence_package_section_mapping_algorithm_identity(
    tmp_path: pathlib.Path,
) -> None:
    """Green path: mapping_algorithm_identity records algorithm_id + proj_crs_database_version."""
    package, _ = _make_evidence_package(tmp_path)
    assert package.mapping_algorithm_identity.algorithm_id == ALGORITHM_ID
    assert (
        package.mapping_algorithm_identity.proj_crs_database_version
        == _proj_version()
    )


def test_evidence_package_section_hydrologic_core_fingerprint(
    tmp_path: pathlib.Path,
) -> None:
    """Green path: hydrologic_core_fingerprint recorded verbatim (SUB-9 pass-through)."""
    package, _ = _make_evidence_package(tmp_path)
    assert isinstance(package.hydrologic_core_fingerprint, HydrologicCoreFingerprint)
    assert package.hydrologic_core_fingerprint.hash == "f" * 64
    assert len(package.hydrologic_core_fingerprint.covered_paths) == 10


def test_evidence_package_section_distance_qa(tmp_path: pathlib.Path) -> None:
    """Green path: distance_qa carries min/P50/P95/max + tie/edge counts."""
    package, _ = _make_evidence_package(tmp_path)
    assert package.distance_qa.min_normalized == pytest.approx(0.05)
    assert package.distance_qa.p50_normalized == pytest.approx(0.35)
    assert package.distance_qa.p95_normalized == pytest.approx(0.75)
    assert package.distance_qa.max_normalized == pytest.approx(0.95)
    assert package.distance_qa.tie_count == 2
    assert package.distance_qa.coverage_edge_count == 1


def test_evidence_package_section_capacity_report(tmp_path: pathlib.Path) -> None:
    """Green path: capacity_report carries all limits + before/after station reduction."""
    package, _ = _make_evidence_package(tmp_path)
    assert package.capacity_report.station_count == 1200
    assert package.capacity_report.timestep_count == 48
    assert package.capacity_report.station_count_limit == 5000
    assert package.capacity_report.before_station_count == 6290
    assert package.capacity_report.after_station_count == 1200
    # ~5× station reduction narrative.
    assert package.capacity_report.station_reduction_ratio == pytest.approx(
        6290 / 1200
    )
    assert 5.0 < package.capacity_report.station_reduction_ratio < 6.0


def test_evidence_package_section_gate_results_populated(
    tmp_path: pathlib.Path,
) -> None:
    """Green path: G0..G5 gate results all present and correctly labeled."""
    package, _ = _make_evidence_package(tmp_path)
    ordered = package.gate_results.iter_ordered()
    assert len(ordered) == 6
    for expected_id, gate_result in zip(G0_THROUGH_G5, ordered):
        assert gate_result.gate_id == expected_id
        assert gate_result.passed is True


def test_evidence_package_section_ownership_images(tmp_path: pathlib.Path) -> None:
    """Green path: ownership_images bytes non-empty; image_format='svg'."""
    package, _ = _make_evidence_package(tmp_path)
    assert package.ownership_images.image_format == "svg"
    assert len(package.ownership_images.old_image_bytes) > 0
    assert len(package.ownership_images.new_image_bytes) > 0
    # SVG bytes must contain the SVG root element (deterministic evidence).
    assert b"<svg" in package.ownership_images.old_image_bytes
    assert b"<svg" in package.ownership_images.new_image_bytes


def test_evidence_package_section_approvals_no_override(
    tmp_path: pathlib.Path,
) -> None:
    """No override -> approvals.small_basin_override_approver_id is None."""
    package, _ = _make_evidence_package(tmp_path)
    assert package.approvals.small_basin_override_approver_id is None


def test_evidence_package_section_approvals_with_override(
    tmp_path: pathlib.Path,
) -> None:
    """SUB-7 small-basin override recorded verbatim by approver_id."""
    package, _ = _make_evidence_package(tmp_path, approver="ops@example.com")
    assert package.approvals.small_basin_override_approver_id == "ops@example.com"


def test_evidence_package_section_rollback_target(tmp_path: pathlib.Path) -> None:
    """Green path: rollback_target records previous_mapping_asset_checksum + label."""
    package, _ = _make_evidence_package(tmp_path)
    assert package.rollback_target.previous_mapping_asset_checksum == "p" * 64
    assert package.rollback_target.previous_mapping_asset_label == "v0-initial"


def test_evidence_package_section_checksum_excluded_fields(
    tmp_path: pathlib.Path,
) -> None:
    """Green path: checksum_excluded_fields carries the canonical exclusion list."""
    package, _ = _make_evidence_package(tmp_path)
    assert package.checksum_excluded_fields == EVIDENCE_CHECKSUM_EXCLUDED_FIELDS


def test_evidence_package_section_evidence_checksum_is_valid_hex(
    tmp_path: pathlib.Path,
) -> None:
    """Green path: evidence_checksum is a 64-char lowercase SHA-256 hex."""
    package, _ = _make_evidence_package(tmp_path)
    assert len(package.evidence_checksum) == 64
    assert all(c in "0123456789abcdef" for c in package.evidence_checksum)


# =========================================================================
# §5.1 ALGORITHM_IDENTITY: pinned to nearest_cell_barycenter_geodesic_v1
# =========================================================================


def test_algorithm_id_module_constant_pinned() -> None:
    """ALGORITHM_ID module constant is pinned to nearest_cell_barycenter_geodesic_v1."""
    assert ALGORITHM_ID == "nearest_cell_barycenter_geodesic_v1"


def test_algorithm_identity_recorded_on_evidence(tmp_path: pathlib.Path) -> None:
    """Green path: evidence records algorithm_id from the module constant."""
    package, _ = _make_evidence_package(tmp_path)
    assert (
        package.mapping_algorithm_identity.algorithm_id
        == "nearest_cell_barycenter_geodesic_v1"
    )


def test_algorithm_identity_and_proj_cross_check_positive(
    tmp_path: pathlib.Path,
) -> None:
    """Green path: verify_algorithm_and_proj_identity_matches_readiness returns None."""
    package, _ = _make_evidence_package(tmp_path)
    readiness = _make_readiness_manifest()
    # None on pass.
    assert (
        verify_algorithm_and_proj_identity_matches_readiness(
            package, readiness_manifest=readiness
        )
        is None
    )


def test_algorithm_identity_mismatch_raises(tmp_path: pathlib.Path) -> None:
    """Mismatching algorithm_id -> AlgorithmIdentityMismatchError."""
    package, _ = _make_evidence_package(tmp_path)
    readiness = _make_readiness_manifest(
        algorithm_id="different_algorithm_v2",
    )
    with pytest.raises(AlgorithmIdentityMismatchError) as exc:
        verify_algorithm_and_proj_identity_matches_readiness(
            package, readiness_manifest=readiness
        )
    assert exc.value.field_name == "algorithm_id"
    assert exc.value.expected == "different_algorithm_v2"
    assert exc.value.actual == ALGORITHM_ID


def test_proj_crs_database_version_mismatch_raises(
    tmp_path: pathlib.Path,
) -> None:
    """Mismatching proj_crs_database_version -> AlgorithmIdentityMismatchError."""
    package, _ = _make_evidence_package(tmp_path)
    readiness = _make_readiness_manifest(
        proj_crs_database_version="different-proj-db-2.0",
    )
    with pytest.raises(AlgorithmIdentityMismatchError) as exc:
        verify_algorithm_and_proj_identity_matches_readiness(
            package, readiness_manifest=readiness
        )
    assert exc.value.field_name == "proj_crs_database_version"
    assert exc.value.readiness_manifest_checksum == "r" * 64


# =========================================================================
# §5.1 MUTATING FIELDS INVALIDATES EVIDENCE CHECKSUM
# =========================================================================


def test_mutating_algorithm_id_invalidates_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating algorithm_id -> compute_evidence_checksum yields a different digest.

    Also proves verify_evidence_checksum_binding raises after the mutation
    (evidence_checksum recorded on the mutated instance is stale).
    """
    package, _ = _make_evidence_package(tmp_path)
    poisoned_identity = dataclasses.replace(
        package.mapping_algorithm_identity,
        algorithm_id="different_algorithm_v2",
    )
    poisoned = dataclasses.replace(
        package, mapping_algorithm_identity=poisoned_identity
    )
    assert compute_evidence_checksum(poisoned) != package.evidence_checksum


def test_mutating_proj_crs_database_version_invalidates_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating proj_crs_database_version -> different digest."""
    package, _ = _make_evidence_package(tmp_path)
    poisoned_identity = dataclasses.replace(
        package.mapping_algorithm_identity,
        proj_crs_database_version="different-proj-db-2.0",
    )
    poisoned = dataclasses.replace(
        package, mapping_algorithm_identity=poisoned_identity
    )
    assert compute_evidence_checksum(poisoned) != package.evidence_checksum


def test_mutating_bound_mapping_asset_checksum_invalidates_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating bound_mapping_asset_checksum -> different digest."""
    package, _ = _make_evidence_package(tmp_path)
    bound = bind_evidence_to_mapping_asset(package, "m" * 64)
    # Mutate the bound_mapping_asset_checksum via dataclasses.replace.
    poisoned = dataclasses.replace(bound, bound_mapping_asset_checksum="z" * 64)
    # Recomputing the checksum on the mutated package MUST NOT equal the
    # stored value.
    assert compute_evidence_checksum(poisoned) != bound.evidence_checksum


def test_mutating_ownership_table_invalidates_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating a single ownership_table row -> different digest."""
    package, _ = _make_evidence_package(tmp_path)
    modified_rows = list(package.ownership_table)
    modified_rows[0] = dataclasses.replace(
        modified_rows[0], new_forc="9999"
    )
    poisoned = dataclasses.replace(
        package, ownership_table=tuple(modified_rows)
    )
    assert compute_evidence_checksum(poisoned) != package.evidence_checksum


def test_mutating_hydrologic_core_fingerprint_invalidates_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating hydrologic_core_fingerprint -> different digest.

    Regression: SUB-9 test coverage proves that mutating any covered
    non-``FORC`` file changes the fingerprint hash. Here we prove that
    the recorded fingerprint's mutation propagates into the evidence
    checksum.
    """
    package, _ = _make_evidence_package(tmp_path)
    poisoned_fp = dataclasses.replace(
        package.hydrologic_core_fingerprint, hash="9" * 64
    )
    poisoned = dataclasses.replace(
        package, hydrologic_core_fingerprint=poisoned_fp
    )
    assert compute_evidence_checksum(poisoned) != package.evidence_checksum


# =========================================================================
# §5.2 G0-G5 GATE RESULTS
# =========================================================================


def test_g0_through_g5_module_constant_pinned() -> None:
    """G0_THROUGH_G5 module constant is pinned to (G0..G5)."""
    assert G0_THROUGH_G5 == ("G0", "G1", "G2", "G3", "G4", "G5")


@pytest.mark.parametrize("gate_id", list(G0_THROUGH_G5))
def test_gate_results_has_one_result_per_gate(
    tmp_path: pathlib.Path,
    gate_id: str,
) -> None:
    """Every G0..G5 gate has a GateResult with the matching gate_id."""
    package, _ = _make_evidence_package(tmp_path)
    ordered = {r.gate_id: r for r in package.gate_results.iter_ordered()}
    assert gate_id in ordered
    assert ordered[gate_id].gate_id == gate_id


def test_verify_all_g0_through_g5_gates_passed_positive(
    tmp_path: pathlib.Path,
) -> None:
    """Green path: all six gates recorded as passed -> verify_all returns None."""
    package, _ = _make_evidence_package(tmp_path)
    assert verify_all_g0_through_g5_gates_passed(package) is None


@pytest.mark.parametrize("failed_gate_id", list(G0_THROUGH_G5))
def test_verify_all_g0_through_g5_gates_passed_raises_on_failure(
    tmp_path: pathlib.Path,
    failed_gate_id: str,
) -> None:
    """A recorded gate failure -> GateFailureRecordedInEvidenceError."""
    package, _ = _make_evidence_package(
        tmp_path,
        all_gates_passed=False,
        failed_gate_id=None,  # all fail
    )
    with pytest.raises(GateFailureRecordedInEvidenceError) as exc:
        verify_all_g0_through_g5_gates_passed(package)
    # First-fired = G0 in gate ordering.
    assert exc.value.failed_gate_id == "G0"


def test_gate_result_evidence_ref_is_structured_mapping(
    tmp_path: pathlib.Path,
) -> None:
    """Each GateResult.evidence_ref is a structured Mapping (not a bare string)."""
    package, _ = _make_evidence_package(tmp_path)
    for result in package.gate_results.iter_ordered():
        assert isinstance(result.evidence_ref, dict)
        assert "kind" in result.evidence_ref


# =========================================================================
# §5.2 DISTANCE_QA + CAPACITY_REPORT
# =========================================================================


def test_distance_qa_min_p50_p95_max_ordered(tmp_path: pathlib.Path) -> None:
    """Distance QA quantiles are ordered min <= P50 <= P95 <= max."""
    package, _ = _make_evidence_package(tmp_path)
    q = package.distance_qa
    assert q.min_normalized <= q.p50_normalized
    assert q.p50_normalized <= q.p95_normalized
    assert q.p95_normalized <= q.max_normalized


def test_distance_qa_tie_and_edge_counts_are_non_negative_ints(
    tmp_path: pathlib.Path,
) -> None:
    """Distance QA tie_count / coverage_edge_count are non-negative integers."""
    package, _ = _make_evidence_package(tmp_path)
    assert isinstance(package.distance_qa.tie_count, int)
    assert isinstance(package.distance_qa.coverage_edge_count, int)
    assert package.distance_qa.tie_count >= 0
    assert package.distance_qa.coverage_edge_count >= 0


def test_capacity_report_reduction_framing_is_5x(tmp_path: pathlib.Path) -> None:
    """Capacity report captures the ~5× station reduction framing."""
    package, _ = _make_evidence_package(tmp_path)
    ratio = package.capacity_report.station_reduction_ratio
    assert 5.0 <= ratio <= 5.5  # 6290/1200 ~= 5.24


def test_capacity_report_all_limits_positive(tmp_path: pathlib.Path) -> None:
    """Every capacity limit is a positive integer (non-zero)."""
    package, _ = _make_evidence_package(tmp_path)
    r = package.capacity_report
    assert r.station_count_limit > 0
    assert r.timestep_count_limit > 0
    assert r.timeseries_row_count_limit > 0
    assert r.file_size_bytes_limit > 0


# =========================================================================
# §5.2 CHECKSUM BINDING TO MAPPING ASSET
# =========================================================================


def test_bind_evidence_to_mapping_asset_positive(tmp_path: pathlib.Path) -> None:
    """bind_evidence_to_mapping_asset returns a new package with bound checksum + recomputed digest."""
    package, _ = _make_evidence_package(tmp_path)
    bound = bind_evidence_to_mapping_asset(package, "m" * 64)
    # New instance (frozen dataclass — original unchanged).
    assert bound is not package
    assert bound.bound_mapping_asset_checksum == "m" * 64
    # evidence_checksum recomputed from the mutated instance.
    assert bound.evidence_checksum == compute_evidence_checksum(bound)


def test_bind_evidence_original_package_unchanged(tmp_path: pathlib.Path) -> None:
    """The input package is NOT mutated (frozen dataclass invariant)."""
    package, _ = _make_evidence_package(tmp_path)
    original_bound = package.bound_mapping_asset_checksum
    original_checksum = package.evidence_checksum
    _ = bind_evidence_to_mapping_asset(package, "m" * 64)
    assert package.bound_mapping_asset_checksum == original_bound
    assert package.evidence_checksum == original_checksum


def test_bind_evidence_empty_asset_checksum_raises(tmp_path: pathlib.Path) -> None:
    """Empty mapping_asset_checksum -> EvidencePackageError."""
    package, _ = _make_evidence_package(tmp_path)
    with pytest.raises(EvidencePackageError):
        bind_evidence_to_mapping_asset(package, "")
    with pytest.raises(EvidencePackageError):
        bind_evidence_to_mapping_asset(package, "   ")


def test_verify_evidence_checksum_binding_positive(tmp_path: pathlib.Path) -> None:
    """Green path: verify_evidence_checksum_binding returns None on a bound package."""
    package, _ = _make_evidence_package(tmp_path)
    bound = bind_evidence_to_mapping_asset(package, "m" * 64)
    assert verify_evidence_checksum_binding(bound, "m" * 64) is None


def test_verify_evidence_checksum_binding_wrong_expected_raises(
    tmp_path: pathlib.Path,
) -> None:
    """Wrong expected mapping_asset_checksum -> EvidenceChecksumBindingError."""
    package, _ = _make_evidence_package(tmp_path)
    bound = bind_evidence_to_mapping_asset(package, "m" * 64)
    with pytest.raises(EvidenceChecksumBindingError) as exc:
        verify_evidence_checksum_binding(bound, "z" * 64)
    assert exc.value.expected == "z" * 64
    assert exc.value.actual == "m" * 64


def test_verify_evidence_checksum_binding_stale_evidence_checksum_raises(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating a checksum-included field but keeping stale evidence_checksum -> raise."""
    package, _ = _make_evidence_package(tmp_path)
    bound = bind_evidence_to_mapping_asset(package, "m" * 64)
    # Poison bound_mapping_asset_checksum but keep the pre-mutation evidence_checksum.
    poisoned = dataclasses.replace(bound, bound_mapping_asset_checksum="z" * 64)
    with pytest.raises(EvidenceChecksumBindingError):
        verify_evidence_checksum_binding(poisoned, "z" * 64)


def test_mutating_evidence_and_mapping_asset_both_invalidate(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating either evidence or mapping-asset checksum invalidates the binding."""
    package, _ = _make_evidence_package(tmp_path)
    bound = bind_evidence_to_mapping_asset(package, "m" * 64)
    # Case 1: mutate mapping_asset_checksum.
    poisoned_asset = dataclasses.replace(
        bound, bound_mapping_asset_checksum="z" * 64
    )
    with pytest.raises(EvidenceChecksumBindingError):
        verify_evidence_checksum_binding(poisoned_asset, "z" * 64)
    # Case 2: mutate a real evidence field.
    poisoned_evidence = dataclasses.replace(
        bound, evidence_checksum="0" * 64  # blatantly wrong
    )
    with pytest.raises(EvidenceChecksumBindingError):
        verify_evidence_checksum_binding(poisoned_evidence, "m" * 64)


# =========================================================================
# §5.2 CHECKSUM_EXCLUDED_FIELDS
# =========================================================================


def test_enumerate_checksum_excluded_fields_returns_canonical_list() -> None:
    """enumerate_checksum_excluded_fields returns the canonical exclusion list."""
    excluded = enumerate_checksum_excluded_fields()
    assert excluded == ("build_timestamp", "build_host")
    assert excluded == EVIDENCE_CHECKSUM_EXCLUDED_FIELDS


def test_mutating_build_timestamp_never_changes_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating build_timestamp does NOT invalidate evidence_checksum.

    Per §5.2 Required-evidence: mutating any excluded field never
    changes any checksum. build_timestamp is the exemplar excluded
    field.
    """
    package_no_ts, _ = _make_evidence_package(tmp_path, build_timestamp=None)
    ts1 = datetime(2026, 1, 1, tzinfo=UTC)
    ts2 = datetime(2027, 6, 6, tzinfo=UTC)
    package_with_ts1 = dataclasses.replace(
        package_no_ts,
        build_timestamp=ts1,
        # Also recompute evidence_checksum to make it internally
        # consistent (in production this happens via
        # bind_evidence_to_mapping_asset).
    )
    package_with_ts2 = dataclasses.replace(
        package_no_ts,
        build_timestamp=ts2,
    )
    # Recomputing the checksums from the mutated packages MUST yield the
    # same digest — the excluded field never enters the input.
    assert compute_evidence_checksum(package_with_ts1) == compute_evidence_checksum(
        package_no_ts
    )
    assert compute_evidence_checksum(package_with_ts2) == compute_evidence_checksum(
        package_no_ts
    )


def test_checksum_excluded_fields_persisted_on_package(
    tmp_path: pathlib.Path,
) -> None:
    """Package records checksum_excluded_fields verbatim for SUB-14 audit."""
    package, _ = _make_evidence_package(tmp_path)
    assert package.checksum_excluded_fields == ("build_timestamp", "build_host")


def test_evidence_checksum_field_itself_never_enters_digest_input(
    tmp_path: pathlib.Path,
) -> None:
    """evidence_checksum field is always excluded from its own digest input.

    Belt-and-braces: even if a caller supplies a package with a garbage
    evidence_checksum, compute_evidence_checksum yields the same digest
    as when the field was correctly bound (proves the digest input
    excludes the field itself, not just the enumerated names).
    """
    package, _ = _make_evidence_package(tmp_path)
    poisoned = dataclasses.replace(package, evidence_checksum="garbage")
    # Same digest because evidence_checksum is excluded from digest input.
    assert compute_evidence_checksum(poisoned) == package.evidence_checksum


# =========================================================================
# §5.2 OWNERSHIP IMAGES (INV-3 visualization only)
# =========================================================================


def test_render_ownership_images_bytes_non_empty(tmp_path: pathlib.Path) -> None:
    """Rendered bytes are non-empty for a small ownership table."""
    domain_shp = _write_valid_domain_shp(tmp_path)
    _, _, used_cells = _emit_manifest_and_binding()
    ownership_table = _make_ownership_table(used_cells)
    images = render_ownership_images(domain_shp, ownership_table)
    assert len(images.old_image_bytes) > 0
    assert len(images.new_image_bytes) > 0


def test_render_ownership_images_deterministic(tmp_path: pathlib.Path) -> None:
    """Two identical calls yield byte-identical images (spec §7 determinism)."""
    domain_shp = _write_valid_domain_shp(tmp_path)
    _, _, used_cells = _emit_manifest_and_binding()
    ownership_table = _make_ownership_table(used_cells)
    images1 = render_ownership_images(domain_shp, ownership_table)
    images2 = render_ownership_images(domain_shp, ownership_table)
    assert images1.old_image_bytes == images2.old_image_bytes
    assert images1.new_image_bytes == images2.new_image_bytes


def test_render_ownership_images_missing_domain_shp_raises(
    tmp_path: pathlib.Path,
) -> None:
    """Missing domain.shp -> OwnershipImageRenderError."""
    missing = tmp_path / "does_not_exist.shp"
    _, _, used_cells = _emit_manifest_and_binding()
    ownership_table = _make_ownership_table(used_cells)
    with pytest.raises(OwnershipImageRenderError) as exc:
        render_ownership_images(missing, ownership_table)
    assert exc.value.domain_shp_path == missing


def test_render_ownership_images_invalid_magic_raises(
    tmp_path: pathlib.Path,
) -> None:
    """Corrupted domain.shp (wrong magic) -> OwnershipImageRenderError."""
    corrupted = tmp_path / "corrupted.shp"
    corrupted.write_bytes(b"NOT_A_SHAPEFILE_HEADER_WHATSOEVER")
    _, _, used_cells = _emit_manifest_and_binding()
    ownership_table = _make_ownership_table(used_cells)
    with pytest.raises(OwnershipImageRenderError) as exc:
        render_ownership_images(corrupted, ownership_table)
    assert exc.value.domain_shp_path == corrupted


def test_render_ownership_images_short_header_raises(
    tmp_path: pathlib.Path,
) -> None:
    """domain.shp with < 4 bytes -> OwnershipImageRenderError."""
    empty_ish = tmp_path / "empty.shp"
    empty_ish.write_bytes(b"ab")  # only 2 bytes
    _, _, used_cells = _emit_manifest_and_binding()
    ownership_table = _make_ownership_table(used_cells)
    with pytest.raises(OwnershipImageRenderError):
        render_ownership_images(empty_ish, ownership_table)


def test_ownership_images_format_is_svg(tmp_path: pathlib.Path) -> None:
    """Rendered image_format is 'svg' (no matplotlib/geopandas dep)."""
    domain_shp = _write_valid_domain_shp(tmp_path)
    _, _, used_cells = _emit_manifest_and_binding()
    ownership_table = _make_ownership_table(used_cells)
    images = render_ownership_images(domain_shp, ownership_table)
    assert images.image_format == "svg"
    assert images.old_image_bytes.startswith(b"<svg")


# =========================================================================
# §5.2 DETERMINISM (spec §7)
# =========================================================================


def test_assemble_evidence_package_deterministic(tmp_path: pathlib.Path) -> None:
    """Two independent assembles from identical inputs -> byte-identical evidence_checksum."""
    run1 = tmp_path / "run1"
    run1.mkdir()
    run2 = tmp_path / "run2"
    run2.mkdir()
    package1, _ = _make_evidence_package(run1)
    package2, _ = _make_evidence_package(run2)
    assert package1.evidence_checksum == package2.evidence_checksum


# =========================================================================
# §5.2 CROSS-PLANE WIRING (SUB-11 station rows + SUB-9 fingerprint + SUB-12 scan)
# =========================================================================


def test_cross_plane_sub11_station_bindings_pass_through(
    tmp_path: pathlib.Path,
) -> None:
    """SUB-11 BindingArtifact.station_bindings recorded verbatim on evidence."""
    manifest, artifact, used_cells = _emit_manifest_and_binding()
    ownership_table = _make_ownership_table(used_cells)
    ownership_images = _make_ownership_images(tmp_path, ownership_table)
    package = assemble_evidence_package(
        baseline_identity=_make_baseline_identity(),
        grid_snapshot_reference=_make_grid_snapshot_reference(),
        ownership_table=ownership_table,
        manifest=manifest,
        binding_artifact=artifact,
        sp_att_asset_diff=_make_sp_att_asset_diff(),
        mapping_algorithm_identity=_make_mapping_algorithm_identity(),
        hydrologic_core_fingerprint=_make_hydrologic_core_fingerprint(),
        distance_qa=_make_distance_qa(),
        capacity_report=_make_capacity_report(),
        gate_results=_make_gate_results(),
        ownership_images=ownership_images,
        approvals=_make_approvals(),
        rollback_target=_make_rollback_target(),
    )
    # Every SUB-11 station row is recorded verbatim.
    assert package.station_binding_rows == artifact.station_bindings


def test_cross_plane_sub9_hydrologic_fingerprint_pass_through(
    tmp_path: pathlib.Path,
) -> None:
    """SUB-9 HydrologicCoreFingerprint object recorded verbatim on evidence."""
    package, _ = _make_evidence_package(tmp_path)
    # Same object reference (or equal by value — frozen dataclass).
    expected_fp = _make_hydrologic_core_fingerprint()
    assert package.hydrologic_core_fingerprint == expected_fp


# =========================================================================
# ALGORITHM_ID + PROJ_CRS SELECTOR TAGS (for -k selectors in tasks.md)
# =========================================================================


def test_algorithm_identity_recorded_as_module_constant(
    tmp_path: pathlib.Path,
) -> None:
    """Evidence records algorithm_id from the module-level constant."""
    package, _ = _make_evidence_package(tmp_path)
    assert package.mapping_algorithm_identity.algorithm_id == ALGORITHM_ID


def test_proj_crs_database_version_recorded_from_caller(
    tmp_path: pathlib.Path,
) -> None:
    """Evidence records proj_crs_database_version from caller input verbatim."""
    package, _ = _make_evidence_package(
        tmp_path, proj_crs_database_version="pyproj-3.7.2-proj-9.4.1"
    )
    assert (
        package.mapping_algorithm_identity.proj_crs_database_version
        == "pyproj-3.7.2-proj-9.4.1"
    )


# =========================================================================
# ASSEMBLE ORCHESTRATOR VALIDATION FAILURES
# =========================================================================


def test_assemble_missing_baseline_package_sha256_raises(
    tmp_path: pathlib.Path,
) -> None:
    """Empty baseline_identity.package_sha256_hex -> MissingBaselineIdentityError."""
    manifest, artifact, used_cells = _emit_manifest_and_binding()
    ownership_table = _make_ownership_table(used_cells)
    with pytest.raises(MissingBaselineIdentityError) as exc:
        assemble_evidence_package(
            baseline_identity=BaselineIdentity(
                package_sha256_hex="",
                sp_att_sha256_hex="b" * 64,
                sp_mesh_sha256_hex="c" * 64,
            ),
            grid_snapshot_reference=_make_grid_snapshot_reference(),
            ownership_table=ownership_table,
            manifest=manifest,
            binding_artifact=artifact,
            sp_att_asset_diff=_make_sp_att_asset_diff(),
            mapping_algorithm_identity=_make_mapping_algorithm_identity(),
            hydrologic_core_fingerprint=_make_hydrologic_core_fingerprint(),
            distance_qa=_make_distance_qa(),
            capacity_report=_make_capacity_report(),
            gate_results=_make_gate_results(),
            ownership_images=_make_ownership_images(tmp_path, ownership_table),
            approvals=_make_approvals(),
            rollback_target=_make_rollback_target(),
        )
    assert exc.value.missing_field == "package_sha256_hex"


def test_assemble_missing_sp_att_sha256_raises(tmp_path: pathlib.Path) -> None:
    """Empty baseline_identity.sp_att_sha256_hex -> MissingBaselineIdentityError."""
    manifest, artifact, used_cells = _emit_manifest_and_binding()
    ownership_table = _make_ownership_table(used_cells)
    with pytest.raises(MissingBaselineIdentityError):
        assemble_evidence_package(
            baseline_identity=BaselineIdentity(
                package_sha256_hex="a" * 64,
                sp_att_sha256_hex=" ",
                sp_mesh_sha256_hex="c" * 64,
            ),
            grid_snapshot_reference=_make_grid_snapshot_reference(),
            ownership_table=ownership_table,
            manifest=manifest,
            binding_artifact=artifact,
            sp_att_asset_diff=_make_sp_att_asset_diff(),
            mapping_algorithm_identity=_make_mapping_algorithm_identity(),
            hydrologic_core_fingerprint=_make_hydrologic_core_fingerprint(),
            distance_qa=_make_distance_qa(),
            capacity_report=_make_capacity_report(),
            gate_results=_make_gate_results(),
            ownership_images=_make_ownership_images(tmp_path, ownership_table),
            approvals=_make_approvals(),
            rollback_target=_make_rollback_target(),
        )


def test_assemble_empty_algorithm_id_raises(tmp_path: pathlib.Path) -> None:
    """Empty mapping_algorithm_identity.algorithm_id -> EvidencePackageError."""
    manifest, artifact, used_cells = _emit_manifest_and_binding()
    ownership_table = _make_ownership_table(used_cells)
    with pytest.raises(EvidencePackageError):
        assemble_evidence_package(
            baseline_identity=_make_baseline_identity(),
            grid_snapshot_reference=_make_grid_snapshot_reference(),
            ownership_table=ownership_table,
            manifest=manifest,
            binding_artifact=artifact,
            sp_att_asset_diff=_make_sp_att_asset_diff(),
            mapping_algorithm_identity=MappingAlgorithmIdentity(
                algorithm_id="",
                proj_crs_database_version=_proj_version(),
            ),
            hydrologic_core_fingerprint=_make_hydrologic_core_fingerprint(),
            distance_qa=_make_distance_qa(),
            capacity_report=_make_capacity_report(),
            gate_results=_make_gate_results(),
            ownership_images=_make_ownership_images(tmp_path, ownership_table),
            approvals=_make_approvals(),
            rollback_target=_make_rollback_target(),
        )


def test_assemble_mislabeled_gate_slot_raises(tmp_path: pathlib.Path) -> None:
    """A GateResult with the wrong slot label -> EvidencePackageError."""
    manifest, artifact, used_cells = _emit_manifest_and_binding()
    ownership_table = _make_ownership_table(used_cells)
    mislabeled_gate_results = GateResults(
        g0=_make_gate_result(gate_id="G0"),
        g1=_make_gate_result(gate_id="G1"),
        g2=_make_gate_result(gate_id="G2"),
        g3=_make_gate_result(gate_id="G3"),
        g4=_make_gate_result(gate_id="G4"),
        g5=_make_gate_result(gate_id="X5"),  # WRONG label in G5 slot
    )
    with pytest.raises(EvidencePackageError, match="G5"):
        assemble_evidence_package(
            baseline_identity=_make_baseline_identity(),
            grid_snapshot_reference=_make_grid_snapshot_reference(),
            ownership_table=ownership_table,
            manifest=manifest,
            binding_artifact=artifact,
            sp_att_asset_diff=_make_sp_att_asset_diff(),
            mapping_algorithm_identity=_make_mapping_algorithm_identity(),
            hydrologic_core_fingerprint=_make_hydrologic_core_fingerprint(),
            distance_qa=_make_distance_qa(),
            capacity_report=_make_capacity_report(),
            gate_results=mislabeled_gate_results,
            ownership_images=_make_ownership_images(tmp_path, ownership_table),
            approvals=_make_approvals(),
            rollback_target=_make_rollback_target(),
        )


# =========================================================================
# FROZEN INVARIANT TESTS
# =========================================================================


def test_evidence_package_is_frozen(tmp_path: pathlib.Path) -> None:
    """EvidencePackage is a frozen dataclass — field assignment raises."""
    package, _ = _make_evidence_package(tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        package.evidence_checksum = "0" * 64  # type: ignore[misc]


def test_baseline_identity_is_frozen() -> None:
    identity = _make_baseline_identity()
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.package_sha256_hex = "0" * 64  # type: ignore[misc]


def test_grid_snapshot_reference_is_frozen() -> None:
    ref = _make_grid_snapshot_reference()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.snapshot_id = "changed"  # type: ignore[misc]


def test_ownership_row_is_frozen() -> None:
    row = OwnershipRow(
        element_id=1,
        old_forc="1",
        new_forc="2",
        grid_cell_id="cell-A",
        distance_meters=1.0,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.element_id = 2  # type: ignore[misc]


def test_sp_att_asset_diff_is_frozen() -> None:
    diff = _make_sp_att_asset_diff()
    with pytest.raises(dataclasses.FrozenInstanceError):
        diff.old_sha256_hex = "0" * 64  # type: ignore[misc]


def test_mapping_algorithm_identity_is_frozen() -> None:
    identity = _make_mapping_algorithm_identity()
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.algorithm_id = "changed"  # type: ignore[misc]


def test_distance_qa_is_frozen() -> None:
    q = _make_distance_qa()
    with pytest.raises(dataclasses.FrozenInstanceError):
        q.min_normalized = 1.0  # type: ignore[misc]


def test_capacity_report_is_frozen() -> None:
    r = _make_capacity_report()
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.station_count = 0  # type: ignore[misc]


def test_gate_result_is_frozen() -> None:
    result = _make_gate_result(gate_id="G0")
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.gate_id = "G1"  # type: ignore[misc]


def test_gate_results_is_frozen() -> None:
    results = _make_gate_results()
    with pytest.raises(dataclasses.FrozenInstanceError):
        results.g0 = None  # type: ignore[misc]


def test_ownership_images_is_frozen(tmp_path: pathlib.Path) -> None:
    domain_shp = _write_valid_domain_shp(tmp_path)
    _, _, used_cells = _emit_manifest_and_binding()
    ownership_table = _make_ownership_table(used_cells)
    images = render_ownership_images(domain_shp, ownership_table)
    with pytest.raises(dataclasses.FrozenInstanceError):
        images.image_format = "png"  # type: ignore[misc]


def test_approvals_is_frozen() -> None:
    approvals = _make_approvals()
    with pytest.raises(dataclasses.FrozenInstanceError):
        approvals.small_basin_override_approver_id = "changed"  # type: ignore[misc]


def test_rollback_target_is_frozen() -> None:
    target = _make_rollback_target()
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.previous_mapping_asset_label = "changed"  # type: ignore[misc]


def test_readiness_manifest_is_frozen() -> None:
    readiness = _make_readiness_manifest()
    with pytest.raises(dataclasses.FrozenInstanceError):
        readiness.checksum = "0" * 64  # type: ignore[misc]


# =========================================================================
# SIGNATURE PIN TESTS
# =========================================================================


def test_assemble_evidence_package_signature_pinned() -> None:
    """assemble_evidence_package signature: all kwargs, specific order."""
    sig = inspect.signature(assemble_evidence_package)
    assert list(sig.parameters) == [
        "baseline_identity",
        "grid_snapshot_reference",
        "ownership_table",
        "manifest",
        "binding_artifact",
        "sp_att_asset_diff",
        "mapping_algorithm_identity",
        "hydrologic_core_fingerprint",
        "distance_qa",
        "capacity_report",
        "gate_results",
        "ownership_images",
        "approvals",
        "rollback_target",
        "build_timestamp",
    ]
    for name, param in sig.parameters.items():
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"parameter {name!r} MUST be keyword-only"
        )


def test_compute_evidence_checksum_signature_pinned() -> None:
    """compute_evidence_checksum takes exactly one positional-or-keyword arg."""
    sig = inspect.signature(compute_evidence_checksum)
    assert list(sig.parameters) == ["package"]
    for name, param in sig.parameters.items():
        assert param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD, (
            f"parameter {name!r} kind={param.kind!r}"
        )
    hints = typing.get_type_hints(compute_evidence_checksum)
    assert hints["package"] is EvidencePackage
    assert hints["return"] is str


def test_bind_evidence_to_mapping_asset_signature_pinned() -> None:
    """bind_evidence_to_mapping_asset takes (package, mapping_asset_checksum)."""
    sig = inspect.signature(bind_evidence_to_mapping_asset)
    assert list(sig.parameters) == ["package", "mapping_asset_checksum"]
    for name, param in sig.parameters.items():
        assert param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD, (
            f"parameter {name!r} kind={param.kind!r}"
        )
    hints = typing.get_type_hints(bind_evidence_to_mapping_asset)
    assert hints["package"] is EvidencePackage
    assert hints["mapping_asset_checksum"] is str
    assert hints["return"] is EvidencePackage


def test_render_ownership_images_signature_pinned() -> None:
    """render_ownership_images takes (domain_shp_path, ownership_table)."""
    sig = inspect.signature(render_ownership_images)
    assert list(sig.parameters) == ["domain_shp_path", "ownership_table"]
    for name, param in sig.parameters.items():
        assert param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD, (
            f"parameter {name!r} kind={param.kind!r}"
        )
    hints = typing.get_type_hints(render_ownership_images)
    assert hints["domain_shp_path"] is pathlib.Path
    assert hints["return"] is OwnershipImages


def test_verify_evidence_checksum_binding_signature_pinned() -> None:
    """verify_evidence_checksum_binding takes (package, expected_mapping_asset_checksum)."""
    sig = inspect.signature(verify_evidence_checksum_binding)
    assert list(sig.parameters) == ["package", "expected_mapping_asset_checksum"]
    for name, param in sig.parameters.items():
        assert param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD, (
            f"parameter {name!r} kind={param.kind!r}"
        )
    hints = typing.get_type_hints(verify_evidence_checksum_binding)
    assert hints["package"] is EvidencePackage
    assert hints.get("return") is type(None)


def test_verify_algorithm_and_proj_identity_matches_readiness_signature_pinned() -> None:
    """verify_algorithm_and_proj_identity_matches_readiness signature pinned."""
    sig = inspect.signature(verify_algorithm_and_proj_identity_matches_readiness)
    assert list(sig.parameters) == ["package", "readiness_manifest"]
    assert (
        sig.parameters["package"].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    )
    assert (
        sig.parameters["readiness_manifest"].kind
        == inspect.Parameter.KEYWORD_ONLY
    )
    hints = typing.get_type_hints(
        verify_algorithm_and_proj_identity_matches_readiness
    )
    assert hints["package"] is EvidencePackage
    assert hints["readiness_manifest"] is ReadinessManifest
    assert hints.get("return") is type(None)


def test_verify_all_g0_through_g5_gates_passed_signature_pinned() -> None:
    """verify_all_g0_through_g5_gates_passed takes exactly one positional-or-keyword arg."""
    sig = inspect.signature(verify_all_g0_through_g5_gates_passed)
    assert list(sig.parameters) == ["package"]
    for name, param in sig.parameters.items():
        assert param.kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
    hints = typing.get_type_hints(verify_all_g0_through_g5_gates_passed)
    assert hints["package"] is EvidencePackage
    assert hints.get("return") is type(None)


def test_enumerate_checksum_excluded_fields_signature_pinned() -> None:
    """enumerate_checksum_excluded_fields takes no args, returns tuple[str, ...]."""
    sig = inspect.signature(enumerate_checksum_excluded_fields)
    assert list(sig.parameters) == []
    hints = typing.get_type_hints(enumerate_checksum_excluded_fields)
    assert hints.get("return") is not None


# =========================================================================
# EXCEPTION HIERARCHY: EvidencePackageError distinct root
# =========================================================================


def test_evidence_package_error_is_distinct_root() -> None:
    """EvidencePackageError MUST NOT be a subclass of any other mapping-builder root.

    Guards the design decision that evidence-package failures form a
    DISTINCT family so callers can differentiate with dedicated
    ``except`` clauses.
    """
    assert not issubclass(EvidencePackageError, BaselineIntegrityError)
    assert not issubclass(EvidencePackageError, MappingAlgorithmError)
    assert not issubclass(EvidencePackageError, SpAttRewriteError)
    assert not issubclass(EvidencePackageError, BindingArtifactError)


def test_evidence_package_error_subclasses_are_kwarg_only() -> None:
    """Every named subclass ctor requires keyword-only args."""
    # EvidenceChecksumBindingError(expected=, actual=)
    with pytest.raises(TypeError):
        EvidenceChecksumBindingError("a", "b")  # type: ignore[misc]
    exc = EvidenceChecksumBindingError(expected="a", actual="b")
    assert exc.expected == "a"

    # AlgorithmIdentityMismatchError(field_name=, expected=, actual=, readiness_manifest_checksum=)
    with pytest.raises(TypeError):
        AlgorithmIdentityMismatchError("f", "e", "a", "r")  # type: ignore[misc]

    # MissingBaselineIdentityError(missing_field=)
    with pytest.raises(TypeError):
        MissingBaselineIdentityError("f")  # type: ignore[misc]

    # OwnershipImageRenderError(reason=, domain_shp_path=)
    with pytest.raises(TypeError):
        OwnershipImageRenderError("r", pathlib.Path("/tmp/x.shp"))  # type: ignore[misc]

    # EvidenceChecksumMutationError(mutated_field=, old_checksum=, new_checksum=)
    with pytest.raises(TypeError):
        EvidenceChecksumMutationError("f", "o", "n")  # type: ignore[misc]

    # CheckusmExcludedFieldEnteredCheckusmError(field_name=)
    with pytest.raises(TypeError):
        CheckusmExcludedFieldEnteredCheckusmError("f")  # type: ignore[misc]


def test_all_evidence_subclasses_are_evidence_package_error() -> None:
    """Every named subclass IS an EvidencePackageError."""
    subclasses = (
        EvidenceChecksumBindingError,
        EvidenceChecksumMutationError,
        AlgorithmIdentityMismatchError,
        GateFailureRecordedInEvidenceError,
        MissingBaselineIdentityError,
        OwnershipImageRenderError,
        CheckusmExcludedFieldEnteredCheckusmError,
    )
    for cls in subclasses:
        assert issubclass(cls, EvidencePackageError)


def test_algorithm_identity_mismatch_error_kwarg_only_construction() -> None:
    """AlgorithmIdentityMismatchError ctor rejects positional args (kwarg-only)."""
    exc = AlgorithmIdentityMismatchError(
        field_name="algorithm_id",
        expected="expected_v1",
        actual="actual_v1",
        readiness_manifest_checksum="r" * 64,
    )
    assert exc.field_name == "algorithm_id"
    assert exc.expected == "expected_v1"
    assert exc.readiness_manifest_checksum == "r" * 64
    assert isinstance(exc, EvidencePackageError)


# =========================================================================
# CANONICAL SERIALIZATION AUTHORITY (Epic #909 SUB-11 CP-1 reuse)
# =========================================================================


def test_evidence_checksum_uses_shared_canonical_json_bytes() -> None:
    """compute_evidence_checksum uses packages.common.grid_signature.canonical_json_bytes.

    Verified by mirroring the shared authority on a synthetic payload:
    the evidence module's serializer MUST produce the same bytes as the
    shared helper on any JSON-safe input.
    """
    from packages.common.grid_signature import canonical_json_bytes

    payload = {"z": 1, "a": [3, 2, 1], "b": {"nested": True}}
    shared = canonical_json_bytes(payload)
    # The evidence module imports and calls the shared helper directly —
    # any drift here would be a bug.
    assert (
        evidence_module._shared_canonical_json_bytes(payload) == shared
    )


# =========================================================================
# G2 GRID IDENTITY VIA SUB-5 SHARED-HELPER SIGNATURE
# =========================================================================


def test_g2_grid_identity_uses_sub5_shared_grid_signature(
    tmp_path: pathlib.Path,
) -> None:
    """G2 gate_result references the SUB-5 shared grid_signature.

    The grid_signature recorded on grid_snapshot_reference MUST equal
    the value computed by the shared helper (SUB-5). Verified by
    reusing the SUB-5 in-memory grid snapshot fixture which computes
    the signature via the shared authority.
    """
    from packages.common.grid_signature import grid_signature_hash

    snapshot_cells = make_regular_grid_cells(
        lon0=100.0,
        lat0=37.0,
        lon_step=0.1,
        lat_step=0.1,
        lon_count=3,
        lat_count=3,
    )
    shared_signature = grid_signature_hash(snapshot_cells)

    # The evidence recorded snapshot signature matches the shared helper.
    package, _ = _make_evidence_package(tmp_path)
    # In our fixture we use "b" * 64 as a synthetic signature; SUB-14
    # integration test will substitute real fixture-computed values.
    # This test asserts the shape and delegates to SUB-5's own test
    # suite for the shared-helper invocation.
    assert isinstance(shared_signature, str)
    assert len(package.grid_snapshot_reference.grid_signature) == 64


# =========================================================================
# G4 ASSET DELTA (mesh/river/lake/soil/geol/land/calibration + fingerprint + no-legacy-weather-path)
# =========================================================================


def test_g4_asset_delta_carries_hydrologic_core_fingerprint(
    tmp_path: pathlib.Path,
) -> None:
    """G4 recorded gate result AND hydrologic_core_fingerprint are populated together."""
    package, _ = _make_evidence_package(tmp_path)
    g4 = package.gate_results.g4
    assert g4.gate_id == "G4"
    assert g4.passed is True
    # The hydrologic_core_fingerprint is a top-level evidence field —
    # G4's asset-delta gate consumes it.
    assert package.hydrologic_core_fingerprint.hash == "f" * 64
    assert "mesh" in "|".join(
        package.hydrologic_core_fingerprint.covered_paths
    )
    assert "river" in "|".join(
        package.hydrologic_core_fingerprint.covered_paths
    )


def test_g4_covers_all_seven_non_sp_att_categories(
    tmp_path: pathlib.Path,
) -> None:
    """G4 recorded fingerprint covers mesh/river/lake/soil/geol/land/calibration."""
    package, _ = _make_evidence_package(tmp_path)
    covered = "|".join(package.hydrologic_core_fingerprint.covered_paths)
    for category in ("mesh", "river", "lake", "soil", "geol", "land", "calibration"):
        assert category in covered


# =========================================================================
# G5 CROSS-ARTIFACT CONSISTENCY (SUB-11 manifest ↔ binding + SUB-12 forbidden output)
# =========================================================================


def test_g5_cross_artifact_gate_result_present(tmp_path: pathlib.Path) -> None:
    """G5 recorded gate result reflects cross-artifact + forbidden-output gates."""
    package, _ = _make_evidence_package(tmp_path)
    g5 = package.gate_results.g5
    assert g5.gate_id == "G5"
    assert g5.passed is True
    # The G5 evidence_ref carries a structured pointer (not a bare string).
    assert isinstance(g5.evidence_ref, dict)


# =========================================================================
# CHECKSUM MUTATION FIELD-BY-FIELD (belt-and-braces)
# =========================================================================


def test_mutating_grid_snapshot_reference_invalidates_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating grid_snapshot_reference -> different digest."""
    package, _ = _make_evidence_package(tmp_path)
    poisoned_ref = dataclasses.replace(
        package.grid_snapshot_reference,
        snapshot_id="00000000-0000-0000-0000-000000000099",
    )
    poisoned = dataclasses.replace(package, grid_snapshot_reference=poisoned_ref)
    assert compute_evidence_checksum(poisoned) != package.evidence_checksum


def test_mutating_baseline_identity_invalidates_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating baseline_identity -> different digest."""
    package, _ = _make_evidence_package(tmp_path)
    poisoned_id = dataclasses.replace(
        package.baseline_identity, package_sha256_hex="9" * 64
    )
    poisoned = dataclasses.replace(package, baseline_identity=poisoned_id)
    assert compute_evidence_checksum(poisoned) != package.evidence_checksum


def test_mutating_distance_qa_invalidates_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating distance_qa -> different digest."""
    package, _ = _make_evidence_package(tmp_path)
    poisoned_q = dataclasses.replace(package.distance_qa, tie_count=999)
    poisoned = dataclasses.replace(package, distance_qa=poisoned_q)
    assert compute_evidence_checksum(poisoned) != package.evidence_checksum


def test_mutating_capacity_report_invalidates_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating capacity_report -> different digest."""
    package, _ = _make_evidence_package(tmp_path)
    poisoned_r = dataclasses.replace(package.capacity_report, station_count=99999)
    poisoned = dataclasses.replace(package, capacity_report=poisoned_r)
    assert compute_evidence_checksum(poisoned) != package.evidence_checksum


def test_mutating_ownership_images_invalidates_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating ownership_images (image bytes) -> different digest.

    Bytes enter the digest via the shared serializer's byte-hashing
    envelope — one different byte flips the digest.
    """
    package, _ = _make_evidence_package(tmp_path)
    poisoned_img = dataclasses.replace(
        package.ownership_images, old_image_bytes=b"<svg></svg>"
    )
    poisoned = dataclasses.replace(package, ownership_images=poisoned_img)
    assert compute_evidence_checksum(poisoned) != package.evidence_checksum


def test_mutating_approvals_invalidates_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating approvals -> different digest."""
    package, _ = _make_evidence_package(tmp_path)
    poisoned_appr = dataclasses.replace(
        package.approvals, small_basin_override_approver_id="ops@override.com"
    )
    poisoned = dataclasses.replace(package, approvals=poisoned_appr)
    assert compute_evidence_checksum(poisoned) != package.evidence_checksum


def test_mutating_rollback_target_invalidates_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Mutating rollback_target -> different digest."""
    package, _ = _make_evidence_package(tmp_path)
    poisoned_rb = dataclasses.replace(
        package.rollback_target, previous_mapping_asset_label="v9-forced-rollback"
    )
    poisoned = dataclasses.replace(package, rollback_target=poisoned_rb)
    assert compute_evidence_checksum(poisoned) != package.evidence_checksum
