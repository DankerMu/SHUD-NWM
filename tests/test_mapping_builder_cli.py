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
import pathlib
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
from workers.mapping_builder.binding import (
    BindingArtifactError,
    ParserRoundTripError,
)
from workers.mapping_builder.cli import (
    BuildResult,
    build_direct_grid_variant,
)
from workers.mapping_builder.evidence import (
    Approvals,
    CapacityReport,
    DistanceQA,
    GridSnapshotReference,
    RollbackTarget,
)
from workers.mapping_builder.integrity import BaselineIntegrityError
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

    # G5 evidence_ref carries the z-policy provenance.
    g5_ref = result.evidence_package.gate_results.g5.evidence_ref
    assert g5_ref["kind"] == "binding_g5"
    assert g5_ref["binding_checksum"] == result.binding_artifact.checksum
    assert g5_ref["sampler_rule_id"] == "nearest_mesh_node_elevation_v1"
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
    """Monkeypatch each library stage import to a tracker; assert G0..G5 order."""
    calls: list[str] = []
    tracked_names = [
        "verify_g0_baseline",
        "verify_g1_non_degenerate_triangles",
        "verify_package_crs",
        "nearest_cell_barycenter_geodesic_v1",
        "copy_and_rewrite_sp_att_forc",
        "emit_direct_grid_manifest_and_binding",
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


# =========================================================================
# FAIL-CLOSED: G5 contract mismatch leaves no variant
# =========================================================================


def test_g5_contract_mismatch_fails_closed_no_variant_written(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatch G5 emitter to raise; assert no variant / .building remnant."""
    def _boom(**_kwargs):
        raise ParserRoundTripError(
            parser_error_message=(
                "simulated G5 contract mismatch — parser could not round-trip the manifest"
            ),
        )

    monkeypatch.setattr(cli_module, "emit_direct_grid_manifest_and_binding", _boom)

    baseline_root = _prepared_baseline(tmp_path)
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


# =========================================================================
# G5 CROSS-CONSISTENCY: manifest.binding_checksum matches SHA-256(binding.json bytes)
# =========================================================================


def test_g5_cross_consistency_binding_checksum_equals_sha256_of_binding_bytes(
    tmp_path: pathlib.Path,
) -> None:
    """Standalone binding artifact bytes hash to the manifest's binding_checksum."""
    result = _run_build(tmp_path)

    binding_bytes = (result.variant_root / "direct_grid_binding.json").read_bytes()
    assert hashlib.sha256(binding_bytes).hexdigest() == result.manifest.binding_checksum
    # And the in-memory artifact bytes match the on-disk bytes.
    assert result.binding_artifact.bytes == binding_bytes

    # Station rows equal element-for-element between manifest and standalone binding.
    manifest_rows = {
        b.station_id: (b.grid_cell_id, b.shud_forcing_index)
        for b in result.manifest.station_bindings
    }
    binding_rows = {
        b.station_id: (b.grid_cell_id, b.shud_forcing_index)
        for b in result.binding_artifact.station_bindings
    }
    assert manifest_rows == binding_rows


# =========================================================================
# MANIFEST PARSES THROUGH DIRECT-GRID CONTRACT
# =========================================================================


def test_emitted_manifest_round_trips_through_direct_grid_contract_parser(
    tmp_path: pathlib.Path,
) -> None:
    """The emitted manifest.json parses cleanly through the contract entrypoint."""
    result = _run_build(tmp_path)

    profile = result.manifest.to_resource_profile_dict()
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
    line_count = len(cli_path.read_text(encoding="utf-8").splitlines())
    assert line_count < 800, (
        f"cli.py grew to {line_count} lines; thin-CLI convention "
        "(Decision 3) suggests keeping the orchestrator under ~500 lines. "
        "If additional orchestration is genuinely needed, raise this "
        "ceiling deliberately — don't ratchet silently."
    )
