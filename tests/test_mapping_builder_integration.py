"""Keliya integration test for the full mapping-builder pipeline (Epic #909 SUB-14, §6.1 + §6.2).

Exercises the compact keliya fixture (484 elements / 32 stations / 8 used cells)
through every G0 -> G5 gate: baseline integrity, non-degenerate triangles, grid
identity, ownership + used-cell subset, .sp.att rewrite, hydrologic core
fingerprint, no-legacy-weather-path, direct-grid manifest + binding emission,
forbidden-output scan, and evidence assembly.

Coverage
--------

* ``test_full_pipeline_g0_through_g5_end_to_end`` — runs each gate in order
  and asserts the pass verdict for each.
* ``test_binding_round_trips_through_forcing_producer_contract_parser`` —
  emitted binding bytes parse cleanly through the existing direct-grid
  contract parser.
* ``test_used_cell_subset_shrinks_stations_to_cells`` — 32 stations -> 8
  used cells (~4x reduction) with 484 elements distributed across them.
* ``test_g4_asset_delta_only_forc_changed`` — variant .sp.att differs from
  baseline only in the FORC column; other columns byte-identical per the
  semantic diff.
* ``test_g4_hydrologic_core_fingerprint_equal`` — fingerprint(variant) ==
  fingerprint(baseline) since non-FORC hydrologic surfaces are unchanged.
* ``test_g5_manifest_and_binding_cross_consistent`` — manifest identity
  fields align with binding artifact + snapshot + variant .sp.att.
* ``test_evidence_checksum_binds_to_mapping_asset_checksum`` — post-bind
  binding + verify_evidence_checksum_binding pass.
* ``test_two_consecutive_builds_yield_byte_identical_output`` — determinism
  proof: two builds against the same inputs produce byte-identical
  bindings, .sp.att, and evidence (excluding build_timestamp).
* ``test_checksum_excluded_fields_enumerated_and_never_enter_any_checksum`` —
  enumeration matches and mutating build_timestamp preserves the checksum.
* ``test_forbidden_output_scan_clean_on_green_pipeline`` — SUB-12 scan
  passes on the mapping-builder's clean artifact set.
* ``test_g2_grid_identity_via_shared_signature_helper`` — G2 gate uses the
  shared ``packages.common.grid_signature.grid_signature_hash`` authority.
* ``test_epic_886_readiness_manifest_flows_into_evidence`` — Epic #886
  readiness manifest's nested proj_crs_database_version projects through
  the SUB-13 adapter and matches the evidence package identity.

Coverage extras
---------------

* ``test_mutating_non_excluded_field_changes_evidence_checksum`` — proves
  every non-``build_timestamp`` field enters the checksum (INV-7).
* ``test_ownership_svg_carries_truncation_marker_at_production_scale`` —
  proves the SUB-13 ownership SVG emits the ``truncated n_rendered=X
  n_total=484`` marker when the 484-element basin exceeds the canvas.
* ``test_algorithm_id_is_versioned_constant`` — algorithm-identity
  guardrail: pins ``algorithm.ALGORITHM_ID`` and ``evidence.ALGORITHM_ID``
  to the same versioned string, blocking silent rename drift.

Fixture
-------

Baseline package at ``tests/fixtures/mapping_builder/keliya/`` contains:

* ``keliya.sp.mesh`` — 484 elements, 276 nodes on a 23x12 lat/lon-major
  quad grid in the Albers CRS (Central_Meridian=105, Standard_Parallel_1=25,
  Standard_Parallel_2=47), non-square steps (1600m lon, 2000m lat) so the
  mesh footprint (35.2km x 22km) spans exactly 4 x 2 = 8 grid cells at
  lat 36N under nearest-cell mapping.
* ``keliya.sp.att`` — 484 rows striping FORC 1..32 across the elements.
* ``keliya.tsd.forc`` — 32 stations placed 4 per target cell at small
  offsets so each station snaps to its target cell center.
* ``gis/keliya.prj`` — Albers WKT verbatim from ``keliya_minimal``.

Regeneration
------------

Rebuilding the fixture via
``uv run python tests/fixtures/mapping_builder/keliya/build.py``
reproduces the checked-in fixture bytes under a pinned pyproj + PROJ
database version. Version drift may yield last-decimal drift in the
``.4f`` node coordinates and the ``.2f`` station X/Y projections, so a
runtime diff assertion would be flaky across environments. The
checked-in files are the authoritative test input and the test never
invokes ``build.py`` at runtime.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import shutil
import struct
from datetime import UTC, datetime

import pytest

from packages.common import grid_signature as grid_signature_module
from tests.fixtures.mapping_builder.in_memory_grid_snapshot import (
    InMemoryGridSnapshotLoader,
    make_regular_grid_cells,
    make_snapshot,
)
from workers.forcing_producer.direct_grid_contract import DirectGridForcingContract
from workers.mapping_builder import (
    ALGORITHM_ID,
    EVIDENCE_CHECKSUM_EXCLUDED_FIELDS,
    Approvals,
    BaselineIdentity,
    CapacityReport,
    CycleLineageSpy,
    DbWriteSpy,
    DistanceQA,
    EvidencePackage,
    GateResult,
    GateResults,
    GridSignatureMismatchError,
    GridSnapshotReference,
    MappingAlgorithmIdentity,
    OwnershipRow,
    ReadinessManifest,
    RollbackTarget,
    SpAttAssetDiff,
    ZPolicy,
    algorithm_id,
    assemble_evidence_package,
    assign_shud_forcing_index,
    bind_evidence_to_mapping_asset,
    build_ancillary_inventory,
    classify_baseline,
    compute_evidence_checksum,
    compute_hydrologic_core_fingerprint,
    copy_and_rewrite_sp_att_forc,
    derive_used_cell_subset,
    emit_direct_grid_manifest_and_binding,
    enumerate_checksum_excluded_fields,
    nearest_cell_barycenter_geodesic_v1,
    project_readiness_proj_crs_database_version,
    render_ownership_images,
    verify_algorithm_and_proj_identity_matches_readiness,
    verify_all_g0_through_g5_gates_passed,
    verify_baseline_inv1_end_to_end,
    verify_binding_round_trips_parser,
    verify_evidence_checksum_binding,
    verify_g0_baseline,
    verify_g1_non_degenerate_triangles,
    verify_grid_identity_precondition,
    verify_hydrologic_core_fingerprint_equal,
    verify_manifest_binding_cross_consistent,
    verify_no_forbidden_runtime_producer_artifacts,
    verify_no_legacy_weather_path_in_active_tree,
    verify_non_forc_columns_unchanged,
    verify_non_sp_att_checksums_equal,
    verify_package_crs,
    verify_small_basin_gate,
)


# Epic #886 readiness manifest — the SUB-13 adapter's canonical input source.
# The change may live either as a live change or an archived one; the test
# accepts whichever path resolves first so archival is not a coupled edit.
def _resolve_epic_886_readiness_manifest() -> pathlib.Path:
    openspec_root = pathlib.Path(__file__).resolve().parent.parent / "openspec"
    live = (
        openspec_root
        / "changes"
        / "cmfd-direct-grid-platform-readiness"
        / "evidence"
        / "readiness-manifest.v1.json"
    )
    if live.exists():
        return live
    archive_dir = openspec_root / "changes" / "archive"
    if archive_dir.exists():
        for archived in sorted(archive_dir.glob("*-cmfd-direct-grid-platform-readiness")):
            candidate = archived / "evidence" / "readiness-manifest.v1.json"
            if candidate.exists():
                return candidate
    return live


_EPIC_886_READINESS_MANIFEST = _resolve_epic_886_readiness_manifest()

# --- fixture constants -----------------------------------------------------

_KELIYA_FIXTURE_DIR = (
    pathlib.Path(__file__).parent / "fixtures" / "mapping_builder" / "keliya"
)

_SOURCE_ID = "IFS"  # normalized form accepted by direct_grid_contract parser
_GRID_ID = "grid_test_v1"
_MAPPING_ASSET_IDENTITY = "mapping-v1-keliya"
_MODEL_INPUT_PACKAGE_ID = "pkg-v1-keliya"

# Grid-snapshot design matching the mesh footprint (4x2 target cells at
# lat 36.1/36.2, lon 100.1/100.2/100.3/100.4). Cell steps 0.1 deg.
_GRID_LON0 = 100.0
_GRID_LAT0 = 36.0
_GRID_STEP = 0.1
_GRID_LON_COUNT = 6
_GRID_LAT_COUNT = 6


def _make_snapshot_and_loader(
    *,
    source_id: str = _SOURCE_ID,
    grid_id: str = _GRID_ID,
):
    """Return (snapshot, snapshot_cells, loader) for the keliya integration."""
    cells = make_regular_grid_cells(
        lon0=_GRID_LON0,
        lat0=_GRID_LAT0,
        lon_step=_GRID_STEP,
        lat_step=_GRID_STEP,
        lon_count=_GRID_LON_COUNT,
        lat_count=_GRID_LAT_COUNT,
    )
    snapshot = make_snapshot(
        source_id=source_id,
        grid_id=grid_id,
        cells=cells,
        bbox_pad=0.5,
    )
    loader = InMemoryGridSnapshotLoader(
        source_id=source_id,
        grid_id=grid_id,
        snapshot=snapshot,
        cells=cells,
    )
    return snapshot, cells, loader


def _copy_baseline(target_root: pathlib.Path) -> pathlib.Path:
    """Deep-copy the keliya fixture into ``target_root/keliya`` and return it."""
    dest = target_root / "keliya"
    shutil.copytree(_KELIYA_FIXTURE_DIR, dest)
    # Remove build.py from the copy — it is not part of the runtime baseline
    # package and would confuse the G0 file-iteration if the test tightens
    # the file whitelist later. build.py never enters the pipeline.
    (dest / "build.py").unlink()
    return dest


def _write_hydrologic_core_stub_files(package_root: pathlib.Path) -> dict[str, tuple[str, ...]]:
    """Write minimal stub files for the seven §3.3/§3.4 categories.

    The keliya fixture does not carry real river/lake/soil/geol/land/calibration
    files; we write empty-ish stubs alongside the copy so the §3.3 file-checksum
    gate and §3.4 fingerprint have real bytes to hash. The same stub bytes go to
    both baseline and variant packages (the §3.3 gate proves EQUALITY between
    them, not any semantic meaning of the bytes).

    Returns the ``category_files`` mapping expected by
    :func:`verify_non_sp_att_checksums_equal` and
    :func:`compute_hydrologic_core_fingerprint`.
    """
    category_paths = {
        "mesh": ("keliya.sp.mesh",),  # already present from the fixture copy
        "river": ("river/keliya.riv",),
        "lake": ("lake/keliya.lake",),
        "soil": ("gis/keliya.soil",),
        "geol": ("gis/keliya.geol",),
        "land": ("gis/keliya.land",),
        "calibration": ("cal/keliya.calib",),
    }
    for _label, rel_paths in category_paths.items():
        for rel_path in rel_paths:
            target = package_root / rel_path
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # Deterministic stub content keyed to the relative path so
            # baseline + variant get the same bytes for the equality gate.
            target.write_bytes(f"stub:{rel_path}\n".encode("utf-8"))
    return category_paths


def _write_valid_domain_shp(target: pathlib.Path) -> pathlib.Path:
    """Write a minimal shapefile with the correct ESRI magic number."""
    target.mkdir(parents=True, exist_ok=True)
    domain_shp = target / "domain.shp"
    magic = struct.pack(">i", 9994)
    header_padding = b"\x00" * 96
    domain_shp.write_bytes(magic + header_padding)
    return domain_shp


# --- helpers ---------------------------------------------------------------


def _build_pipeline(
    tmp_path: pathlib.Path,
    *,
    mapping_asset_identity: str = _MAPPING_ASSET_IDENTITY,
) -> dict:
    """Run G0 -> G5 pipeline against a fresh baseline copy and return artifacts.

    Returns a dict with baseline_root, variant_root, ownerships, used_cells,
    shud_forcing_index, snapshot, snapshot_cells, sp_att_rewrite_report,
    manifest, binding_artifact, category_files, crs_report, sp_att_bytes.
    """
    baseline_root = _copy_baseline(tmp_path)
    variant_root = tmp_path / "variant"
    variant_root.mkdir()

    # G0/G1/CRS.
    g0 = verify_g0_baseline(baseline_root)
    g1 = verify_g1_non_degenerate_triangles(baseline_root)
    crs = verify_package_crs(baseline_root)

    # G2/G3.
    snapshot, snapshot_cells, loader = _make_snapshot_and_loader()
    ownerships = nearest_cell_barycenter_geodesic_v1(
        baseline_root, _SOURCE_ID, _GRID_ID, loader
    )
    used_cells = derive_used_cell_subset(ownerships, snapshot_cells)
    shud_forcing_index = assign_shud_forcing_index(used_cells)
    verify_small_basin_gate(used_cells)

    # G4: rewrite .sp.att.
    baseline_att = baseline_root / "keliya.sp.att"
    variant_att = variant_root / "keliya.sp.att"
    sp_att_report = copy_and_rewrite_sp_att_forc(
        baseline_att_path=baseline_att,
        variant_att_path=variant_att,
        ownership=ownerships,
        shud_forcing_index=shud_forcing_index,
        used_cell_count=len(used_cells),
    )
    verify_non_forc_columns_unchanged(baseline_att, variant_att)

    # Stub non-.sp.att category files (identical bytes on both sides).
    category_files = _write_hydrologic_core_stub_files(baseline_root)
    _write_hydrologic_core_stub_files(variant_root)

    # Ensure the mesh reference is inside variant_root too (the §3.3 gate
    # resolves the "mesh" category relative path under both roots).
    (variant_root / "keliya.sp.mesh").write_bytes(
        (baseline_root / "keliya.sp.mesh").read_bytes()
    )

    # §3.3 file-checksum equality gate.
    verify_non_sp_att_checksums_equal(
        baseline_root,
        variant_root,
        category_files=category_files,
    )

    # §3.4 hydrologic core fingerprint equality.
    fingerprint = verify_hydrologic_core_fingerprint_equal(
        baseline_root,
        variant_root,
        baseline_sp_att_path=baseline_att,
        variant_sp_att_path=variant_att,
        category_files=category_files,
        baseline_state_schema_bytes=b"state-schema-v1",
        variant_state_schema_bytes=b"state-schema-v1",
        baseline_solver_config_bytes=b"solver-config-v1",
        variant_solver_config_bytes=b"solver-config-v1",
    )

    # §3.5 no-legacy-weather-path in active tree. The active forcing subtree
    # in the variant does not exist in the minimal fixture; make an empty
    # subdirectory so the gate has a directory to scan.
    active_forcing_dir = variant_root / "input"
    active_forcing_dir.mkdir(parents=True, exist_ok=True)
    verify_no_legacy_weather_path_in_active_tree(
        variant_root, active_forcing_subdir="input"
    )

    # G5: emit manifest + binding.
    z_policy = ZPolicy(
        policy_name="sentinel",
        readiness_manifest_checksum="r" * 64,
        per_cell_z={c.grid_cell_id: -9999.0 for c in used_cells},
    )
    sp_att_bytes = variant_att.read_bytes()
    manifest, binding_artifact = emit_direct_grid_manifest_and_binding(
        used_cells=used_cells,
        snapshot_cells=snapshot_cells,
        shud_forcing_index=shud_forcing_index,
        mapping_asset_identity=mapping_asset_identity,
        model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
        sp_att_path="keliya/keliya.sp.att",
        sp_att_bytes=sp_att_bytes,
        applicable_source_ids=(_SOURCE_ID,),
        grid_id=_GRID_ID,
        grid_signature=snapshot.grid_signature,
        z_policy=z_policy,
        binding_uri="s3://bucket/mapping-v1-keliya/binding.json",
        model_crs_wkt=crs.wkt,
    )

    return {
        "baseline_root": baseline_root,
        "variant_root": variant_root,
        "g0": g0,
        "g1": g1,
        "crs_report": crs,
        "snapshot": snapshot,
        "snapshot_cells": snapshot_cells,
        "ownerships": ownerships,
        "used_cells": used_cells,
        "shud_forcing_index": shud_forcing_index,
        "sp_att_report": sp_att_report,
        "sp_att_bytes": sp_att_bytes,
        "category_files": category_files,
        "manifest": manifest,
        "binding_artifact": binding_artifact,
        "hydrologic_core_fingerprint": fingerprint,
        "z_policy": z_policy,
    }


def _make_forbidden_output_scan(build_result: dict):
    """Run the §8.1 forbidden-output scan on the emitted artifact set.

    Walk the actual on-disk artifacts under ``variant_root`` rather than a
    hardcoded declared list — the SUB-12 gate protects against unexpected
    files, not just those the pipeline declares upfront. A rogue file
    dropped in the variant subtree by a future regression must reach the
    gate so its filename gets checked against the forbidden regexes.
    """
    variant = build_result["variant_root"]
    emitted = sorted(
        (p for p in variant.rglob("*") if p.is_file()),
        key=lambda p: str(p),
    )
    return verify_no_forbidden_runtime_producer_artifacts(
        emitted,
        db_write_spy=DbWriteSpy(),
        cycle_lineage_spy=CycleLineageSpy(),
    )


def _assemble_full_evidence(
    tmp_path: pathlib.Path,
    build_result: dict,
    *,
    build_timestamp: datetime | None = None,
    proj_crs_database_version: str = "proj:9.5.1 db-major:1 db-minor:20 proj-data:1.19",
) -> EvidencePackage:
    """Assemble a full evidence package for the keliya build."""
    scan = _make_forbidden_output_scan(build_result)

    ownership_rows: list[OwnershipRow] = []
    baseline_att_rows = {
        int(row[0]): int(row[4])
        for row in (
            line.split()
            for line in (build_result["baseline_root"] / "keliya.sp.att")
            .read_text()
            .splitlines()[2:]
        )
    }
    for own in build_result["ownerships"]:
        old_forc = baseline_att_rows[own.element_id]
        new_forc = build_result["shud_forcing_index"][own.grid_cell_id]
        ownership_rows.append(
            OwnershipRow(
                element_id=own.element_id,
                old_forc=str(old_forc),
                new_forc=str(new_forc),
                grid_cell_id=own.grid_cell_id,
                distance_meters=float(own.geodesic_distance_m),
            )
        )

    domain_shp = _write_valid_domain_shp(tmp_path)
    ownership_images = render_ownership_images(domain_shp, tuple(ownership_rows))

    manifest = build_result["manifest"]
    binding_artifact = build_result["binding_artifact"]
    baseline_root = build_result["baseline_root"]
    g0 = build_result["g0"]

    return assemble_evidence_package(
        baseline_identity=BaselineIdentity(
            package_sha256_hex=g0.package_checksum,
            sp_att_sha256_hex=hashlib.sha256(
                (baseline_root / "keliya.sp.att").read_bytes()
            ).hexdigest(),
            sp_mesh_sha256_hex=hashlib.sha256(
                (baseline_root / "keliya.sp.mesh").read_bytes()
            ).hexdigest(),
        ),
        grid_snapshot_reference=GridSnapshotReference(
            snapshot_id=str(build_result["snapshot"].grid_snapshot_id),
            grid_signature=build_result["snapshot"].grid_signature,
            snapshot_checksum=build_result["snapshot"].grid_definition_checksum,
        ),
        ownership_table=tuple(ownership_rows),
        manifest=manifest,
        binding_artifact=binding_artifact,
        sp_att_asset_diff=SpAttAssetDiff(
            old_sha256_hex=build_result["sp_att_report"].checksums.baseline_sha256,
            new_sha256_hex=build_result["sp_att_report"].checksums.variant_sha256,
            semantic_diff_summary=build_result["sp_att_report"].semantic_diff,
        ),
        mapping_algorithm_identity=MappingAlgorithmIdentity(
            algorithm_id=ALGORITHM_ID,
            proj_crs_database_version=proj_crs_database_version,
        ),
        hydrologic_core_fingerprint=build_result["hydrologic_core_fingerprint"],
        forbidden_output_scan=scan,
        distance_qa=DistanceQA(
            min_normalized=0.0,
            p50_normalized=0.3,
            p95_normalized=0.7,
            max_normalized=0.95,
            tie_count=0,
            coverage_edge_count=0,
        ),
        capacity_report=CapacityReport(
            station_count=len(build_result["used_cells"]),
            timestep_count=48,
            timeseries_row_count=48 * len(build_result["used_cells"]),
            file_size_bytes=1_048_576,
            station_count_limit=5000,
            timestep_count_limit=336,
            timeseries_row_count_limit=1_500_000,
            file_size_bytes_limit=100_000_000,
            before_station_count=32,
            after_station_count=len(build_result["used_cells"]),
            station_reduction_ratio=32 / len(build_result["used_cells"]),
        ),
        gate_results=GateResults(
            g0=GateResult(
                gate_id="G0",
                passed=True,
                evidence_ref={"kind": "baseline_integrity_report", "checksum": g0.package_checksum},
            ),
            g1=GateResult(
                gate_id="G1",
                passed=True,
                evidence_ref={
                    "kind": "g1_non_degenerate_report",
                    "min_area": build_result["g1"].min_observed_area,
                },
            ),
            g2=GateResult(
                gate_id="G2",
                passed=True,
                evidence_ref={
                    "kind": "grid_identity_precondition",
                    "grid_signature": build_result["snapshot"].grid_signature,
                },
            ),
            g3=GateResult(
                gate_id="G3",
                passed=True,
                evidence_ref={
                    "kind": "ownership_table",
                    "used_cell_count": len(build_result["used_cells"]),
                },
            ),
            g4=GateResult(
                gate_id="G4",
                passed=True,
                evidence_ref={
                    "kind": "sp_att_rewrite_report",
                    "variant_sha256": build_result[
                        "sp_att_report"
                    ].checksums.variant_sha256,
                },
            ),
            g5=GateResult(
                gate_id="G5",
                passed=True,
                evidence_ref={
                    "kind": "manifest_binding_cross_consistent",
                    "binding_checksum": manifest.binding_checksum,
                },
            ),
        ),
        ownership_images=ownership_images,
        approvals=Approvals(
            builder_approver_id="tester@example.com",
            reviewer_approver_id="reviewer@example.com",
            small_basin_override_approver_id=None,
        ),
        rollback_target=RollbackTarget(
            previous_mapping_asset_checksum="",
            previous_mapping_asset_label="<initial>",
        ),
        build_timestamp=build_timestamp,
    )


# =========================================================================
# 1. FULL PIPELINE G0 -> G5
# =========================================================================


def test_full_pipeline_g0_through_g5_end_to_end(tmp_path: pathlib.Path) -> None:
    """Run every G0-G5 gate against the keliya fixture; every gate passes."""
    result = _build_pipeline(tmp_path)

    # G0: baseline integrity.
    assert len(result["g0"].element_id_set) == 484
    assert result["g0"].max_forc_value == 32
    assert result["g0"].tsd_forc_reference_count == 32

    # G1: non-degenerate triangles.
    assert result["g1"].element_count == 484
    assert result["g1"].min_observed_area > 1e-6

    # INV-1 end-to-end (§1.3): baseline unchanged after full-stack read.
    inv1 = verify_baseline_inv1_end_to_end(result["baseline_root"])
    assert inv1.pre_checksums == inv1.post_checksums

    # Ancillary inventory + classification are RECORD-ONLY.
    inventory = build_ancillary_inventory(result["baseline_root"])
    classification = classify_baseline(result["baseline_root"])
    assert isinstance(inventory.entries, tuple)
    assert isinstance(classification.startdate_heterogeneity, tuple)

    # G2 grid identity was already exercised inside `_build_pipeline` via
    # `nearest_cell_barycenter_geodesic_v1`, which invokes
    # `verify_grid_identity_precondition` internally (see
    # test_g2_grid_identity_via_shared_signature_helper below for the
    # negative-path proof against a tampered signature).

    # G3: 484 ownerships / 8 used cells / shud_forcing_index 1..8.
    assert len(result["ownerships"]) == 484
    assert len(result["used_cells"]) == 8
    assert sorted(result["shud_forcing_index"].values()) == list(range(1, 9))

    # G4: sp.att rewrite + semantic diff + hydrologic_core_fingerprint.
    assert result["sp_att_report"].rewritten_row_count == 484
    assert result["sp_att_report"].used_cell_count == 8
    assert result["hydrologic_core_fingerprint"].hash
    assert len(result["hydrologic_core_fingerprint"].covered_paths) == 10

    # G5: manifest carries 8 bindings, one per used cell.
    assert len(result["manifest"].station_bindings) == 8
    assert result["manifest"].sp_att_checksum == hashlib.sha256(
        result["sp_att_bytes"]
    ).hexdigest()

    # §8.1: no forbidden runtime producer artifact.
    scan = _make_forbidden_output_scan(result)
    assert scan.passed
    assert scan.offending_paths == ()
    assert scan.offending_db_writes == ()
    assert scan.cycle_lineage_records == ()

    # Evidence assembly + bind + all-gates-passed.
    package = _assemble_full_evidence(tmp_path, result)
    verify_all_g0_through_g5_gates_passed(package)


# =========================================================================
# 2. BINDING ROUND-TRIPS THROUGH PARSER
# =========================================================================


def test_binding_round_trips_through_forcing_producer_contract_parser(
    tmp_path: pathlib.Path,
) -> None:
    """Emitted binding parses cleanly through the direct-grid contract parser."""
    result = _build_pipeline(tmp_path)
    manifest = result["manifest"]
    contract = verify_binding_round_trips_parser(
        manifest.to_resource_profile_dict(), source_id=_SOURCE_ID
    )
    assert isinstance(contract, DirectGridForcingContract)
    assert contract.forcing_mapping_mode == "direct_grid"
    assert contract.grid_id == _GRID_ID
    assert contract.grid_signature == result["snapshot"].grid_signature
    assert len(contract.stations) == len(manifest.station_bindings)
    # Row set aligns per-station (station_id + shud_forcing_index +
    # grid_cell_id).
    parsed_by_id = {s.station_id: s for s in contract.stations}
    for m_row in manifest.station_bindings:
        p_row = parsed_by_id[m_row.station_id]
        assert p_row.shud_forcing_index == m_row.shud_forcing_index
        assert p_row.grid_cell_id == m_row.grid_cell_id


# =========================================================================
# 3. USED-CELL SUBSET SHRINKS STATIONS TO CELLS
# =========================================================================


def test_used_cell_subset_shrinks_stations_to_cells(tmp_path: pathlib.Path) -> None:
    """484 elements / 32 stations / 8 used cells (~4x reduction narrative)."""
    result = _build_pipeline(tmp_path)
    assert len(result["used_cells"]) == 8
    # 32 stations in the baseline .tsd.forc.
    tsd_forc_line1 = (result["baseline_root"] / "keliya.tsd.forc").read_text().splitlines()[0]
    assert tsd_forc_line1.split()[0] == "32"
    # 8 used cells -> ~4x reduction from 32 stations. Assert the ratio lands
    # in the docs §14 framing (~4-5x, not 1x and not 10x).
    ratio = 32 / len(result["used_cells"])
    assert 3.5 < ratio < 5.5


# =========================================================================
# 4. G4 ASSET DELTA: ONLY FORC CHANGED
# =========================================================================


def test_g4_asset_delta_only_forc_changed(tmp_path: pathlib.Path) -> None:
    """Variant .sp.att differs from baseline only in the FORC column."""
    result = _build_pipeline(tmp_path)
    baseline_att = result["baseline_root"] / "keliya.sp.att"
    variant_att = result["variant_root"] / "keliya.sp.att"

    # verify_non_forc_columns_unchanged is fail-closed; it returned None so
    # the non-FORC columns are byte-identical keyed by element_id.
    verify_non_forc_columns_unchanged(baseline_att, variant_att)

    # Semantic diff records only FORC deltas — the diff is non-empty (most
    # elements' FORC changed from 1..32 to 1..8) and every entry only carries
    # element_id + old_forc + new_forc.
    diff = result["sp_att_report"].semantic_diff
    assert len(diff.entries) > 0
    for entry in diff.entries:
        assert entry.old_forc != entry.new_forc
        assert 1 <= entry.new_forc <= 8  # rewritten to shud_forcing_index
        assert 1 <= entry.old_forc <= 32  # original station-id range


# =========================================================================
# 5. G4 HYDROLOGIC CORE FINGERPRINT EQUAL
# =========================================================================


def test_g4_hydrologic_core_fingerprint_equal(tmp_path: pathlib.Path) -> None:
    """Fingerprint(variant) == fingerprint(baseline) since non-FORC surfaces unchanged."""
    result = _build_pipeline(tmp_path)
    baseline_root = result["baseline_root"]
    variant_root = result["variant_root"]
    category_files = result["category_files"]
    baseline_att = baseline_root / "keliya.sp.att"
    variant_att = variant_root / "keliya.sp.att"

    baseline_fp = compute_hydrologic_core_fingerprint(
        baseline_root,
        sp_att_path=baseline_att,
        category_files=category_files,
        state_schema_bytes=b"state-schema-v1",
        solver_config_bytes=b"solver-config-v1",
    )
    variant_fp = compute_hydrologic_core_fingerprint(
        variant_root,
        sp_att_path=variant_att,
        category_files=category_files,
        state_schema_bytes=b"state-schema-v1",
        solver_config_bytes=b"solver-config-v1",
    )
    assert baseline_fp.hash == variant_fp.hash
    # And equals the fingerprint the pipeline already computed.
    assert baseline_fp.hash == result["hydrologic_core_fingerprint"].hash


# =========================================================================
# 6. G5 MANIFEST + BINDING CROSS-CONSISTENT
# =========================================================================


def test_g5_manifest_and_binding_cross_consistent(tmp_path: pathlib.Path) -> None:
    """Manifest identity fields align with binding artifact + snapshot + variant sp.att."""
    result = _build_pipeline(tmp_path)
    manifest = result["manifest"]
    binding_artifact = result["binding_artifact"]
    snapshot = result["snapshot"]
    sp_att_bytes = result["sp_att_bytes"]

    # Manifest.grid_signature == snapshot.grid_signature.
    assert manifest.grid_signature == snapshot.grid_signature
    # Manifest.binding_checksum == sha256(binding_bytes).
    assert manifest.binding_checksum == hashlib.sha256(binding_artifact.bytes).hexdigest()
    # Manifest.sp_att_checksum == sha256(variant_sp_att_bytes).
    assert manifest.sp_att_checksum == hashlib.sha256(sp_att_bytes).hexdigest()

    # Cross-consistency gate returns None on pass.
    assert (
        verify_manifest_binding_cross_consistent(manifest, binding_artifact) is None
    )

    # Station rows map 1:1 between manifest and binding artifact.
    manifest_by_id = {b.station_id: b for b in manifest.station_bindings}
    artifact_by_id = {b.station_id: b for b in binding_artifact.station_bindings}
    assert set(manifest_by_id) == set(artifact_by_id)
    for station_id, m_row in manifest_by_id.items():
        a_row = artifact_by_id[station_id]
        assert m_row.grid_cell_id == a_row.grid_cell_id
        assert m_row.shud_forcing_index == a_row.shud_forcing_index


# =========================================================================
# 7. EVIDENCE CHECKSUM BINDS TO MAPPING ASSET
# =========================================================================


def test_evidence_checksum_binds_to_mapping_asset_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """After bind_evidence_to_mapping_asset, verify_evidence_checksum_binding passes."""
    result = _build_pipeline(tmp_path)
    package = _assemble_full_evidence(tmp_path, result)

    # Bind to a synthetic mapping-asset checksum.
    mapping_asset_checksum = hashlib.sha256(b"synthetic-mapping-asset").hexdigest()
    bound = bind_evidence_to_mapping_asset(package, mapping_asset_checksum)

    assert bound.bound_mapping_asset_checksum == mapping_asset_checksum
    # Gate: verify_evidence_checksum_binding returns None on pass.
    assert (
        verify_evidence_checksum_binding(bound, mapping_asset_checksum) is None
    )


# =========================================================================
# 8. DETERMINISM PROOF: TWO CONSECUTIVE BUILDS BYTE-IDENTICAL
# =========================================================================


def test_two_consecutive_builds_yield_byte_identical_output(
    tmp_path: pathlib.Path,
) -> None:
    """Two builds against the same baseline + snapshot + algorithm version -> byte-identical output.

    Also proves that mutating ``build_timestamp`` between builds does NOT
    change ``evidence_checksum`` (per §5.2 excluded-field discipline).
    """
    build_a = _build_pipeline(tmp_path / "a")
    build_b = _build_pipeline(tmp_path / "b")

    # Binding bytes byte-identical.
    assert build_a["binding_artifact"].bytes == build_b["binding_artifact"].bytes
    # Binding checksum byte-identical.
    assert build_a["binding_artifact"].checksum == build_b["binding_artifact"].checksum

    # Variant .sp.att bytes byte-identical.
    assert build_a["sp_att_bytes"] == build_b["sp_att_bytes"]
    # And sp_att_checksum byte-identical.
    assert (
        build_a["sp_att_report"].checksums.variant_sha256
        == build_b["sp_att_report"].checksums.variant_sha256
    )

    # Two evidence packages with DIFFERENT build_timestamps but otherwise
    # identical inputs -> identical evidence_checksum (the timestamp is
    # excluded from the digest).
    ts_1 = datetime(2026, 1, 1, tzinfo=UTC)
    ts_2 = datetime(2026, 6, 30, tzinfo=UTC)
    package_1 = _assemble_full_evidence(
        tmp_path / "ev1", build_a, build_timestamp=ts_1
    )
    package_2 = _assemble_full_evidence(
        tmp_path / "ev2", build_b, build_timestamp=ts_2
    )
    assert package_1.evidence_checksum == package_2.evidence_checksum

    # Bind to the same mapping-asset checksum -> bound evidence_checksums equal too.
    mapping_asset_checksum = hashlib.sha256(b"synthetic-mapping-asset").hexdigest()
    bound_1 = bind_evidence_to_mapping_asset(package_1, mapping_asset_checksum)
    bound_2 = bind_evidence_to_mapping_asset(package_2, mapping_asset_checksum)
    assert bound_1.evidence_checksum == bound_2.evidence_checksum


# =========================================================================
# 9. CHECKSUM EXCLUDED FIELDS ENUMERATED + NEVER ENTER CHECKSUM
# =========================================================================


def test_checksum_excluded_fields_enumerated_and_never_enter_any_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """``enumerate_checksum_excluded_fields()`` matches the module constant."""
    excluded = enumerate_checksum_excluded_fields()
    assert excluded == EVIDENCE_CHECKSUM_EXCLUDED_FIELDS
    assert excluded == ("build_timestamp",)

    # Mutating build_timestamp preserves the checksum.
    result = _build_pipeline(tmp_path)
    package = _assemble_full_evidence(
        tmp_path,
        result,
        build_timestamp=datetime(2026, 3, 15, tzinfo=UTC),
    )
    original_checksum = package.evidence_checksum
    mutated = dataclasses.replace(
        package, build_timestamp=datetime(2026, 9, 30, tzinfo=UTC)
    )
    assert compute_evidence_checksum(mutated) == original_checksum


# =========================================================================
# 9b. NON-EXCLUDED FIELD REGRESSION GUARD: every non-excluded field enters
# the checksum. Complements test #9 (which proves excluded fields do NOT
# enter) by proving the other direction of INV-7.
# =========================================================================


def test_mutating_non_excluded_field_changes_evidence_checksum(
    tmp_path: pathlib.Path,
) -> None:
    """Every non-``build_timestamp`` field enters ``evidence_checksum``.

    Test #9 covers half of INV-7 (mutating the enumerated excluded field
    ``build_timestamp`` preserves the checksum). This test covers the
    other half: mutating any of a representative sample of non-excluded
    fields MUST change the checksum. The sample includes at least one
    field per manifest-, integrity-, and QA-derived surface so a future
    silent-skip regression on any of them fails loud.
    """
    result = _build_pipeline(tmp_path)
    package = _assemble_full_evidence(
        tmp_path,
        result,
        build_timestamp=datetime(2026, 3, 15, tzinfo=UTC),
    )
    baseline_checksum = compute_evidence_checksum(package)

    # distance_qa — QA-derived surface.
    mutated_distance_qa = dataclasses.replace(
        package.distance_qa,
        p50_normalized=package.distance_qa.p50_normalized + 0.1,
    )
    mutated_pkg = dataclasses.replace(package, distance_qa=mutated_distance_qa)
    assert compute_evidence_checksum(mutated_pkg) != baseline_checksum

    # hydrologic_core_fingerprint — G4 integrity surface.
    mutated_fingerprint = dataclasses.replace(
        package.hydrologic_core_fingerprint,
        hash="f" * 64,
    )
    mutated_pkg = dataclasses.replace(
        package, hydrologic_core_fingerprint=mutated_fingerprint
    )
    assert compute_evidence_checksum(mutated_pkg) != baseline_checksum

    # sp_att_asset_diff — manifest-adjacent .sp.att surface (the manifest
    # itself is not a top-level EvidencePackage field; its identity fields
    # flow through GridSnapshotReference + SpAttAssetDiff + BindingArtifact
    # cross-checks, and sp_att_asset_diff is the manifest-derived surface
    # most closely bound to the mapping asset).
    mutated_diff = dataclasses.replace(
        package.sp_att_asset_diff,
        new_sha256_hex="a" * 64,
    )
    mutated_pkg = dataclasses.replace(package, sp_att_asset_diff=mutated_diff)
    assert compute_evidence_checksum(mutated_pkg) != baseline_checksum

    # capacity_report — QA/limits surface.
    mutated_capacity = dataclasses.replace(
        package.capacity_report,
        station_count=package.capacity_report.station_count + 1,
    )
    mutated_pkg = dataclasses.replace(package, capacity_report=mutated_capacity)
    assert compute_evidence_checksum(mutated_pkg) != baseline_checksum


# =========================================================================
# 10. FORBIDDEN OUTPUT SCAN CLEAN ON GREEN PIPELINE
# =========================================================================


def test_forbidden_output_scan_clean_on_green_pipeline(
    tmp_path: pathlib.Path,
) -> None:
    """SUB-12 scan passes on the mapping-builder's clean artifact set.

    Since ``_make_forbidden_output_scan`` walks the actual variant subtree
    (see helper docstring), the scanned path count MUST equal the on-disk
    file count under ``variant_root``: 1 rewritten ``.sp.att`` + 1 mesh
    copy + 6 stub category files (river/lake/soil/geol/land/calibration)
    = 8 files. The empty ``input/`` directory is filtered out by
    ``is_file()``.
    """
    result = _build_pipeline(tmp_path)
    scan = _make_forbidden_output_scan(result)
    on_disk_files = sorted(
        (p for p in result["variant_root"].rglob("*") if p.is_file()),
        key=lambda p: str(p),
    )
    assert scan.passed is True
    assert scan.scanned_path_count == len(on_disk_files)
    assert scan.scanned_path_count == 8
    assert scan.offending_paths == ()
    assert scan.offending_db_writes == ()
    assert scan.cycle_lineage_records == ()


# =========================================================================
# 11. G2 GRID IDENTITY USES THE SHARED SIGNATURE HELPER
# =========================================================================


def test_g2_grid_identity_via_shared_signature_helper(
    tmp_path: pathlib.Path,
) -> None:
    """G2 gate uses the shared authority and fails closed on signature tamper.

    The positive round-trip recomputes the signature from the loaded
    snapshot cells via ``packages.common.grid_signature.grid_signature_hash``
    (the SOLE signature authority, cross-checked at ``make_snapshot``
    construction). The negative half then stages a tampered snapshot with
    a divergent signature and asserts the REAL G2 gate
    ``verify_grid_identity_precondition`` raises
    :class:`GridSignatureMismatchError` — proving the gate does not
    silently accept a diverged signature.
    """
    # Positive: shared helper on the snapshot cells reproduces the stored
    # signature. ``_build_pipeline`` already ran the gate inside
    # ``nearest_cell_barycenter_geodesic_v1`` (the fact the pipeline
    # succeeded is a positive-path proof).
    result = _build_pipeline(tmp_path / "positive")
    snapshot = result["snapshot"]
    snapshot_cells = result["snapshot_cells"]
    recomputed = grid_signature_module.grid_signature_hash(tuple(snapshot_cells))
    assert snapshot.grid_signature == recomputed

    # Negative: a tampered signature MUST fail the G2 gate closed. Stage a
    # fresh loader carrying a fabricated signature and invoke the public
    # gate directly.
    tampered_cells = make_regular_grid_cells(
        lon0=_GRID_LON0,
        lat0=_GRID_LAT0,
        lon_step=_GRID_STEP,
        lat_step=_GRID_STEP,
        lon_count=_GRID_LON_COUNT,
        lat_count=_GRID_LAT_COUNT,
    )
    tampered_snapshot = make_snapshot(
        source_id=_SOURCE_ID,
        grid_id=_GRID_ID,
        cells=tampered_cells,
        bbox_pad=0.5,
        grid_signature_override="deadbeef" * 8,
    )
    tampered_loader = InMemoryGridSnapshotLoader(
        source_id=_SOURCE_ID,
        grid_id=_GRID_ID,
        snapshot=tampered_snapshot,
        cells=tampered_cells,
    )
    with pytest.raises(GridSignatureMismatchError):
        verify_grid_identity_precondition(
            source_id=_SOURCE_ID,
            grid_id=_GRID_ID,
            barycenters_wgs84=[(1, 100.25, 36.15)],
            store=tampered_loader,
        )


# =========================================================================
# 12. EPIC #886 READINESS MANIFEST FLOWS INTO EVIDENCE
# =========================================================================


def test_epic_886_readiness_manifest_flows_into_evidence(
    tmp_path: pathlib.Path,
) -> None:
    """Epic #886 nested ``proj_crs_database_version`` projects into the evidence identity.

    Loads the real Epic #886
    ``openspec/changes/cmfd-direct-grid-platform-readiness/evidence/readiness-manifest.v1.json``
    verbatim (not a hand-crafted subset) so the cross-plane contract is
    exercised against the actual producer output. The full
    ``proj_db_metadata`` dict from the manifest carries ~15 fields
    (``EPSG.*``, ``ESRI.*``, ``IGNF.*``, ``NKG.*``, ``PROJ.VERSION``,
    ``PROJ_DATA.VERSION``, ``DATABASE.LAYOUT.VERSION.MAJOR/MINOR``); the
    projection reads only the four load-bearing fields and ignores the
    rest — this test proves the adapter tolerates the extra keys.

    The SUB-13 adapter :func:`project_readiness_proj_crs_database_version`
    projects the nested dict to a canonical str. The evidence package's
    ``MappingAlgorithmIdentity`` records the projected string;
    :func:`verify_algorithm_and_proj_identity_matches_readiness` accepts
    equal readiness content and returns None.
    """
    assert _EPIC_886_READINESS_MANIFEST.exists(), (
        f"Epic #886 readiness manifest missing at {_EPIC_886_READINESS_MANIFEST}; "
        "this test defends the cross-plane contract with the producer's real output"
    )
    manifest_dict = json.loads(_EPIC_886_READINESS_MANIFEST.read_text())
    proj_crs_block = manifest_dict["proj_crs_database_version"]

    # The full block carries the extra ~11 metadata keys the adapter must
    # tolerate; this assertion pins the surface area.
    assert set(proj_crs_block["proj_db_metadata"]) >= {
        "DATABASE.LAYOUT.VERSION.MAJOR",
        "DATABASE.LAYOUT.VERSION.MINOR",
        "PROJ_DATA.VERSION",
        "PROJ.VERSION",
        "EPSG.VERSION",
        "ESRI.VERSION",
        "IGNF.VERSION",
        "NKG.VERSION",
    }

    projected = project_readiness_proj_crs_database_version(proj_crs_block)
    # Canonical projection is exactly the docstring-declared format,
    # sourced from the real manifest's four load-bearing fields.
    expected = (
        f"proj:{proj_crs_block['proj_version']} "
        f"db-major:{proj_crs_block['proj_db_metadata']['DATABASE.LAYOUT.VERSION.MAJOR']} "
        f"db-minor:{proj_crs_block['proj_db_metadata']['DATABASE.LAYOUT.VERSION.MINOR']} "
        f"proj-data:{proj_crs_block['proj_db_metadata']['PROJ_DATA.VERSION']}"
    )
    assert projected == expected

    result = _build_pipeline(tmp_path)
    package = _assemble_full_evidence(
        tmp_path, result, proj_crs_database_version=projected
    )
    assert package.mapping_algorithm_identity.proj_crs_database_version == projected

    readiness = ReadinessManifest(
        algorithm_id=ALGORITHM_ID,
        proj_crs_database_version=projected,
        checksum="r" * 64,
    )
    # Gate returns None on pass — cross-plane identity round-trips cleanly
    # from producer manifest to consumer evidence.
    assert (
        verify_algorithm_and_proj_identity_matches_readiness(
            package, readiness_manifest=readiness
        )
        is None
    )


# =========================================================================
# EXTRA: algorithm_id is pinned to the versioned constant.
# =========================================================================


def test_algorithm_id_is_versioned_constant() -> None:
    """The mapping-algorithm identifier is pinned to the versioned constant.

    Two constants must agree: the module-level ``algorithm_id`` from
    :mod:`workers.mapping_builder.algorithm` (used at compute time) and the
    ``ALGORITHM_ID`` re-exported from :mod:`workers.mapping_builder.evidence`
    (used at evidence-recording time). Any drift is a versioning bug.
    """
    assert algorithm_id == "nearest_cell_barycenter_geodesic_v1"
    assert ALGORITHM_ID == algorithm_id


# =========================================================================
# EXTRA: ownership SVG carries the truncation marker at production scale.
# =========================================================================


def test_ownership_svg_carries_truncation_marker_at_production_scale(
    tmp_path: pathlib.Path,
) -> None:
    """The SUB-13 ownership SVG emits ``truncated n_rendered=X n_total=Y`` at 484 rows.

    The SVG canvas fits ~26 rows before the render loop breaks (SUB-13
    F-5 truncation discipline). At the keliya production scale (484
    elements), the vast majority of rows are clipped; the SUB-13 F-5
    marker is what turns a silent clip into a loud signal for downstream
    reviewers ("484 elements, only 26 rows visible"). Without this
    marker at 484 rows, a review pass on the SVG could silently miss
    99.5% of the ownership table.
    """
    result = _build_pipeline(tmp_path)
    package = _assemble_full_evidence(tmp_path, result)

    old_bytes = package.ownership_images.old_image_bytes
    new_bytes = package.ownership_images.new_image_bytes

    # The literal marker text MUST appear on both sides; a rendered_count
    # value is env-dependent (canvas height + font-metric arithmetic),
    # but the total n_total=484 is fixed by the fixture and asserted.
    assert b"truncated" in old_bytes, (
        "old-side ownership SVG missing truncation marker at 484-row scale"
    )
    assert b"n_total=484" in old_bytes
    assert b"truncated" in new_bytes, (
        "new-side ownership SVG missing truncation marker at 484-row scale"
    )
    assert b"n_total=484" in new_bytes
