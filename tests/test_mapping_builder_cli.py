"""Tests for :mod:`workers.mapping_builder.cli` (Epic #973 SUB-2 §2.1).

Coverage
--------

* ``end_to_end`` — full G0..G5 chain over the keliya fixture produces
  the variant tree with manifest + binding + evidence in-memory. G5's
  ``evidence_ref`` records the z-policy sampler rule id + verdict
  resolution provenance (SUB-1 wiring).
* ``stage_order`` — the exact library stages fire in G0..G5 order
  (monkeypatch each stage to a call tracker).
* ``no_variant_written`` — a G0 checksum mismatch and a G5 contract
  mismatch each fail closed with zero variant / binding / manifest
  artifacts (atomic-rename discipline: neither the finalized
  ``variant_root`` nor the ``<variant_root>.building`` tmp dir remains).
* Manifest round-trips through
  :func:`workers.forcing_producer.direct_grid_contract.load_forcing_mapping_contract_from_manifest`;
  G5 cross-consistency between the manifest's ``binding_checksum`` and
  the standalone binding artifact's bytes.
* ``no_duplicated_stage_logic`` — the CLI module imports each stage
  function from its owning library module and does NOT define a
  same-named local function (import-site guardrail against silent
  re-implementation).

Fixture routing
---------------
Uses the compact keliya fixture at
``tests/fixtures/mapping_builder/keliya/`` (484 elements / 32 stations /
8 used cells). The CLI's operator-argv path-authority resolver (SUB-3,
§2.2) is out of scope for §2.1 — every test drives the orchestration
function :func:`workers.mapping_builder.cli.build_direct_grid_variant`
directly with kwargs. The full keliya deterministic-byte-compare through
the resolver is SUB-5's territory.
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import re
import shutil
import struct

import pytest

from tests.fixtures.mapping_builder.in_memory_grid_snapshot import (
    InMemoryGridSnapshotLoader,
    make_regular_grid_cells,
    make_snapshot,
)
from workers.forcing_producer.direct_grid_contract import (
    DirectGridForcingContract,
    load_forcing_mapping_contract_from_manifest,
)
from workers.mapping_builder import cli as cli_module
from workers.mapping_builder.algorithm import GridSignatureMismatchError
from workers.mapping_builder.binding import (
    BindingArtifactError,
    ForbiddenOutputClass,
    ForbiddenRuntimeProducerArtifactError,
    ParserRoundTripError,
)
from workers.mapping_builder.cli import (
    DEFAULT_OBJECT_STORE_ROOT,
    BuildResult,
    PackagePathAuthorityError,
    ResolvedPackagePath,
    build_direct_grid_variant,
    resolve_package_path,
)
from workers.mapping_builder.evidence import (
    ALGORITHM_ID,
    Approvals,
    CapacityReport,
    DistanceQA,
    GridSnapshotReference,
    RollbackTarget,
)
from workers.mapping_builder.integrity import BaselineIntegrityError
from workers.mapping_builder.rewrite import (
    LEGACY_CYCLE_TSD_FORC_PATTERN,
    LEGACY_STATION_LONLAT_CSV_PATTERN,
    LEGACY_STATION_NUMBERED_CSV_PATTERN,
)
from workers.mapping_builder.z_policy_verdict import (
    EXPECTED_VERDICT_FILE_SHA256,
    SAMPLER_RULE_ID,
)

# --- fixture constants -----------------------------------------------------

_KELIYA_FIXTURE_DIR = (
    pathlib.Path(__file__).parent / "fixtures" / "mapping_builder" / "keliya"
)

_SOURCE_ID = "IFS"
_GRID_ID = "grid_test_v1"
_MAPPING_ASSET_IDENTITY = "mapping-v1-keliya"
_MODEL_INPUT_PACKAGE_ID = "pkg-v1-keliya"
_BINDING_URI = "s3://bucket/mapping-v1-keliya/binding.json"
_SP_ATT_MANIFEST_PATH = "keliya/keliya.sp.att"
_PROJ_CRS_DB_VERSION = "proj:9.5.1 db-major:1 db-minor:20 proj-data:1.19"

# Grid design matching integration test's keliya coverage.
_GRID_LON0 = 100.0
_GRID_LAT0 = 36.0
_GRID_STEP = 0.1
_GRID_LON_COUNT = 6
_GRID_LAT_COUNT = 6

# Category-file mapping matches integration test's stub layout.
_CATEGORY_FILES: dict[str, tuple[str, ...]] = {
    "mesh": ("keliya.sp.mesh",),
    "river": ("river/keliya.riv",),
    "lake": ("lake/keliya.lake",),
    "soil": ("gis/keliya.soil",),
    "geol": ("gis/keliya.geol",),
    "land": ("gis/keliya.land",),
    "calibration": ("cal/keliya.calib",),
}


# --- helpers ---------------------------------------------------------------


def _prepared_baseline(tmp_path: pathlib.Path) -> pathlib.Path:
    """Copy the keliya fixture and stub the seven §3.3 category files.

    Returns the baseline root under ``tmp_path``. The stubs are the same
    per-relative-path deterministic bytes the integration test writes on
    both sides — the CLI's baseline -> variant copytree then propagates
    them into the variant, keeping the §3.3 file-checksum equality gate
    honest without stubbing on both sides here.
    """
    baseline_root = tmp_path / "baseline"
    shutil.copytree(_KELIYA_FIXTURE_DIR, baseline_root)
    (baseline_root / "build.py").unlink(missing_ok=True)
    for rel_paths in _CATEGORY_FILES.values():
        for rel_path in rel_paths:
            target = baseline_root / rel_path
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"stub:{rel_path}\n".encode("utf-8"))
    return baseline_root


def _write_domain_shp(tmp_path: pathlib.Path) -> pathlib.Path:
    """Write a minimal ESRI-magic shapefile for ownership-image rendering."""
    domain_shp = tmp_path / "domain.shp"
    magic = struct.pack(">i", 9994)
    header_padding = b"\x00" * 96
    domain_shp.write_bytes(magic + header_padding)
    return domain_shp


def _snapshot_and_loader():
    """Return ``(snapshot, cells, loader, reference)`` for the keliya grid."""
    cells = make_regular_grid_cells(
        lon0=_GRID_LON0,
        lat0=_GRID_LAT0,
        lon_step=_GRID_STEP,
        lat_step=_GRID_STEP,
        lon_count=_GRID_LON_COUNT,
        lat_count=_GRID_LAT_COUNT,
    )
    snapshot = make_snapshot(
        source_id=_SOURCE_ID,
        grid_id=_GRID_ID,
        cells=cells,
        bbox_pad=0.5,
    )
    loader = InMemoryGridSnapshotLoader(
        source_id=_SOURCE_ID,
        grid_id=_GRID_ID,
        snapshot=snapshot,
        cells=cells,
    )
    reference = GridSnapshotReference(
        snapshot_id=str(snapshot.grid_snapshot_id),
        grid_signature=snapshot.grid_signature,
        snapshot_checksum=snapshot.grid_definition_checksum,
    )
    return snapshot, cells, loader, reference


def _canned_qa_and_capacity() -> tuple[DistanceQA, CapacityReport]:
    """Return literal DistanceQA + CapacityReport matching the integration harness."""
    distance_qa = DistanceQA(
        min_normalized=0.0,
        p50_normalized=0.3,
        p95_normalized=0.7,
        max_normalized=0.95,
        tie_count=0,
        coverage_edge_count=0,
    )
    capacity_report = CapacityReport(
        station_count=8,
        timestep_count=48,
        timeseries_row_count=48 * 8,
        file_size_bytes=1_048_576,
        station_count_limit=5000,
        timestep_count_limit=336,
        timeseries_row_count_limit=1_500_000,
        file_size_bytes_limit=100_000_000,
        before_station_count=32,
        after_station_count=8,
        station_reduction_ratio=32 / 8,
    )
    return distance_qa, capacity_report


def _run_build(
    tmp_path: pathlib.Path,
    *,
    variant_dirname: str = "variant",
    z_policy_verdict_path: pathlib.Path | None = None,
) -> BuildResult:
    """Drive the CLI orchestration over a fresh keliya baseline copy."""
    baseline_root = _prepared_baseline(tmp_path)
    variant_root = tmp_path / variant_dirname
    domain_shp = _write_domain_shp(tmp_path)
    _, snapshot_cells, loader, snapshot_reference = _snapshot_and_loader()
    distance_qa, capacity_report = _canned_qa_and_capacity()

    return build_direct_grid_variant(
        baseline_root=baseline_root,
        variant_root=variant_root,
        source_id=_SOURCE_ID,
        grid_id=_GRID_ID,
        grid_snapshot_loader=loader,
        snapshot_cells=snapshot_cells,
        grid_snapshot_reference=snapshot_reference,
        mapping_asset_identity=_MAPPING_ASSET_IDENTITY,
        model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
        binding_uri=_BINDING_URI,
        sp_att_manifest_path=_SP_ATT_MANIFEST_PATH,
        category_files=_CATEGORY_FILES,
        state_schema_bytes=b"state-schema-v1",
        solver_config_bytes=b"solver-config-v1",
        domain_shp_path=domain_shp,
        proj_crs_database_version=_PROJ_CRS_DB_VERSION,
        approvals=Approvals(
            builder_approver_id="tester@example.com",
            reviewer_approver_id="reviewer@example.com",
            small_basin_override_approver_id=None,
        ),
        rollback_target=RollbackTarget(
            previous_mapping_asset_checksum="",
            previous_mapping_asset_label="<initial>",
        ),
        distance_qa=distance_qa,
        capacity_report=capacity_report,
        z_policy_verdict_path=z_policy_verdict_path,
    )


# =========================================================================
# END-TO-END: full G0..G5 build over keliya
# =========================================================================


def test_end_to_end_g0_through_g5_produces_variant_tree(tmp_path: pathlib.Path) -> None:
    """Full CLI chain writes the variant tree; evidence records the z-policy provenance."""
    result = _run_build(tmp_path)

    # variant_root exists and is populated.
    assert result.variant_root.exists()
    assert result.variant_root.is_dir()
    assert (result.variant_root / "manifest.json").is_file()
    assert (result.variant_root / "direct_grid_binding.json").is_file()
    # No leftover .building sibling.
    tmp_variant = result.variant_root.with_name(result.variant_root.name + ".building")
    assert not tmp_variant.exists()

    # 8 used cells => 8 station bindings.
    assert len(result.manifest.station_bindings) == 8
    assert len(result.binding_artifact.station_bindings) == 8

    # Ownership evidence covers every keliya element (484 rows).
    assert len(result.evidence_package.ownership_table) == 484

    # Grid snapshot reference is threaded through unchanged.
    _, _snapshot_cells, _loader, snapshot_reference = _snapshot_and_loader()
    assert (
        result.evidence_package.grid_snapshot_reference.grid_signature
        == snapshot_reference.grid_signature
    )

    # Per-gate evidence_ref shape assertions (kind + key presence).
    gate_results = result.evidence_package.gate_results
    g0_ref = gate_results.g0.evidence_ref
    assert g0_ref["kind"] == "baseline_integrity_report"
    assert "checksum" in g0_ref
    g1_ref = gate_results.g1.evidence_ref
    assert g1_ref["kind"] == "g1_non_degenerate"
    assert g1_ref["element_count"] == 484
    g2_ref = gate_results.g2.evidence_ref
    assert g2_ref["kind"] == "ownership_algorithm"
    assert g2_ref["algorithm"] == ALGORITHM_ID
    g3_ref = gate_results.g3.evidence_ref
    assert g3_ref["kind"] == "hydrologic_core_fingerprint_equal"
    assert "fingerprint_hash" in g3_ref
    g4_ref = gate_results.g4.evidence_ref
    assert g4_ref["kind"] == "sp_att_rewrite"
    assert g4_ref["rewritten_row_count"] == 484
    assert g4_ref["used_cell_count"] == 8

    # G5 evidence_ref carries the z-policy provenance.
    g5_ref = gate_results.g5.evidence_ref
    assert g5_ref["kind"] == "binding_g5"
    assert g5_ref["binding_checksum"] == result.binding_artifact.checksum
    assert g5_ref["sampler_rule_id"] == SAMPLER_RULE_ID
    assert g5_ref["verdict_resolution"]["verified_sha256"] == EXPECTED_VERDICT_FILE_SHA256
    assert g5_ref["verdict_resolution"]["override_used"] is False
    assert g5_ref["verdict_resolution"]["resolved_path"].endswith(
        "z-policy-solver-audit-verdict.md"
    )

    # Deterministic evidence: build_timestamp unset per §2.4.
    assert result.evidence_package.build_timestamp is None


# =========================================================================
# STAGE ORDER: exact G0..G5 sequence
# =========================================================================


def test_stage_order_matches_g0_through_g5(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatch every library stage import to a tracker; assert G0..G5 order.

    Covers all 19 stage functions asserted by
    :func:`test_no_duplicated_stage_logic_all_stage_functions_come_from_stage_modules`
    so adjacent-swap regressions (e.g. reordering
    ``derive_used_cell_subset`` and ``assign_shud_forcing_index``, or the
    SUB-1 ``resolve_verdict -> build_z_policy -> sample_per_cell_z``
    contract) surface as an assertion failure here rather than silently.
    """
    calls: list[str] = []
    tracked_names = [
        "verify_g0_baseline",
        "verify_g1_non_degenerate_triangles",
        "verify_package_crs",
        "nearest_cell_barycenter_geodesic_v1",
        "derive_used_cell_subset",
        "assign_shud_forcing_index",
        "verify_small_basin_gate",
        "resolve_verdict",
        "build_z_policy",
        "sample_per_cell_z",
        "copy_and_rewrite_sp_att_forc",
        "verify_non_forc_columns_unchanged",
        "verify_non_sp_att_checksums_equal",
        "verify_hydrologic_core_fingerprint_equal",
        "verify_no_legacy_weather_path_in_active_tree",
        "emit_direct_grid_manifest_and_binding",
        "verify_no_forbidden_runtime_producer_artifacts",
        "render_ownership_images",
        "assemble_evidence_package",
    ]
    for name in tracked_names:
        real = getattr(cli_module, name)

        def _tracker(*args, _name=name, _real=real, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(cli_module, name, _tracker)

    _run_build(tmp_path)

    assert calls == tracked_names, (
        f"stages did not fire in G0..G5 order; got {calls!r}"
    )


# =========================================================================
# FAIL-CLOSED: G0 checksum mismatch leaves no variant
# =========================================================================


def test_g0_checksum_mismatch_fails_closed_no_variant_written(
    tmp_path: pathlib.Path,
) -> None:
    """A baseline .sp.att with a bogus element ID triggers G0; no variant emitted."""
    baseline_root = _prepared_baseline(tmp_path)
    # Corrupt one row of .sp.att so its element-id set diverges from the
    # mesh — triggers UnequalElementIdSetError under G0.
    sp_att = baseline_root / "keliya.sp.att"
    lines = sp_att.read_text().splitlines()
    # Rewrite element_id 1 to 9999 (outside the mesh element-id range).
    corrupted = lines[:]
    tokens = corrupted[2].split()
    tokens[0] = "9999"
    corrupted[2] = "\t".join(tokens)
    sp_att.write_text("\n".join(corrupted) + "\n")

    # Snapshot the corrupted baseline tree so we can verify the failed
    # build did not mutate the baseline in place.
    pre_baseline_files = {
        p.relative_to(baseline_root)
        for p in baseline_root.rglob("*")
        if p.is_file()
    }
    variant_root = tmp_path / "variant"
    domain_shp = _write_domain_shp(tmp_path)
    _, snapshot_cells, loader, snapshot_reference = _snapshot_and_loader()
    distance_qa, capacity_report = _canned_qa_and_capacity()

    with pytest.raises(BaselineIntegrityError):
        build_direct_grid_variant(
            baseline_root=baseline_root,
            variant_root=variant_root,
            source_id=_SOURCE_ID,
            grid_id=_GRID_ID,
            grid_snapshot_loader=loader,
            snapshot_cells=snapshot_cells,
            grid_snapshot_reference=snapshot_reference,
            mapping_asset_identity=_MAPPING_ASSET_IDENTITY,
            model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
            binding_uri=_BINDING_URI,
            sp_att_manifest_path=_SP_ATT_MANIFEST_PATH,
            category_files=_CATEGORY_FILES,
            state_schema_bytes=b"state-schema-v1",
            solver_config_bytes=b"solver-config-v1",
            domain_shp_path=domain_shp,
            proj_crs_database_version=_PROJ_CRS_DB_VERSION,
            approvals=Approvals(
                builder_approver_id="tester@example.com",
                reviewer_approver_id="reviewer@example.com",
                small_basin_override_approver_id=None,
            ),
            rollback_target=RollbackTarget(
                previous_mapping_asset_checksum="",
                previous_mapping_asset_label="<initial>",
            ),
            distance_qa=distance_qa,
            capacity_report=capacity_report,
        )

    # No-partial-output: final variant absent + tmp .building absent.
    assert not variant_root.exists()
    assert not variant_root.with_name(variant_root.name + ".building").exists()
    # And the baseline tree is unchanged (byte-for-byte equal file set).
    post_baseline_files = {
        p.relative_to(baseline_root)
        for p in baseline_root.rglob("*")
        if p.is_file()
    }
    assert post_baseline_files == pre_baseline_files, (
        "baseline tree was mutated by a failed build"
    )


# =========================================================================
# FAIL-CLOSED: G5 contract mismatch leaves no variant
# =========================================================================


def test_g5_emitter_raise_triggers_cleanup_and_no_variant(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scaffold-cleanup proof: any emitter raise -> no variant / no ``.building``.

    Monkeypatch-driven — proves that when the G5 emitter raises
    :class:`BindingArtifactError` (here a :class:`ParserRoundTripError`
    subclass, the canonical contract-mismatch shape), the orchestrator's
    try/except cleanup runs and no partial artifact remains. The
    companion :func:`test_g5_contract_mismatch_fails_closed_data_driven`
    proves the same fail-closed contract via a real gate mismatch with no
    injected raise.
    """
    def _boom(**_kwargs):
        raise ParserRoundTripError(
            parser_error_message=(
                "simulated G5 contract mismatch — parser could not round-trip the manifest"
            ),
        )

    monkeypatch.setattr(cli_module, "emit_direct_grid_manifest_and_binding", _boom)

    baseline_root = _prepared_baseline(tmp_path)
    pre_baseline_files = {
        p.relative_to(baseline_root)
        for p in baseline_root.rglob("*")
        if p.is_file()
    }
    variant_root = tmp_path / "variant"
    domain_shp = _write_domain_shp(tmp_path)
    _, snapshot_cells, loader, snapshot_reference = _snapshot_and_loader()
    distance_qa, capacity_report = _canned_qa_and_capacity()

    with pytest.raises(BindingArtifactError):
        build_direct_grid_variant(
            baseline_root=baseline_root,
            variant_root=variant_root,
            source_id=_SOURCE_ID,
            grid_id=_GRID_ID,
            grid_snapshot_loader=loader,
            snapshot_cells=snapshot_cells,
            grid_snapshot_reference=snapshot_reference,
            mapping_asset_identity=_MAPPING_ASSET_IDENTITY,
            model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
            binding_uri=_BINDING_URI,
            sp_att_manifest_path=_SP_ATT_MANIFEST_PATH,
            category_files=_CATEGORY_FILES,
            state_schema_bytes=b"state-schema-v1",
            solver_config_bytes=b"solver-config-v1",
            domain_shp_path=domain_shp,
            proj_crs_database_version=_PROJ_CRS_DB_VERSION,
            approvals=Approvals(
                builder_approver_id="tester@example.com",
                reviewer_approver_id="reviewer@example.com",
                small_basin_override_approver_id=None,
            ),
            rollback_target=RollbackTarget(
                previous_mapping_asset_checksum="",
                previous_mapping_asset_label="<initial>",
            ),
            distance_qa=distance_qa,
            capacity_report=capacity_report,
        )

    assert not variant_root.exists()
    assert not variant_root.with_name(variant_root.name + ".building").exists()
    post_baseline_files = {
        p.relative_to(baseline_root)
        for p in baseline_root.rglob("*")
        if p.is_file()
    }
    assert post_baseline_files == pre_baseline_files, (
        "baseline tree was mutated by a failed build"
    )


def test_g5_contract_mismatch_fails_closed_data_driven(
    tmp_path: pathlib.Path,
) -> None:
    """Real-gate proof: signature mismatch surfaces from library code.

    The G2 gate's :func:`verify_grid_identity_precondition` recomputes
    the ``grid_signature`` from the loaded cells and compares it against
    the stored snapshot value. Wiring the snapshot with a
    ``grid_signature_override`` that DIFFERS from what
    :func:`grid_signature_hash` produces triggers
    :class:`GridSignatureMismatchError` from real library code — no
    monkeypatch, no injected raise. Task §2.1 frames this under "G5
    contract mismatch"; the ACTUAL fail-closed invariant is "any gate
    mismatch -> no partial output", proven here at the earliest gate
    that catches signature drift so no artifact ever reaches disk.
    """
    baseline_root = _prepared_baseline(tmp_path)
    pre_baseline_files = {
        p.relative_to(baseline_root)
        for p in baseline_root.rglob("*")
        if p.is_file()
    }
    variant_root = tmp_path / "variant"
    domain_shp = _write_domain_shp(tmp_path)
    distance_qa, capacity_report = _canned_qa_and_capacity()

    # Build cells + snapshot with a deliberately-wrong grid_signature.
    cells = make_regular_grid_cells(
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
        cells=cells,
        bbox_pad=0.5,
        grid_signature_override="deadbeef" * 8,  # 64-char hex, invalid recompute
    )
    tampered_loader = InMemoryGridSnapshotLoader(
        source_id=_SOURCE_ID,
        grid_id=_GRID_ID,
        snapshot=tampered_snapshot,
        cells=cells,
    )
    tampered_reference = GridSnapshotReference(
        snapshot_id=str(tampered_snapshot.grid_snapshot_id),
        grid_signature=tampered_snapshot.grid_signature,
        snapshot_checksum=tampered_snapshot.grid_definition_checksum,
    )

    with pytest.raises(GridSignatureMismatchError):
        build_direct_grid_variant(
            baseline_root=baseline_root,
            variant_root=variant_root,
            source_id=_SOURCE_ID,
            grid_id=_GRID_ID,
            grid_snapshot_loader=tampered_loader,
            snapshot_cells=cells,
            grid_snapshot_reference=tampered_reference,
            mapping_asset_identity=_MAPPING_ASSET_IDENTITY,
            model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
            binding_uri=_BINDING_URI,
            sp_att_manifest_path=_SP_ATT_MANIFEST_PATH,
            category_files=_CATEGORY_FILES,
            state_schema_bytes=b"state-schema-v1",
            solver_config_bytes=b"solver-config-v1",
            domain_shp_path=domain_shp,
            proj_crs_database_version=_PROJ_CRS_DB_VERSION,
            approvals=Approvals(
                builder_approver_id="tester@example.com",
                reviewer_approver_id="reviewer@example.com",
                small_basin_override_approver_id=None,
            ),
            rollback_target=RollbackTarget(
                previous_mapping_asset_checksum="",
                previous_mapping_asset_label="<initial>",
            ),
            distance_qa=distance_qa,
            capacity_report=capacity_report,
        )

    assert not variant_root.exists()
    assert not variant_root.with_name(variant_root.name + ".building").exists()
    post_baseline_files = {
        p.relative_to(baseline_root)
        for p in baseline_root.rglob("*")
        if p.is_file()
    }
    assert post_baseline_files == pre_baseline_files, (
        "baseline tree was mutated by a failed build"
    )


# =========================================================================
# G5 CROSS-CONSISTENCY: manifest.binding_checksum matches SHA-256(binding.json bytes)
# =========================================================================


def test_g5_cross_consistency_binding_checksum_equals_sha256_of_binding_bytes(
    tmp_path: pathlib.Path,
) -> None:
    """Standalone binding artifact bytes hash to the manifest's binding_checksum.

    Cross-checks the ON-DISK bytes (not the in-memory dataclass) so a
    regression in the file-write path — e.g. wrong serializer or a
    truncated write — surfaces here instead of hiding behind the shared
    in-memory objects.
    """
    result = _run_build(tmp_path)

    binding_bytes = (result.variant_root / "direct_grid_binding.json").read_bytes()
    assert hashlib.sha256(binding_bytes).hexdigest() == result.manifest.binding_checksum
    # And the in-memory artifact bytes match the on-disk bytes.
    assert result.binding_artifact.bytes == binding_bytes

    # Parse both artifacts back from disk and cross-check station rows so
    # a persistence-only regression (empty write, missing field, wrong
    # serializer) fails here rather than passing under a library-level
    # invariant check. The manifest is nested under the outer
    # ``resource_profile.direct_grid_forcing`` section per docs §7.1.
    manifest_disk = json.loads(
        (result.variant_root / "manifest.json").read_bytes()
    )
    manifest_section = manifest_disk["direct_grid_forcing"]
    binding_disk = json.loads(binding_bytes)
    manifest_rows_disk = {
        b["station_id"]: (b["grid_cell_id"], b["shud_forcing_index"])
        for b in manifest_section["station_bindings"]
    }
    binding_rows_disk = {
        b["station_id"]: (b["grid_cell_id"], b["shud_forcing_index"])
        for b in binding_disk["station_bindings"]
    }
    assert manifest_rows_disk == binding_rows_disk


# =========================================================================
# MANIFEST PARSES THROUGH DIRECT-GRID CONTRACT
# =========================================================================


def test_emitted_manifest_round_trips_through_direct_grid_contract_parser(
    tmp_path: pathlib.Path,
) -> None:
    """The emitted manifest.json parses cleanly through the contract entrypoint.

    Reads the manifest bytes back from disk (rather than passing the
    in-memory :class:`DirectGridManifest`) so a regression in the file
    persistence path — stale dict, empty write, wrong serializer — fails
    here rather than passing on the pre-serialization dataclass.
    """
    result = _run_build(tmp_path)

    manifest_path = result.variant_root / "manifest.json"
    profile = json.loads(manifest_path.read_bytes())
    contract = load_forcing_mapping_contract_from_manifest(profile, source_id=_SOURCE_ID)

    assert isinstance(contract, DirectGridForcingContract)
    assert contract.forcing_mapping_mode == "direct_grid"
    assert contract.grid_id == _GRID_ID
    assert contract.binding_checksum == result.manifest.binding_checksum
    assert len(contract.stations) == len(result.manifest.station_bindings)


# =========================================================================
# EVIDENCE: G5 evidence_ref records sampler_rule_id + verdict resolution
# =========================================================================


def test_evidence_g5_records_sampler_rule_id_and_verdict_resolution(
    tmp_path: pathlib.Path,
) -> None:
    """G5 evidence_ref pins the SUB-1 sampler rule id + verdict SHA-256."""
    result = _run_build(tmp_path)

    g5_ref = result.evidence_package.gate_results.g5.evidence_ref
    assert g5_ref["sampler_rule_id"] == SAMPLER_RULE_ID
    assert g5_ref["verdict_resolution"]["verified_sha256"] == EXPECTED_VERDICT_FILE_SHA256
    # The resolved path is absolute and points at the committed evidence file.
    resolved = pathlib.Path(g5_ref["verdict_resolution"]["resolved_path"])
    assert resolved.is_absolute()
    assert resolved.name == "z-policy-solver-audit-verdict.md"


# =========================================================================
# REGRESSION: FORC column is header-located, not hardcoded to index 4
# =========================================================================


def test_ownership_row_old_forc_reads_forc_column_not_hardcoded_index(
    tmp_path: pathlib.Path,
) -> None:
    """OwnershipRow.old_forc is read via the FORC header token, not tokens[4].

    Regression proof for the previous ``_read_baseline_forc_by_element``
    behavior that hardcoded ``int(tokens[4])`` as the FORC column. A
    legal ``.sp.att`` with FORC permuted to a non-canonical column index
    would silently record the SOIL / GEOL / LC value at index 4 as
    ``old_forc`` on every :class:`OwnershipRow`. Here we swap the
    ``SOIL`` and ``FORC`` columns (moving FORC to index 1) and assert
    the evidence bundle records each element's real FORC value.
    """
    baseline_root = _prepared_baseline(tmp_path)
    sp_att = baseline_root / "keliya.sp.att"
    lines = sp_att.read_text().splitlines()
    # Original header: INDEX SOIL GEOL LC FORC MF BC SS LAKE (FORC at 4).
    # Permuted header: INDEX FORC GEOL LC SOIL MF BC SS LAKE (FORC at 1).
    orig_header = lines[1].split()
    assert orig_header[1] == "SOIL" and orig_header[4] == "FORC", (
        "keliya fixture header changed; regression permutation needs "
        f"SOIL@1 + FORC@4 as its swap axis (got {orig_header!r})"
    )
    permuted_header = list(orig_header)
    permuted_header[1], permuted_header[4] = permuted_header[4], permuted_header[1]
    lines[1] = "\t".join(permuted_header)
    # Record the original values so we can assert the evidence bundle
    # picked up the real FORC (index 1 after swap) rather than SOIL
    # (index 4 after swap).
    forc_by_element: dict[int, int] = {}
    soil_by_element: dict[int, int] = {}
    for i in range(2, len(lines)):
        tokens = lines[i].split()
        if not tokens:
            continue
        element_id = int(tokens[0])
        soil_by_element[element_id] = int(tokens[1])
        forc_by_element[element_id] = int(tokens[4])
        tokens[1], tokens[4] = tokens[4], tokens[1]
        lines[i] = "\t".join(tokens)
    sp_att.write_text("\n".join(lines) + "\n")

    variant_root = tmp_path / "variant"
    domain_shp = _write_domain_shp(tmp_path)
    _, snapshot_cells, loader, snapshot_reference = _snapshot_and_loader()
    distance_qa, capacity_report = _canned_qa_and_capacity()

    result = build_direct_grid_variant(
        baseline_root=baseline_root,
        variant_root=variant_root,
        source_id=_SOURCE_ID,
        grid_id=_GRID_ID,
        grid_snapshot_loader=loader,
        snapshot_cells=snapshot_cells,
        grid_snapshot_reference=snapshot_reference,
        mapping_asset_identity=_MAPPING_ASSET_IDENTITY,
        model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
        binding_uri=_BINDING_URI,
        sp_att_manifest_path=_SP_ATT_MANIFEST_PATH,
        category_files=_CATEGORY_FILES,
        state_schema_bytes=b"state-schema-v1",
        solver_config_bytes=b"solver-config-v1",
        domain_shp_path=domain_shp,
        proj_crs_database_version=_PROJ_CRS_DB_VERSION,
        approvals=Approvals(
            builder_approver_id="tester@example.com",
            reviewer_approver_id="reviewer@example.com",
            small_basin_override_approver_id=None,
        ),
        rollback_target=RollbackTarget(
            previous_mapping_asset_checksum="",
            previous_mapping_asset_label="<initial>",
        ),
        distance_qa=distance_qa,
        capacity_report=capacity_report,
    )

    # Only elements where FORC != SOIL after swap distinguish the two
    # code paths (a hardcoded-index reader returns SOIL; a header-located
    # reader returns FORC). Assert at least one such element exists so
    # this regression test carries a meaningful signal.
    distinguishing_elements = [
        eid for eid, forc in forc_by_element.items()
        if forc != soil_by_element[eid]
    ]
    assert distinguishing_elements, (
        "keliya fixture has no row where FORC != SOIL after swap; the "
        "regression test cannot distinguish header-located FORC from a "
        "hardcoded tokens[4] fallback"
    )

    ownership_by_element = {
        row.element_id: row
        for row in result.evidence_package.ownership_table
    }
    for element_id in distinguishing_elements:
        expected_forc = forc_by_element[element_id]
        row = ownership_by_element[element_id]
        assert row.old_forc == str(expected_forc), (
            f"element {element_id}: expected old_forc={expected_forc!s}, "
            f"got old_forc={row.old_forc!r}; the CLI is reading the wrong "
            "column (hardcoded index rather than header-located FORC)"
        )


# =========================================================================
# REGRESSION: Elevation column is header-located, not hardcoded to index 4
# =========================================================================


def test_parse_mesh_nodes_permuted_column_order_correctly_reads_elevation(
    tmp_path: pathlib.Path,
) -> None:
    """_parse_mesh_nodes reads Elevation via the header token, not tokens[4].

    The canonical keliya node header is ``ID X Y AqDepth Elevation`` (so
    ``Elevation`` sits at index 4). A permuted-but-legal header
    ``ID X Y Elevation AqDepth`` places Elevation at index 3 — a
    hardcoded ``tokens[4]`` reader would silently read ``AqDepth`` as
    the elevation and feed it into the z-policy sampler. Here we permute
    the columns AND stamp distinct sentinel values so a wrong-column
    read is visible in ``result.manifest.z_policy.per_cell_z``.
    """
    baseline_root = _prepared_baseline(tmp_path)
    sp_mesh = baseline_root / "keliya.sp.mesh"
    text_lines = sp_mesh.read_text().splitlines()
    # Element count from line 0 to locate the node block.
    n_elements = int(text_lines[0].split()[0])
    node_header_line = 1 + 1 + n_elements  # element count + element cols + rows
    n_nodes = int(text_lines[node_header_line].split()[0])
    node_col_header_line = node_header_line + 1
    orig_node_header = text_lines[node_col_header_line].split()
    assert orig_node_header == ["ID", "X", "Y", "AqDepth", "Elevation"], (
        "keliya .sp.mesh node header changed; regression permutation "
        f"expects canonical ID/X/Y/AqDepth/Elevation (got {orig_node_header!r})"
    )
    # Permute to ID X Y Elevation AqDepth (Elevation now at index 3).
    permuted_header = ["ID", "X", "Y", "Elevation", "AqDepth"]
    text_lines[node_col_header_line] = "\t".join(permuted_header)
    # Stamp sentinel values across every node row: Elevation = 777.0,
    # AqDepth = 111.0 — distinct enough that a wrong-column read
    # (AqDepth as Elevation) surfaces as a per_cell_z value of 111.0.
    node_data_start = node_col_header_line + 1
    for i in range(node_data_start, node_data_start + n_nodes):
        tokens = text_lines[i].split()
        # Rewrite: ID X Y Elevation(777) AqDepth(111).
        text_lines[i] = "\t".join([tokens[0], tokens[1], tokens[2], "777.0", "111.0"])
    sp_mesh.write_text("\n".join(text_lines) + "\n")

    variant_root = tmp_path / "variant"
    domain_shp = _write_domain_shp(tmp_path)
    _, snapshot_cells, loader, snapshot_reference = _snapshot_and_loader()
    distance_qa, capacity_report = _canned_qa_and_capacity()

    result = build_direct_grid_variant(
        baseline_root=baseline_root,
        variant_root=variant_root,
        source_id=_SOURCE_ID,
        grid_id=_GRID_ID,
        grid_snapshot_loader=loader,
        snapshot_cells=snapshot_cells,
        grid_snapshot_reference=snapshot_reference,
        mapping_asset_identity=_MAPPING_ASSET_IDENTITY,
        model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
        binding_uri=_BINDING_URI,
        sp_att_manifest_path=_SP_ATT_MANIFEST_PATH,
        category_files=_CATEGORY_FILES,
        state_schema_bytes=b"state-schema-v1",
        solver_config_bytes=b"solver-config-v1",
        domain_shp_path=domain_shp,
        proj_crs_database_version=_PROJ_CRS_DB_VERSION,
        approvals=Approvals(
            builder_approver_id="tester@example.com",
            reviewer_approver_id="reviewer@example.com",
            small_basin_override_approver_id=None,
        ),
        rollback_target=RollbackTarget(
            previous_mapping_asset_checksum="",
            previous_mapping_asset_label="<initial>",
        ),
        distance_qa=distance_qa,
        capacity_report=capacity_report,
    )

    # Every station's z value is sampled from _parse_mesh_nodes elevations
    # via the SUB-1 nearest_mesh_node_elevation_v1 sampler. With uniform
    # 777.0 sentinels on every mesh node, every station.z MUST equal 777.0.
    # A hardcoded tokens[4] elevation read would surface 111.0 here after
    # column permutation.
    station_bindings = result.manifest.station_bindings
    assert station_bindings, "expected non-empty station_bindings"
    for station in station_bindings:
        assert station.z == pytest.approx(777.0), (
            f"station {station.station_id}: expected z=777.0 (Elevation "
            f"sentinel), got z={station.z!r}; _parse_mesh_nodes is reading "
            "the wrong column (hardcoded index rather than header-located "
            "Elevation)"
        )


# =========================================================================
# REGRESSION: mid-copytree failure leaves no ``.building`` residue
# =========================================================================


def test_copytree_failure_mid_copy_leaves_no_building_residue(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``shutil.copytree`` mid-copy failure MUST leave no partial ``.building``.

    Previously ``shutil.copytree`` ran outside the try/except so a mid-
    copy failure (disk full / EIO / KeyboardInterrupt) left the partial
    ``<variant_root>.building`` directory on disk, violating the
    docstring's atomic-rename promise. This regression stubs
    ``shutil.copytree`` to create a partial tmp dir and then raise, and
    asserts the wrapper's cleanup runs.
    """
    # Prepare the baseline + snapshot BEFORE monkeypatching (both use
    # shutil.copytree internally for fixture setup); the fake copytree
    # only replaces the CLI's own call inside build_direct_grid_variant.
    baseline_root = _prepared_baseline(tmp_path)
    variant_root = tmp_path / "variant"
    domain_shp = _write_domain_shp(tmp_path)
    _, snapshot_cells, loader, snapshot_reference = _snapshot_and_loader()
    distance_qa, capacity_report = _canned_qa_and_capacity()

    def _fake_copytree(src, dst, *_args, **_kwargs):
        dst_path = pathlib.Path(dst)
        dst_path.mkdir(parents=True)
        (dst_path / "partial.dat").write_bytes(b"stale mid-copy residue")
        raise OSError("simulated disk full during copytree")

    monkeypatch.setattr(cli_module.shutil, "copytree", _fake_copytree)

    with pytest.raises(OSError, match="simulated disk full"):
        build_direct_grid_variant(
            baseline_root=baseline_root,
            variant_root=variant_root,
            source_id=_SOURCE_ID,
            grid_id=_GRID_ID,
            grid_snapshot_loader=loader,
            snapshot_cells=snapshot_cells,
            grid_snapshot_reference=snapshot_reference,
            mapping_asset_identity=_MAPPING_ASSET_IDENTITY,
            model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
            binding_uri=_BINDING_URI,
            sp_att_manifest_path=_SP_ATT_MANIFEST_PATH,
            category_files=_CATEGORY_FILES,
            state_schema_bytes=b"state-schema-v1",
            solver_config_bytes=b"solver-config-v1",
            domain_shp_path=domain_shp,
            proj_crs_database_version=_PROJ_CRS_DB_VERSION,
            approvals=Approvals(
                builder_approver_id="tester@example.com",
                reviewer_approver_id="reviewer@example.com",
                small_basin_override_approver_id=None,
            ),
            rollback_target=RollbackTarget(
                previous_mapping_asset_checksum="",
                previous_mapping_asset_label="<initial>",
            ),
            distance_qa=distance_qa,
            capacity_report=capacity_report,
        )

    assert not variant_root.exists()
    assert not variant_root.with_name(variant_root.name + ".building").exists()


# =========================================================================
# GUARD: build_direct_grid_variant rejects a pre-existing variant_root
# =========================================================================


def test_build_direct_grid_variant_rejects_pre_existing_variant_root(
    tmp_path: pathlib.Path,
) -> None:
    """A pre-existing ``variant_root`` fails fast with ``ValueError``.

    The atomic-rename discipline requires that the caller supply a
    fresh, non-existent path. A collision must fail closed BEFORE any
    file is copied so the caller's existing tree is not mutated.
    """
    baseline_root = _prepared_baseline(tmp_path)
    variant_root = tmp_path / "variant"
    variant_root.mkdir()
    sentinel = variant_root / "sentinel.txt"
    sentinel.write_bytes(b"user-owned content")

    domain_shp = _write_domain_shp(tmp_path)
    _, snapshot_cells, loader, snapshot_reference = _snapshot_and_loader()
    distance_qa, capacity_report = _canned_qa_and_capacity()

    with pytest.raises(ValueError, match="already exists"):
        build_direct_grid_variant(
            baseline_root=baseline_root,
            variant_root=variant_root,
            source_id=_SOURCE_ID,
            grid_id=_GRID_ID,
            grid_snapshot_loader=loader,
            snapshot_cells=snapshot_cells,
            grid_snapshot_reference=snapshot_reference,
            mapping_asset_identity=_MAPPING_ASSET_IDENTITY,
            model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
            binding_uri=_BINDING_URI,
            sp_att_manifest_path=_SP_ATT_MANIFEST_PATH,
            category_files=_CATEGORY_FILES,
            state_schema_bytes=b"state-schema-v1",
            solver_config_bytes=b"solver-config-v1",
            domain_shp_path=domain_shp,
            proj_crs_database_version=_PROJ_CRS_DB_VERSION,
            approvals=Approvals(
                builder_approver_id="tester@example.com",
                reviewer_approver_id="reviewer@example.com",
                small_basin_override_approver_id=None,
            ),
            rollback_target=RollbackTarget(
                previous_mapping_asset_checksum="",
                previous_mapping_asset_label="<initial>",
            ),
            distance_qa=distance_qa,
            capacity_report=capacity_report,
        )

    # Sentinel file MUST still be intact — nothing was mutated.
    assert sentinel.is_file()
    assert sentinel.read_bytes() == b"user-owned content"


# =========================================================================
# §2.2 INPUT-AUTHORITY RESOLVER (SUB-3)
# =========================================================================
# Every test below is either matched by
#   pytest -k "input_authority or dev_workspace or object_store_root"
# or exercises the argparse main whose behavior is the SUB-3 evidence
# floor. The resolver is pure path-string logic — no filesystem read on
# any test path — and the sanctioned test channel for landing tmp
# object-store fixtures is ``--object-store-root=<tmp>``.


def test_input_authority_object_store_shape_default_root_accepted() -> None:
    """A path shaped under the default root is accepted; override flag is False."""
    package_path = (
        DEFAULT_OBJECT_STORE_ROOT
        / "models"
        / "basins_keliya_shud"
        / "rel_v1"
        / "package"
    )

    resolved = resolve_package_path(package_path=package_path)

    assert isinstance(resolved, ResolvedPackagePath)
    assert resolved.baseline_root == package_path
    assert resolved.basin_name == "keliya"
    assert resolved.release_id == "rel_v1"

    evidence = resolved.input_authority_evidence
    assert evidence["kind"] == "input_authority"
    assert evidence["package_path"] == str(package_path)
    assert evidence["basin_name"] == "keliya"
    assert evidence["release_id"] == "rel_v1"
    assert evidence["channel"] == "object_store"
    assert evidence["object_store_root"] == str(DEFAULT_OBJECT_STORE_ROOT)
    assert evidence["object_store_root_override"] is False
    assert evidence["dev_workspace_override"] is None


def test_input_authority_object_store_root_override_accepted_and_recorded(
    tmp_path: pathlib.Path,
) -> None:
    """--object-store-root override is honored and recorded on the evidence."""
    staged_root = tmp_path / "staged"
    package_path = (
        staged_root
        / "models"
        / "basins_keliya_shud"
        / "rel_v1"
        / "package"
    )

    resolved = resolve_package_path(
        package_path=package_path,
        object_store_root=staged_root,
    )

    evidence = resolved.input_authority_evidence
    assert evidence["object_store_root_override"] is True
    assert evidence["object_store_root"] == str(staged_root)
    assert evidence["channel"] == "object_store"
    assert evidence["basin_name"] == "keliya"
    assert evidence["release_id"] == "rel_v1"
    assert evidence["dev_workspace_override"] is None


# Both dev-workspace prefixes declared by
# :data:`workers.mapping_builder.cli._DEV_WORKSPACE_PREFIXES`. Parametrized
# fold (Phase 6): every dev-workspace test below runs twice — once per
# prefix — so a silent typo or removal of the node-22 (``/volume/nwm/Basins``)
# entry cannot pass while the node-27 (``/home/ghdc/nwm/Basins``) entry
# alone still works.
_DEV_WORKSPACE_PREFIX_PARAMS = [
    pathlib.Path("/home/ghdc/nwm/Basins"),
    pathlib.Path("/volume/nwm/Basins"),
]


@pytest.mark.parametrize("dev_prefix", _DEV_WORKSPACE_PREFIX_PARAMS)
def test_dev_workspace_path_rejected_by_default_no_read(
    dev_prefix: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dev-workspace path fails closed with no filesystem read by default.

    Trip-wires ``pathlib.Path.exists``, ``pathlib.Path.iterdir``, and
    ``pathlib.Path.read_bytes`` for the duration of the call so any
    resolver-internal filesystem interaction with the operator path
    surfaces as an assertion here (not as a silent authority bypass).
    Runs once per prefix in
    :data:`workers.mapping_builder.cli._DEV_WORKSPACE_PREFIXES`.
    """
    filesystem_hits: list[str] = []

    def _hit(method: str):
        def _trip(self, *_args, **_kwargs):
            filesystem_hits.append(f"{method}:{self!s}")
            raise AssertionError(
                f"resolver called Path.{method}({self!s}) — §2.2 forbids "
                "any filesystem read on a rejected dev-workspace path"
            )
        return _trip

    monkeypatch.setattr(pathlib.Path, "exists", _hit("exists"))
    monkeypatch.setattr(pathlib.Path, "iterdir", _hit("iterdir"))
    monkeypatch.setattr(pathlib.Path, "read_bytes", _hit("read_bytes"))
    monkeypatch.setattr(pathlib.Path, "read_text", _hit("read_text"))

    with pytest.raises(PackagePathAuthorityError, match="dev-workspace"):
        resolve_package_path(
            package_path=dev_prefix / "keliya" / "package",
        )
    assert filesystem_hits == []


@pytest.mark.parametrize("dev_prefix", _DEV_WORKSPACE_PREFIX_PARAMS)
def test_dev_workspace_override_accepted_and_recorded(
    dev_prefix: pathlib.Path,
) -> None:
    """--allow-dev-workspace + rationale unblocks the path and records the override.

    Runs once per prefix in
    :data:`workers.mapping_builder.cli._DEV_WORKSPACE_PREFIXES`.
    """
    dev_path = dev_prefix / "keliya" / "package"

    resolved = resolve_package_path(
        package_path=dev_path,
        allow_dev_workspace=True,
        dev_workspace_rationale="node-27 emergency debug",
    )

    assert resolved.basin_name == "keliya"
    evidence = resolved.input_authority_evidence
    assert evidence["channel"] == "dev_workspace"
    assert evidence["dev_workspace_override"] == {
        "path": str(dev_path),
        "rationale": "node-27 emergency debug",
    }


@pytest.mark.parametrize("dev_prefix", _DEV_WORKSPACE_PREFIX_PARAMS)
def test_dev_workspace_override_requires_non_empty_rationale(
    dev_prefix: pathlib.Path,
) -> None:
    """--allow-dev-workspace without a rationale still fails closed.

    Runs once per prefix in
    :data:`workers.mapping_builder.cli._DEV_WORKSPACE_PREFIXES`.
    """
    dev_path = dev_prefix / "keliya" / "package"

    with pytest.raises(PackagePathAuthorityError, match="rationale"):
        resolve_package_path(
            package_path=dev_path,
            allow_dev_workspace=True,
            dev_workspace_rationale=None,
        )
    with pytest.raises(PackagePathAuthorityError, match="rationale"):
        resolve_package_path(
            package_path=dev_path,
            allow_dev_workspace=True,
            dev_workspace_rationale="   ",
        )


def test_resolve_package_path_is_deterministic(tmp_path: pathlib.Path) -> None:
    """Same kwargs twice produce byte-identical resolution + evidence.

    :func:`resolve_package_path` is claimed to be pure lexical path
    arithmetic — no filesystem read, no clock, no counter, no memoization.
    This test locks that contract on BOTH sanctioned channels
    (object-store shape with ``--object-store-root`` override, and
    dev-workspace override) so a future regression that injects a UUID,
    monotonic id, or cached state slips through only by breaking this
    determinism assertion.
    """
    # Channel 1: object-store shape via --object-store-root override.
    staged_root = tmp_path / "staged"
    package_path = (
        staged_root
        / "models"
        / "basins_keliya_shud"
        / "rel_v1"
        / "package"
    )
    res1 = resolve_package_path(
        package_path=package_path,
        object_store_root=staged_root,
    )
    res2 = resolve_package_path(
        package_path=package_path,
        object_store_root=staged_root,
    )
    assert res1 == res2
    assert res1.input_authority_evidence == res2.input_authority_evidence

    # Channel 2: dev-workspace override for symmetry across both accepted channels.
    dev_path = pathlib.Path("/home/ghdc/nwm/Basins/keliya/package")
    dev1 = resolve_package_path(
        package_path=dev_path,
        allow_dev_workspace=True,
        dev_workspace_rationale="determinism lock",
    )
    dev2 = resolve_package_path(
        package_path=dev_path,
        allow_dev_workspace=True,
        dev_workspace_rationale="determinism lock",
    )
    assert dev1 == dev2
    assert dev1.input_authority_evidence == dev2.input_authority_evidence


def test_object_store_root_non_matching_path_fails_closed_no_read_no_output(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A path that does not sit under --object-store-root fails closed silently."""
    staged_root = tmp_path / "staged"

    # capsys baseline: capture any stray print / stderr from the resolver.
    _ = capsys.readouterr()

    with pytest.raises(PackagePathAuthorityError):
        resolve_package_path(
            package_path=pathlib.Path(
                "/some/other/path/models/basins_keliya_shud/rel_v1/package"
            ),
            object_store_root=staged_root,
        )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_input_authority_neither_shape_fails_closed() -> None:
    """A random path (neither object-store nor dev-workspace) fails closed."""
    with pytest.raises(PackagePathAuthorityError, match="neither"):
        resolve_package_path(package_path=pathlib.Path("/random/junk/path"))


# =========================================================================
# §2.2 G0 EVIDENCE PROPAGATION: input_authority merges into evidence_ref
# =========================================================================


def test_g0_evidence_ref_carries_input_authority_when_build_receives_it(
    tmp_path: pathlib.Path,
) -> None:
    """build_direct_grid_variant merges input_authority_evidence into G0.

    Uses the sanctioned tmp ``--object-store-root`` channel: the keliya
    fixture is staged under
    ``<tmp>/staged/models/basins_keliya_shud/<release>/package/`` and
    the resolver's returned evidence dict is threaded through
    ``build_direct_grid_variant``. The resulting evidence bundle's G0
    ``evidence_ref`` MUST carry the ``input_authority`` sub-dict verbatim.
    """
    staged_root = tmp_path / "staged"
    release_id = "rel_v1"
    staged_package = (
        staged_root
        / "models"
        / "basins_keliya_shud"
        / release_id
        / "package"
    )
    # Stage the keliya fixture at the sanctioned path.
    shutil.copytree(_KELIYA_FIXTURE_DIR, staged_package)
    (staged_package / "build.py").unlink(missing_ok=True)
    for rel_paths in _CATEGORY_FILES.values():
        for rel_path in rel_paths:
            target = staged_package / rel_path
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"stub:{rel_path}\n".encode("utf-8"))

    resolved = resolve_package_path(
        package_path=staged_package,
        object_store_root=staged_root,
    )

    variant_root = tmp_path / "variant"
    domain_shp = _write_domain_shp(tmp_path)
    _, snapshot_cells, loader, snapshot_reference = _snapshot_and_loader()
    distance_qa, capacity_report = _canned_qa_and_capacity()

    result = build_direct_grid_variant(
        baseline_root=resolved.baseline_root,
        variant_root=variant_root,
        source_id=_SOURCE_ID,
        grid_id=_GRID_ID,
        grid_snapshot_loader=loader,
        snapshot_cells=snapshot_cells,
        grid_snapshot_reference=snapshot_reference,
        mapping_asset_identity=_MAPPING_ASSET_IDENTITY,
        model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
        binding_uri=_BINDING_URI,
        sp_att_manifest_path=_SP_ATT_MANIFEST_PATH,
        category_files=_CATEGORY_FILES,
        state_schema_bytes=b"state-schema-v1",
        solver_config_bytes=b"solver-config-v1",
        domain_shp_path=domain_shp,
        proj_crs_database_version=_PROJ_CRS_DB_VERSION,
        approvals=Approvals(
            builder_approver_id="tester@example.com",
            reviewer_approver_id="reviewer@example.com",
            small_basin_override_approver_id=None,
        ),
        rollback_target=RollbackTarget(
            previous_mapping_asset_checksum="",
            previous_mapping_asset_label="<initial>",
        ),
        distance_qa=distance_qa,
        capacity_report=capacity_report,
        input_authority_evidence=resolved.input_authority_evidence,
    )

    g0_ref = result.evidence_package.gate_results.g0.evidence_ref
    assert g0_ref["kind"] == "baseline_integrity_report"
    assert "checksum" in g0_ref
    # The input_authority sub-dict is present and byte-equal to the
    # resolver's returned evidence.
    assert g0_ref["input_authority"] == dict(resolved.input_authority_evidence)
    # And in particular records the non-default object-store root.
    assert g0_ref["input_authority"]["object_store_root"] == str(staged_root)
    assert g0_ref["input_authority"]["object_store_root_override"] is True
    assert g0_ref["input_authority"]["channel"] == "object_store"


def test_g0_evidence_ref_omits_input_authority_when_build_receives_none(
    tmp_path: pathlib.Path,
) -> None:
    """When the caller bypasses the argv resolver, G0 keeps its SUB-2 shape."""
    result = _run_build(tmp_path)

    g0_ref = result.evidence_package.gate_results.g0.evidence_ref
    assert g0_ref["kind"] == "baseline_integrity_report"
    assert "checksum" in g0_ref
    assert "input_authority" not in g0_ref


# =========================================================================
# §2.2 ARGPARSE MAIN: resolver success + failure + parse error
# =========================================================================


def test_argparse_main_object_store_root_success_prints_resolution_and_exits_zero(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A resolvable object-store shape → exit 0 + resolution JSON on stdout."""
    staged_root = tmp_path / "staged"
    package_path = (
        staged_root
        / "models"
        / "basins_keliya_shud"
        / "rel_v1"
        / "package"
    )

    exit_code = cli_module.main(
        [
            "--package-path",
            str(package_path),
            "--variant-root",
            str(tmp_path / "variant-out"),
            "--object-store-root",
            str(staged_root),
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "resolved"
    assert payload["basin_name"] == "keliya"
    assert payload["release_id"] == "rel_v1"
    assert payload["baseline_root"] == str(package_path)
    assert payload["variant_root"] == str(tmp_path / "variant-out")
    evidence = payload["input_authority_evidence"]
    assert evidence["object_store_root_override"] is True
    assert evidence["object_store_root"] == str(staged_root)
    assert evidence["channel"] == "object_store"
    # No error text on stderr.
    assert captured.err == ""


def test_argparse_main_input_authority_failure_prints_error_and_exits_nonzero(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A random path → exit 1 + JSON error diagnostic on stderr (nothing on stdout)."""
    exit_code = cli_module.main(
        [
            "--package-path",
            "/random/junk/path",
            "--variant-root",
            str(tmp_path / "variant-out"),
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    diagnostic = json.loads(captured.err)
    assert diagnostic["status"] == "error"
    assert diagnostic["error"] == "PackagePathAuthorityError"
    assert "neither" in diagnostic["message"]


def test_main_argparse_parse_error_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown argparse flag surfaces as ``SystemExit(2)`` (argparse default).

    Guards against a downstream wrapper that swallows argparse errors
    into a zero exit — the operator must see a non-zero exit for any
    malformed invocation.
    """
    with pytest.raises(SystemExit) as excinfo:
        cli_module.main(["--nonexistent-flag"])
    assert excinfo.value.code == 2
    # argparse writes its own diagnostic to stderr; only verify presence.
    captured = capsys.readouterr()
    assert captured.err != ""


# =========================================================================
# §2.3 SUB-4 FORBIDDEN-OUTPUT SCAN + READ-ONLY BASELINE INVARIANTS
# =========================================================================
# The three tests below cover the required
#   pytest -k "forbidden_output or read_only or zero_write"
# Evidence Floor filter. All exercise ``build_direct_grid_variant``
# through the same keliya fixture the SUB-2 tests use, so a §2.3
# regression surfaces alongside the existing G0..G5 chain tests.


def _snapshot_baseline_byte_set(
    baseline_root: pathlib.Path,
) -> dict[str, str]:
    """Return ``{relpath: sha256_hex}`` for every file under ``baseline_root``.

    Sorted by rel-path for deterministic iteration; per-file SHA-256 is
    the same primitive :func:`_compute_per_file_checksums` uses so a
    read-only invariant assertion is byte-equivalent to the library
    :class:`BaselineIntegrityReport`'s per-file checksums.
    """
    entries: dict[str, str] = {}
    for path in sorted(baseline_root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(baseline_root).as_posix()
        entries[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return entries


def test_forbidden_output_written_tree_clean_after_happy_path_build(
    tmp_path: pathlib.Path,
) -> None:
    """The happy-path variant tree contains zero §8.1 forbidden runtime artifacts.

    Enumerates every file the CLI wrote under ``variant_root`` and
    asserts none matches the three §8.1 filename regexes (cycle-dated
    ``.tsd.forc``, ``X<lon>Y<lat>.csv``, ``X<n>.csv``), and that the
    evidence bundle's :class:`ForbiddenOutputScanResult` records
    ``passed=True`` with all three offending tuples empty. This is the
    §2.3 written-tree scan on the clean set — companion to the two
    fail-closed injection tests below.
    """
    result = _run_build(tmp_path)

    variant_root = result.variant_root
    written_files = [p for p in variant_root.rglob("*") if p.is_file()]
    assert written_files, "expected non-empty variant tree from happy build"

    # No filename matches any of the three §8.1 forbidden-runtime patterns.
    for path in written_files:
        name = path.name
        assert not LEGACY_CYCLE_TSD_FORC_PATTERN.fullmatch(name), (
            f"variant carries a cycle-dated .tsd.forc {path!s}; "
            "§8.1 boundary says the runtime producer owns cycle .tsd.forc"
        )
        assert not LEGACY_STATION_LONLAT_CSV_PATTERN.fullmatch(name), (
            f"variant carries a legacy X<lon>Y<lat>.csv {path!s}; "
            "§8.1 boundary says the runtime producer owns station weather CSVs"
        )
        assert not LEGACY_STATION_NUMBERED_CSV_PATTERN.fullmatch(name), (
            f"variant carries a legacy X<n>.csv {path!s}; "
            "§8.1 boundary says the runtime producer owns station weather CSVs"
        )

    # And the active-forcing subtree (``input/``) carries no legacy CMFD
    # weather CSV either — belt-and-suspenders against the §3.5 gate.
    active_forcing_dir = variant_root / "input"
    if active_forcing_dir.is_dir():
        for path in active_forcing_dir.rglob("*"):
            if not path.is_file():
                continue
            name = path.name
            assert not LEGACY_STATION_LONLAT_CSV_PATTERN.fullmatch(name)
            assert not LEGACY_STATION_NUMBERED_CSV_PATTERN.fullmatch(name)
            assert not LEGACY_CYCLE_TSD_FORC_PATTERN.fullmatch(name)

    # And the evidence bundle records the §8.1 scan verdict as PASSED
    # with every offending tuple empty (no path / no DB write / no
    # cycle-lineage record).
    scan = result.evidence_package.forbidden_output_scan
    assert scan.passed is True
    assert scan.offending_paths == ()
    assert scan.offending_db_writes == ()
    assert scan.cycle_lineage_records == ()
    # And the scan actually walked the written tree — a zero
    # ``scanned_path_count`` would be an empty-artifact-set false PASS.
    assert scan.scanned_path_count == len(written_files)


def test_forbidden_output_injected_tsd_forc_in_baseline_fails_closed_no_variant(
    tmp_path: pathlib.Path,
) -> None:
    """Injected cycle-dated ``.tsd.forc`` fails closed via §8.1; no variant survives.

    Renames the keliya fixture's ``keliya.tsd.forc`` to
    ``20240101.tsd.forc`` so G0's single-``.tsd.forc`` invariant still
    holds (else G0's ``UnparseableAttError`` would fire before §8.1)
    while the resulting variant tree would carry a cycle-dated
    ``.tsd.forc`` at root. The CLI's §8.1 scan
    (:func:`_forbidden_output_scan`) MUST raise
    :class:`ForbiddenRuntimeProducerArtifactError`, the outer
    try/except MUST remove the ``.building`` staging directory, and the
    baseline byte-set MUST be unchanged.
    """
    baseline_root = _prepared_baseline(tmp_path)
    # Rename baseline .tsd.forc -> cycle-dated name so §8.1 fires at
    # line 812 in cli.py (not G0's multiple-.tsd.forc rejection nor
    # the §3.5 active-tree gate which only scans ``input/``).
    original_tsd_forc = baseline_root / "keliya.tsd.forc"
    injected_tsd_forc = baseline_root / "20240101.tsd.forc"
    original_tsd_forc.rename(injected_tsd_forc)
    # Sanity: the injected name actually matches the §8.1 pattern.
    assert LEGACY_CYCLE_TSD_FORC_PATTERN.fullmatch(injected_tsd_forc.name)

    pre_baseline_bytes = _snapshot_baseline_byte_set(baseline_root)
    variant_root = tmp_path / "variant"
    domain_shp = _write_domain_shp(tmp_path)
    _, snapshot_cells, loader, snapshot_reference = _snapshot_and_loader()
    distance_qa, capacity_report = _canned_qa_and_capacity()

    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as excinfo:
        build_direct_grid_variant(
            baseline_root=baseline_root,
            variant_root=variant_root,
            source_id=_SOURCE_ID,
            grid_id=_GRID_ID,
            grid_snapshot_loader=loader,
            snapshot_cells=snapshot_cells,
            grid_snapshot_reference=snapshot_reference,
            mapping_asset_identity=_MAPPING_ASSET_IDENTITY,
            model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
            binding_uri=_BINDING_URI,
            sp_att_manifest_path=_SP_ATT_MANIFEST_PATH,
            category_files=_CATEGORY_FILES,
            state_schema_bytes=b"state-schema-v1",
            solver_config_bytes=b"solver-config-v1",
            domain_shp_path=domain_shp,
            proj_crs_database_version=_PROJ_CRS_DB_VERSION,
            approvals=Approvals(
                builder_approver_id="tester@example.com",
                reviewer_approver_id="reviewer@example.com",
                small_basin_override_approver_id=None,
            ),
            rollback_target=RollbackTarget(
                previous_mapping_asset_checksum="",
                previous_mapping_asset_label="<initial>",
            ),
            distance_qa=distance_qa,
            capacity_report=capacity_report,
        )

    # The exception surfaces the offending class + evidence so operators
    # can trace which §8.1 category fired without re-running the scan.
    assert (
        excinfo.value.offending_class
        == ForbiddenOutputClass.CYCLE_DATED_TSD_FORC.value
    )
    assert pathlib.Path(excinfo.value.offending_evidence).name == injected_tsd_forc.name
    # The scan_summary carries the same 4-class breakdown the pass path
    # exposes on the evidence bundle.
    scan_summary = excinfo.value.scan_summary
    assert scan_summary.passed is False
    assert len(scan_summary.offending_paths) >= 1
    assert scan_summary.offending_paths[0][0] == (
        ForbiddenOutputClass.CYCLE_DATED_TSD_FORC.value
    )

    # No-partial-output: final variant absent + tmp ``.building`` absent.
    assert not variant_root.exists()
    assert not variant_root.with_name(variant_root.name + ".building").exists()

    # Read-only baseline: every file's SHA-256 is byte-identical.
    post_baseline_bytes = _snapshot_baseline_byte_set(baseline_root)
    assert post_baseline_bytes == pre_baseline_bytes, (
        "baseline tree was mutated by a failed build — SUB-4 zero-write "
        "invariant is broken"
    )


@pytest.mark.parametrize("injected_csv_name", ["X100Y37.csv", "X100Y37.CSV"])
def test_forbidden_output_injected_station_weather_csv_fails_closed_no_variant(
    tmp_path: pathlib.Path,
    injected_csv_name: str,
) -> None:
    """Injected ``X<lon>Y<lat>.csv`` at baseline root fails closed via §8.1.

    Plants ``X100Y37.csv`` at the baseline root (NOT under ``input/``, so
    the §3.5 active-tree gate — which only walks the active forcing
    subdir — is bypassed and the §8.1 written-tree scan is what catches
    the violation). Companion to the ``.tsd.forc`` injection test — the
    two together exercise the two on-disk §8.1 forbidden classes
    (``cycle_dated_tsd_forc`` + ``station_weather_csv``) via the CLI's
    written-tree scan.

    Parametrized over lowercase ``.csv`` and uppercase ``.CSV`` — the
    §8.2 clause-A pattern (``binding.py:2287-2292``) is documented as
    case-insensitive via ``re.IGNORECASE`` at ``rewrite.py:710``; the
    uppercase case guards against silent removal of that flag.
    """
    baseline_root = _prepared_baseline(tmp_path)
    injected_csv = baseline_root / injected_csv_name
    injected_csv.write_bytes(b"stub legacy CMFD station weather CSV\n")
    # Sanity: the injected name actually matches the §8.2 clause-A pattern.
    assert LEGACY_STATION_LONLAT_CSV_PATTERN.fullmatch(injected_csv.name)

    pre_baseline_bytes = _snapshot_baseline_byte_set(baseline_root)
    variant_root = tmp_path / "variant"
    domain_shp = _write_domain_shp(tmp_path)
    _, snapshot_cells, loader, snapshot_reference = _snapshot_and_loader()
    distance_qa, capacity_report = _canned_qa_and_capacity()

    with pytest.raises(ForbiddenRuntimeProducerArtifactError) as excinfo:
        build_direct_grid_variant(
            baseline_root=baseline_root,
            variant_root=variant_root,
            source_id=_SOURCE_ID,
            grid_id=_GRID_ID,
            grid_snapshot_loader=loader,
            snapshot_cells=snapshot_cells,
            grid_snapshot_reference=snapshot_reference,
            mapping_asset_identity=_MAPPING_ASSET_IDENTITY,
            model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
            binding_uri=_BINDING_URI,
            sp_att_manifest_path=_SP_ATT_MANIFEST_PATH,
            category_files=_CATEGORY_FILES,
            state_schema_bytes=b"state-schema-v1",
            solver_config_bytes=b"solver-config-v1",
            domain_shp_path=domain_shp,
            proj_crs_database_version=_PROJ_CRS_DB_VERSION,
            approvals=Approvals(
                builder_approver_id="tester@example.com",
                reviewer_approver_id="reviewer@example.com",
                small_basin_override_approver_id=None,
            ),
            rollback_target=RollbackTarget(
                previous_mapping_asset_checksum="",
                previous_mapping_asset_label="<initial>",
            ),
            distance_qa=distance_qa,
            capacity_report=capacity_report,
        )

    assert (
        excinfo.value.offending_class
        == ForbiddenOutputClass.STATION_WEATHER_CSV.value
    )
    assert pathlib.Path(excinfo.value.offending_evidence).name == injected_csv.name
    # The scan_summary carries the same 4-class breakdown the pass path
    # exposes on the evidence bundle.
    scan_summary = excinfo.value.scan_summary
    assert scan_summary.passed is False
    assert len(scan_summary.offending_paths) >= 1
    assert scan_summary.offending_paths[0][0] == (
        ForbiddenOutputClass.STATION_WEATHER_CSV.value
    )

    assert not variant_root.exists()
    assert not variant_root.with_name(variant_root.name + ".building").exists()

    post_baseline_bytes = _snapshot_baseline_byte_set(baseline_root)
    assert post_baseline_bytes == pre_baseline_bytes


def test_zero_write_baseline_files_read_only_pre_post_checksums_unchanged(
    tmp_path: pathlib.Path,
) -> None:
    """Happy build never mutates baseline files — pre/post SHA-256s are equal.

    Snapshots per-file SHA-256 of every regular file under
    ``baseline_root`` BEFORE and AFTER a green
    :func:`build_direct_grid_variant` run. The two byte-sets MUST be
    dict-equal so the SUB-4 §2.3 zero-production-writes invariant is
    proved. The CLI's ``shutil.copytree`` copies FROM the baseline, and
    every subsequent write lands in ``variant_root`` — this test
    guarantees no downstream code silently writes back into the
    baseline.
    """
    baseline_root = _prepared_baseline(tmp_path)
    pre_baseline_bytes = _snapshot_baseline_byte_set(baseline_root)
    assert pre_baseline_bytes, "expected non-empty baseline for the invariant lock"

    variant_root = tmp_path / "variant"
    domain_shp = _write_domain_shp(tmp_path)
    _, snapshot_cells, loader, snapshot_reference = _snapshot_and_loader()
    distance_qa, capacity_report = _canned_qa_and_capacity()

    result = build_direct_grid_variant(
        baseline_root=baseline_root,
        variant_root=variant_root,
        source_id=_SOURCE_ID,
        grid_id=_GRID_ID,
        grid_snapshot_loader=loader,
        snapshot_cells=snapshot_cells,
        grid_snapshot_reference=snapshot_reference,
        mapping_asset_identity=_MAPPING_ASSET_IDENTITY,
        model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
        binding_uri=_BINDING_URI,
        sp_att_manifest_path=_SP_ATT_MANIFEST_PATH,
        category_files=_CATEGORY_FILES,
        state_schema_bytes=b"state-schema-v1",
        solver_config_bytes=b"solver-config-v1",
        domain_shp_path=domain_shp,
        proj_crs_database_version=_PROJ_CRS_DB_VERSION,
        approvals=Approvals(
            builder_approver_id="tester@example.com",
            reviewer_approver_id="reviewer@example.com",
            small_basin_override_approver_id=None,
        ),
        rollback_target=RollbackTarget(
            previous_mapping_asset_checksum="",
            previous_mapping_asset_label="<initial>",
        ),
        distance_qa=distance_qa,
        capacity_report=capacity_report,
    )

    # Baseline byte-set is byte-identical post-build.
    post_baseline_bytes = _snapshot_baseline_byte_set(baseline_root)
    assert post_baseline_bytes == pre_baseline_bytes, (
        "SUB-4 §2.3 zero-write invariant broken: baseline files were "
        "mutated during a happy build"
    )
    # Sanity: the write path did commit — variant_root exists and holds
    # the emitted artifacts, so the equality above proves "baseline
    # untouched despite a green build", not "no build happened at all".
    assert result.variant_root == variant_root
    assert variant_root.is_dir()
    assert (variant_root / "manifest.json").is_file()
    assert (variant_root / "direct_grid_binding.json").is_file()


# =========================================================================
# §2.4 SUB-5 CLI DETERMINISM + KELIYA E2E VIA RESOLVER CHANNEL
# =========================================================================
# The three tests below cover the required
#   pytest -k "deterministic or keliya"
# Evidence Floor filter. Each drives ``build_direct_grid_variant`` over
# the compact keliya fixture — the first two prove byte-level determinism
# + wall-clock-free evidence; the third routes the build through the
# SUB-3 §2.2 object-store input-authority resolver so the argv-authority
# channel is exercised end-to-end (staging → resolve → build) alongside
# the 484 elements / 32 stations / 8 used cells fixture invariants.


def test_build_direct_grid_variant_is_deterministic_across_two_runs_raw_byte_identical(
    tmp_path: pathlib.Path,
) -> None:
    """Two runs on identical inputs produce byte-identical on-disk artifacts.

    Raw byte comparison — NO field masking — of every file the CLI writes
    under ``variant_root``: ``manifest.json``, ``direct_grid_binding.json``,
    and the variant ``.sp.att``. Two ``build_direct_grid_variant`` invocations
    on freshly-staged copies of the keliya fixture MUST produce identical
    bytes for each artifact. Any smuggled wall-clock, UUID, monotonic id,
    or environment-dependent value surfaces here as an inequality; the
    library helper :func:`packages.common.grid_signature.canonical_json_bytes`
    already guarantees deterministic key ordering + float formatting, so a
    failure here always points at the caller side (this file + cli.py).

    Runs the two builds under sibling tmp subdirs so each caller supplies
    a fresh ``variant_root`` per the atomic-rename discipline.
    """
    run_a_root = tmp_path / "run_a"
    run_b_root = tmp_path / "run_b"
    run_a_root.mkdir()
    run_b_root.mkdir()

    result_a = _run_build(run_a_root)
    result_b = _run_build(run_b_root)

    # Every on-disk artifact the CLI writes: byte-for-byte equal.
    on_disk_artifacts = ("manifest.json", "direct_grid_binding.json", "keliya.sp.att")
    for filename in on_disk_artifacts:
        bytes_a = (result_a.variant_root / filename).read_bytes()
        bytes_b = (result_b.variant_root / filename).read_bytes()
        assert bytes_a == bytes_b, (
            f"{filename}: two runs produced diverging bytes — "
            f"SHA-256(a)={hashlib.sha256(bytes_a).hexdigest()} vs "
            f"SHA-256(b)={hashlib.sha256(bytes_b).hexdigest()}; "
            "some caller-side non-determinism (wall-clock, UUID, counter, "
            "or env-dependent value) leaked into the emitted bytes"
        )

    # Redundant lock on the derived checksums — the manifest's recorded
    # binding_checksum and the binding artifact's own SHA-256 both hash the
    # same emitted bytes, so a byte-identity divergence would already fire
    # above; equality here is a belt-and-suspenders check against a future
    # regression that recomputed a checksum from a different byte payload.
    assert result_a.manifest.binding_checksum == result_b.manifest.binding_checksum
    assert result_a.binding_artifact.checksum == result_b.binding_artifact.checksum

    # And the evidence package's ``build_timestamp`` MUST be unset in both
    # runs — the §2.4 no-wall-clock discipline is what makes the byte-level
    # determinism above possible in the first place.
    assert result_a.evidence_package.build_timestamp is None
    assert result_b.evidence_package.build_timestamp is None


def test_deterministic_cli_emits_evidence_with_build_timestamp_unset_no_wall_clock(
    tmp_path: pathlib.Path,
) -> None:
    """The emitted evidence records ``build_timestamp`` as ``None`` — no wall-clock leak.

    §2.4 no-wall-clock discipline: the CLI orchestrator MUST NOT stamp any
    ``datetime.now()`` / ``time.time()`` / date literal into the evidence
    bundle or the on-disk manifest/binding. Assertions:

    1. :attr:`EvidencePackage.build_timestamp` is ``None`` (the direct
       oracle for the unset-timestamp contract per cli.py line ~982).
    2. The emitted ``manifest.json`` and ``direct_grid_binding.json`` bytes
       contain no ``build_timestamp`` key (belt-and-suspenders against a
       future regression that serialized the field into the manifest).
    3. Neither on-disk artifact contains any ISO-8601 datetime pattern
       (``YYYY-MM-DDTHH:MM:SS``) — a wall-clock leak in an evidence_ref or
       manifest field would surface as a matching substring here.
    """
    result = _run_build(tmp_path)

    # 1. Primary oracle: the evidence dataclass field is unset.
    assert result.evidence_package.build_timestamp is None, (
        f"evidence_package.build_timestamp is {result.evidence_package.build_timestamp!r}; "
        "§2.4 forbids any wall-clock stamp — cli.py must pass build_timestamp=None"
    )

    # 2. On-disk sanity: neither emitted artifact carries the field name.
    manifest_bytes = (result.variant_root / "manifest.json").read_bytes()
    binding_bytes = (result.variant_root / "direct_grid_binding.json").read_bytes()
    assert b"build_timestamp" not in manifest_bytes, (
        "manifest.json bytes contain 'build_timestamp' — evidence field "
        "must not leak into the manifest surface"
    )
    assert b"build_timestamp" not in binding_bytes, (
        "direct_grid_binding.json bytes contain 'build_timestamp' — the "
        "evidence field must not leak into the standalone binding surface"
    )

    # 3. No ISO-8601 datetime pattern anywhere in the manifest / binding
    # bytes. A leaked ``datetime.now().isoformat()`` (with or without the
    # ``Z`` UTC suffix) would surface as ``YYYY-MM-DDTHH:MM:SS`` here.
    iso_datetime_pattern = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
    for filename, content in (
        ("manifest.json", manifest_bytes),
        ("direct_grid_binding.json", binding_bytes),
    ):
        match = iso_datetime_pattern.search(content)
        assert match is None, (
            f"{filename}: emitted bytes contain an ISO-8601 datetime "
            f"{match.group().decode()!r} — a wall-clock value leaked into "
            "the deterministic artifact surface"
        )


def test_keliya_end_to_end_cli_build_via_object_store_root_resolver_channel(
    tmp_path: pathlib.Path,
) -> None:
    """Full G0→G5 build over keliya staged under a tmp ``--object-store-root``.

    Exercises the SUB-3 §2.2 sanctioned tmp ``--object-store-root`` staging
    channel end-to-end: the keliya fixture is copied to
    ``<tmp>/object_store/models/basins_keliya_shud/rel_v1/package/`` (the
    exact object-store release-frozen shape the resolver validates),
    :func:`resolve_package_path` is called with the tmp root override,
    and :func:`build_direct_grid_variant` runs the full G0..G5 chain with
    the resolver's evidence dict threaded through. Assertions cover the
    fixture invariants (484 elements / 32→8 stations / 8 used cells), the
    emitted-artifact set, the G0..G5 gate results, and the G0
    ``evidence_ref`` propagation of the input-authority record.
    """
    # Stage the keliya fixture at the sanctioned object-store shape.
    staged_root = tmp_path / "object_store"
    release_id = "rel_v1"
    staged_package = (
        staged_root
        / "models"
        / "basins_keliya_shud"
        / release_id
        / "package"
    )
    shutil.copytree(_KELIYA_FIXTURE_DIR, staged_package)
    (staged_package / "build.py").unlink(missing_ok=True)
    for rel_paths in _CATEGORY_FILES.values():
        for rel_path in rel_paths:
            target = staged_package / rel_path
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"stub:{rel_path}\n".encode("utf-8"))

    # Resolve via the SUB-3 argv-authority channel with the tmp root
    # override. This is the ONLY sanctioned way to land tmp fixtures under
    # the object-store shape per §2.2 (no ad-hoc bypass).
    resolved = resolve_package_path(
        package_path=staged_package,
        object_store_root=staged_root,
    )
    assert resolved.basin_name == "keliya"
    assert resolved.release_id == release_id
    assert resolved.input_authority_evidence["channel"] == "object_store"
    assert resolved.input_authority_evidence["object_store_root_override"] is True
    assert resolved.input_authority_evidence["object_store_root"] == str(staged_root)

    # Drive the full G0..G5 build from the resolver's outputs.
    variant_root = tmp_path / "variant"
    domain_shp = _write_domain_shp(tmp_path)
    _, snapshot_cells, loader, snapshot_reference = _snapshot_and_loader()
    distance_qa, capacity_report = _canned_qa_and_capacity()

    result = build_direct_grid_variant(
        baseline_root=resolved.baseline_root,
        variant_root=variant_root,
        source_id=_SOURCE_ID,
        grid_id=_GRID_ID,
        grid_snapshot_loader=loader,
        snapshot_cells=snapshot_cells,
        grid_snapshot_reference=snapshot_reference,
        mapping_asset_identity=_MAPPING_ASSET_IDENTITY,
        model_input_package_id=_MODEL_INPUT_PACKAGE_ID,
        binding_uri=_BINDING_URI,
        sp_att_manifest_path=_SP_ATT_MANIFEST_PATH,
        category_files=_CATEGORY_FILES,
        state_schema_bytes=b"state-schema-v1",
        solver_config_bytes=b"solver-config-v1",
        domain_shp_path=domain_shp,
        proj_crs_database_version=_PROJ_CRS_DB_VERSION,
        approvals=Approvals(
            builder_approver_id="tester@example.com",
            reviewer_approver_id="reviewer@example.com",
            small_basin_override_approver_id=None,
        ),
        rollback_target=RollbackTarget(
            previous_mapping_asset_checksum="",
            previous_mapping_asset_label="<initial>",
        ),
        distance_qa=distance_qa,
        capacity_report=capacity_report,
        input_authority_evidence=resolved.input_authority_evidence,
    )

    # BuildResult shape + variant_root population.
    assert isinstance(result, BuildResult)
    assert result.variant_root == variant_root
    assert variant_root.is_dir()
    assert (variant_root / "manifest.json").is_file()
    assert (variant_root / "direct_grid_binding.json").is_file()
    # The variant .sp.att sits at the same relative path as in the baseline
    # (mirrored by ``shutil.copytree`` + overwritten in place by G4).
    assert (variant_root / "keliya.sp.att").is_file()
    # No leftover .building sibling.
    assert not variant_root.with_name(variant_root.name + ".building").exists()

    # Fixture-shape invariants: 484 elements / 32 raw stations / 8 used cells.
    # ``ownership_table`` covers every element (mesh oracle = 484).
    assert len(result.evidence_package.ownership_table) == 484
    # 32 raw stations pre-reduction is recorded on the caller-supplied
    # capacity report (fed into the harness at
    # :func:`_canned_qa_and_capacity`, matching the keliya .tsd.forc count).
    assert result.evidence_package.capacity_report.before_station_count == 32
    # After ``derive_used_cell_subset``: 8 used cells → 8 station bindings
    # (surfaced both on the manifest and on the standalone binding).
    assert len(result.manifest.station_bindings) == 8
    assert len(result.binding_artifact.station_bindings) == 8
    assert result.evidence_package.capacity_report.after_station_count == 8

    # G0..G5 all passed.
    gate_results = result.evidence_package.gate_results
    for gate in (
        gate_results.g0,
        gate_results.g1,
        gate_results.g2,
        gate_results.g3,
        gate_results.g4,
        gate_results.g5,
    ):
        assert gate.passed is True, f"{gate.gate_id}: expected passed=True, got {gate!r}"

    # G0 evidence_ref propagates the resolver's input_authority record
    # verbatim so downstream audits can reconstruct the argv-authority
    # decision from the evidence bundle alone (SUB-3 wiring).
    g0_ref = gate_results.g0.evidence_ref
    assert g0_ref["kind"] == "baseline_integrity_report"
    assert g0_ref["input_authority"] == dict(resolved.input_authority_evidence)
    assert g0_ref["input_authority"]["basin_name"] == "keliya"
    assert g0_ref["input_authority"]["release_id"] == release_id
    assert g0_ref["input_authority"]["channel"] == "object_store"

    # Deterministic evidence: build_timestamp unset per §2.4.
    assert result.evidence_package.build_timestamp is None


# =========================================================================
# NO DUPLICATED STAGE LOGIC: import-site + no-local-def guardrail
# =========================================================================


def test_no_duplicated_stage_logic_all_stage_functions_come_from_stage_modules() -> None:
    """CLI imports each stage function and defines no same-name local function.

    Two-part guardrail:

    1. Each canonical stage name is imported into ``cli_module`` and its
       ``__module__`` attribute points at the owning library module (never
       ``workers.mapping_builder.cli``).
    2. The CLI source has no local ``def`` of any tracked stage name — a
       rebinding via ``def verify_g0_baseline(...)`` inside cli.py would
       be a silent re-implementation.

    Also asserts the module stays under a reasonable line count so the
    "thin CLI" convention (Decision 3) does not regress into a bloated
    orchestrator.
    """
    expected_origin = {
        "verify_g0_baseline": "workers.mapping_builder.integrity",
        "verify_g1_non_degenerate_triangles": "workers.mapping_builder.integrity",
        "verify_package_crs": "workers.mapping_builder.integrity",
        "nearest_cell_barycenter_geodesic_v1": "workers.mapping_builder.algorithm",
        "derive_used_cell_subset": "workers.mapping_builder.algorithm",
        "assign_shud_forcing_index": "workers.mapping_builder.algorithm",
        "verify_small_basin_gate": "workers.mapping_builder.algorithm",
        "copy_and_rewrite_sp_att_forc": "workers.mapping_builder.rewrite",
        "parse_sp_att_forc_rows": "workers.mapping_builder.rewrite",
        "verify_non_forc_columns_unchanged": "workers.mapping_builder.rewrite",
        "verify_non_sp_att_checksums_equal": "workers.mapping_builder.rewrite",
        "verify_hydrologic_core_fingerprint_equal": "workers.mapping_builder.rewrite",
        "verify_no_legacy_weather_path_in_active_tree": "workers.mapping_builder.rewrite",
        "emit_direct_grid_manifest_and_binding": "workers.mapping_builder.binding",
        "verify_no_forbidden_runtime_producer_artifacts": "workers.mapping_builder.binding",
        "assemble_evidence_package": "workers.mapping_builder.evidence",
        "render_ownership_images": "workers.mapping_builder.evidence",
        "resolve_verdict": "workers.mapping_builder.z_policy_verdict",
        "build_z_policy": "workers.mapping_builder.z_policy_verdict",
        "sample_per_cell_z": "workers.mapping_builder.z_policy_verdict",
    }

    # 1. Import site: each name is present in cli_module and its __module__
    # attribute points at the owning stage module.
    for name, expected_module in expected_origin.items():
        assert hasattr(cli_module, name), (
            f"CLI does not import required stage function {name!r}"
        )
        stage_fn = getattr(cli_module, name)
        assert callable(stage_fn), f"{name} is not callable"
        assert stage_fn.__module__ == expected_module, (
            f"{name} is defined in {stage_fn.__module__!r}, "
            f"expected {expected_module!r} — CLI must not re-implement stage logic"
        )

    # 2. AST scan: no local `def` of any tracked name.
    cli_path = pathlib.Path(cli_module.__file__)
    tree = ast.parse(cli_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name not in expected_origin, (
                f"cli.py defines a local `def {node.name}` — this shadows the "
                "library stage import and violates the thin-CLI discipline "
                "(Decision 3: no re-implementation of any stage)"
            )

    # 3. Thin-CLI line-count guardrail. A modest ceiling — expansion beyond
    # this suggests stage logic (not orchestration) creeping into cli.py.
    # SUB-3 (§2.2) landed the input-authority resolver (~230 lines including
    # docstrings + constants + exception + dataclass), pushing the ceiling
    # up. Deliberate bump — the resolver IS orchestration surface (argv
    # authority validation), not a G0..G5 stage re-implementation.
    line_count = len(cli_path.read_text(encoding="utf-8").splitlines())
    assert line_count < 1200, (
        f"cli.py grew to {line_count} lines; thin-CLI convention "
        "(Decision 3) suggests keeping the orchestrator under ~1000 lines. "
        "If additional orchestration is genuinely needed, raise this "
        "ceiling deliberately — don't ratchet silently."
    )
