"""§2.1 mapping builder CLI: chain G0-G5 stages, no partial output.

Thin operator entrypoint (Epic #973 SUB-2, OpenSpec change
``direct-grid-build-enablement``). Chains the existing
``workers.mapping_builder`` library stages plus the SUB-1
:mod:`workers.mapping_builder.z_policy_verdict` module. This module
NEVER re-implements any G0..G5 stage; every gate is delegated to the
library import. The import-site + no-local-def guardrail is enforced
by ``tests/test_mapping_builder_cli.py::test_no_duplicated_stage_logic_all_stage_functions_come_from_stage_modules``.

Deferred: SUB-3 (§2.2 operator argv path resolver), SUB-4 (§2.3 written-
tree forbidden-output scan), SUB-5 (§2.4 keliya deterministic 2-run
byte compare routed through the SUB-3 resolver). The argparse ``main``
here is a scaffold that returns exit code 2; the operator-facing shape
lands in SUB-3. Programmatic callers (tests, downstream orchestrators)
invoke :func:`build_direct_grid_variant` directly.

No-partial-output pattern: caller supplies a target ``variant_root`` that
MUST NOT already exist; the orchestrator stages writes into a sibling
``<variant_root>.building`` directory and ``os.rename`` -promotes it on
success; any raise removes the tmp dir and ``variant_root`` never
appears (atomic-rename discipline enforced by the two fail-closed
tests).

z_policy chain: :func:`resolve_verdict` -> :func:`build_z_policy`
(empty per_cell_z) -> :func:`sample_per_cell_z` ->
:func:`dataclasses.replace` on the frozen :class:`ZPolicy`. The
verified SHA-256 + sampler rule id + resolved path are recorded on the
emitted G5 :class:`GateResult.evidence_ref` so downstream audits
recover the provenance from the evidence package alone.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import pathlib
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from packages.common.grid_registry_store import CanonicalGridCell
from packages.common.grid_signature import canonical_json_bytes

# Stage imports — the import site is asserted by
# ``test_no_duplicated_stage_logic_all_stage_functions_come_from_stage_modules``.
from workers.mapping_builder import z_policy_verdict
from workers.mapping_builder.algorithm import (
    GridSnapshotLoader,
    SmallBasinApproval,
    assign_shud_forcing_index,
    derive_used_cell_subset,
    nearest_cell_barycenter_geodesic_v1,
    verify_small_basin_gate,
)
from workers.mapping_builder.binding import (
    BindingArtifact,
    BindingArtifactError,
    CycleLineageSpy,
    DbWriteSpy,
    DirectGridManifest,
    ForbiddenOutputScanResult,
    ZPolicy,
    emit_direct_grid_manifest_and_binding,
    verify_no_forbidden_runtime_producer_artifacts,
)
from workers.mapping_builder.evidence import (
    ALGORITHM_ID,
    Approvals,
    BaselineIdentity,
    CapacityReport,
    DistanceQA,
    EvidencePackage,
    GateResult,
    GateResults,
    GridSnapshotReference,
    MappingAlgorithmIdentity,
    OwnershipRow,
    RollbackTarget,
    SpAttAssetDiff,
    assemble_evidence_package,
    render_ownership_images,
)
from workers.mapping_builder.integrity import (
    BaselineIntegrityReport,
    G1NonDegenerateReport,
    PackageCrsReport,
    read_sp_mesh_geometry,
    verify_g0_baseline,
    verify_g1_non_degenerate_triangles,
    verify_package_crs,
)
from workers.mapping_builder.rewrite import (
    SpAttRewriteReport,
    copy_and_rewrite_sp_att_forc,
    parse_sp_att_forc_rows,
    verify_hydrologic_core_fingerprint_equal,
    verify_no_legacy_weather_path_in_active_tree,
    verify_non_forc_columns_unchanged,
    verify_non_sp_att_checksums_equal,
)
from workers.mapping_builder.z_policy_verdict import (
    MeshNode,
    PackageProjection,
    UsedCell,
    VerdictResolution,
    VerdictResolutionError,
    build_z_policy,
    resolve_verdict,
    sample_per_cell_z,
)

_MANIFEST_FILENAME = "manifest.json"
_BINDING_FILENAME = "direct_grid_binding.json"


# --- return record --------------------------------------------------------


@dataclass(frozen=True)
class BuildResult:
    """Frozen return record from :func:`build_direct_grid_variant`.

    Attributes
    ----------
    variant_root:
        The finalized variant package root (post atomic-rename).
    manifest:
        Emitted :class:`DirectGridManifest`.
    binding_artifact:
        Emitted :class:`BindingArtifact` (standalone binding).
    evidence_package:
        Assembled :class:`EvidencePackage` (returned in-memory; on-disk
        evidence bytes serialization lands in a follow-up sub-issue).
    verdict_resolution:
        The :class:`VerdictResolution` used for the z_policy binding.
        Recorded so upstream tests / SUB-3 evidence-recorder can bind
        the resolved path + override flag + verified SHA-256 without
        re-running :func:`resolve_verdict`.
    """

    variant_root: pathlib.Path
    manifest: DirectGridManifest
    binding_artifact: BindingArtifact
    evidence_package: EvidencePackage
    verdict_resolution: VerdictResolution


# --- helpers --------------------------------------------------------------


def _sha256_file(path: pathlib.Path) -> str:
    """Return the SHA-256 hex digest of the file's byte contents."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _parse_mesh_nodes(baseline_root: pathlib.Path) -> tuple[MeshNode, ...]:
    """Parse the baseline ``.sp.mesh`` node table into :class:`MeshNode` records.

    Reuses :func:`workers.mapping_builder.integrity.read_sp_mesh_geometry`
    for the ``(elements, node_xy)`` geometry contract (the SUB-4 public
    parser). The G1 helper validates the header row's first three columns
    (``ID X Y``) and surfaces ``(x, y)`` only; per-node ``Elevation`` is
    not surfaced by any existing public helper (each existing consumer
    needs only the X/Y coordinates). The CLI reads Elevation here by
    locating the ``Elevation`` column via the header row rather than by
    hardcoded index, so a permuted-but-legal column order (e.g.
    ``ID X Y Elevation AqDepth``) is handled correctly instead of
    silently reading ``AqDepth`` values as elevations.

    Tech debt (deferred, out of §2.1 scope): the header locator here
    duplicates the layout traversal that
    :func:`_parse_sp_mesh_g1_geometry` already performs internally.
    Extracting a public ``read_sp_mesh_node_columns`` helper in
    ``integrity.py`` would let the CLI drop the private-suffix helper
    import and the manual line-index arithmetic — tracked as a follow-up
    against the integrity module.
    """
    _elements, node_xy = read_sp_mesh_geometry(baseline_root)

    # Read Elevation from the file — read_sp_mesh_geometry surfaces only
    # (x, y). Locate the node block via the shared header conventions.
    from workers.mapping_builder.integrity import _find_single_by_suffix

    mesh_path = _find_single_by_suffix(baseline_root, ".sp.mesh")
    lines = mesh_path.read_text(encoding="utf-8").splitlines()
    element_header_counts = lines[0].split()
    n_elements = int(element_header_counts[0])
    node_header_index = 1 + 1 + n_elements
    node_header_counts = lines[node_header_index].split()
    n_nodes = int(node_header_counts[0])
    node_column_header = lines[node_header_index + 1]
    node_column_tokens = node_column_header.split()
    try:
        elevation_col_index = node_column_tokens.index("Elevation")
    except ValueError as exc:
        raise ValueError(
            f"{mesh_path.name}: node header missing 'Elevation' column; "
            f"got columns {node_column_tokens!r}"
        ) from exc
    node_data_start = node_header_index + 2  # skip header count + column names

    nodes: list[MeshNode] = []
    for raw in lines[node_data_start : node_data_start + n_nodes]:
        tokens = raw.split()
        node_id = int(tokens[0])
        x, y = node_xy[node_id]  # trust the checksum-bound parser's coords
        nodes.append(
            MeshNode(
                node_id=node_id,
                x=float(x),
                y=float(y),
                elevation=float(tokens[elevation_col_index]),
            )
        )
    return tuple(nodes)


def _adapt_used_cells_for_sampler(
    used_cells: Sequence[CanonicalGridCell],
) -> tuple[UsedCell, ...]:
    """Convert :class:`CanonicalGridCell` records to sampler-facing :class:`UsedCell`."""
    return tuple(
        UsedCell(
            cell_id=cell.grid_cell_id,
            wgs84_lon=float(cell.longitude),
            wgs84_lat=float(cell.latitude),
        )
        for cell in used_cells
    )


def _build_ownership_rows(
    *,
    ownerships,
    shud_forcing_index: Mapping[str, int],
    baseline_forc_by_element: Mapping[int, int],
) -> tuple[OwnershipRow, ...]:
    """Assemble :class:`OwnershipRow` records from ownership + forcing-index maps."""
    rows: list[OwnershipRow] = []
    for own in ownerships:
        old_forc = baseline_forc_by_element[int(own.element_id)]
        new_forc = shud_forcing_index[own.grid_cell_id]
        rows.append(
            OwnershipRow(
                element_id=int(own.element_id),
                old_forc=str(old_forc),
                new_forc=str(new_forc),
                grid_cell_id=own.grid_cell_id,
                distance_meters=float(own.geodesic_distance_m),
            )
        )
    return tuple(rows)


def _forbidden_output_scan(variant_root: pathlib.Path) -> ForbiddenOutputScanResult:
    """Walk the written variant tree and run the §8.1 forbidden-output gate.

    Passes the actual on-disk file set to
    :func:`verify_no_forbidden_runtime_producer_artifacts` with empty
    :class:`DbWriteSpy` + :class:`CycleLineageSpy` — the CLI writes no
    DB rows and no cycle-lineage records by design, so the empty spies
    are the honest boundary-monitor proof.

    The full §8.1 written-tree scan (SUB-4, §2.3) will land the
    injected-forbidden-artifact negative path; here we only run the
    gate on the clean set so SUB-2's stage chain has the required
    :class:`ForbiddenOutputScanResult` for the evidence bundle.
    """
    emitted = sorted(
        (p for p in variant_root.rglob("*") if p.is_file()),
        key=lambda p: str(p),
    )
    return verify_no_forbidden_runtime_producer_artifacts(
        emitted,
        db_write_spy=DbWriteSpy(),
        cycle_lineage_spy=CycleLineageSpy(),
    )


def _write_manifest(variant_root: pathlib.Path, manifest: DirectGridManifest) -> None:
    """Serialize the manifest to ``variant_root/manifest.json`` via the shared authority."""
    payload = manifest.to_resource_profile_dict()
    (variant_root / _MANIFEST_FILENAME).write_bytes(canonical_json_bytes(payload))


def _write_binding_artifact(
    variant_root: pathlib.Path, binding_artifact: BindingArtifact
) -> None:
    """Persist the standalone binding artifact bytes verbatim.

    The bytes are the library's canonical JSON serialization (from
    :func:`packages.common.grid_signature.canonical_json_bytes` via
    :func:`workers.mapping_builder.binding.emit_direct_grid_manifest_and_binding`),
    so writing ``binding_artifact.bytes`` verbatim preserves the checksum
    invariant ``binding_artifact.checksum == SHA-256(binding_artifact.bytes)``.
    """
    (variant_root / _BINDING_FILENAME).write_bytes(binding_artifact.bytes)


# --- orchestration --------------------------------------------------------


def build_direct_grid_variant(
    *,
    baseline_root: pathlib.Path,
    variant_root: pathlib.Path,
    source_id: str,
    grid_id: str,
    grid_snapshot_loader: GridSnapshotLoader,
    snapshot_cells: Sequence[CanonicalGridCell],
    grid_snapshot_reference: GridSnapshotReference,
    mapping_asset_identity: str,
    model_input_package_id: str,
    binding_uri: str,
    sp_att_manifest_path: str,
    category_files: Mapping[str, Sequence[str]],
    state_schema_bytes: bytes,
    solver_config_bytes: bytes,
    domain_shp_path: pathlib.Path,
    proj_crs_database_version: str,
    approvals: Approvals,
    rollback_target: RollbackTarget,
    distance_qa: DistanceQA,
    capacity_report: CapacityReport,
    applicable_source_ids: Sequence[str] | None = None,
    active_forcing_subdir: str = "input",
    small_basin_approval: SmallBasinApproval | None = None,
    z_policy_verdict_path: pathlib.Path | None = None,
) -> BuildResult:
    """Chain G0..G5 mapping stages, emit the variant, and assemble evidence.

    Stage order (asserted by the ``stage_order`` test): G0
    :func:`verify_g0_baseline` -> G1
    :func:`verify_g1_non_degenerate_triangles` ->
    :func:`verify_package_crs` -> G2/ownership
    :func:`nearest_cell_barycenter_geodesic_v1` -> G3
    :func:`derive_used_cell_subset` + :func:`assign_shud_forcing_index`
    + :func:`verify_small_basin_gate` -> z-policy chain
    (:func:`resolve_verdict` -> :func:`build_z_policy` ->
    :func:`sample_per_cell_z` -> :func:`dataclasses.replace`) -> G4
    :func:`copy_and_rewrite_sp_att_forc` + non-FORC / §3.3 / §3.4 / §3.5
    verifications -> G5
    :func:`emit_direct_grid_manifest_and_binding` -> §8.1 scan on the
    written tree -> :func:`assemble_evidence_package` with G0..G5
    :class:`GateResult` records (the G5 slot carries the z-policy
    sampler rule id + verdict resolution per tasks.md §2.1).

    Fail-closed: any raise propagates AFTER ``shutil.rmtree`` on the
    ``<variant_root>.building`` staging directory; ``variant_root`` is
    never created. Caller MUST supply a fresh ``variant_root`` path
    (``ValueError`` on collision).

    Parameters accepted verbatim from the caller (no derivation): the
    CLI stays a thin chain per Decision 3, so the caller pre-computes
    ``distance_qa``, ``capacity_report``, ``proj_crs_database_version``,
    ``approvals``, ``rollback_target``, and the ``category_files`` +
    ``state_schema_bytes`` + ``solver_config_bytes`` fingerprint inputs.

    Raises the typed error families from each stage: G0
    :class:`BaselineIntegrityError`, G1 collinear/degenerate errors, G2
    :class:`MappingAlgorithmError` family, G3
    :class:`SmallBasinBlockedError`, z-policy
    :class:`VerdictResolutionError`, G4
    :class:`SpAttRewriteError` /
    :class:`HydrologicCoreFingerprintMismatchError` /
    :class:`LegacyWeatherPathInActiveTreeError`, G5
    :class:`BindingArtifactError` (and subclasses). See each stage
    module's own docstring for the full contract.
    """
    if variant_root.exists():
        raise ValueError(
            f"variant_root already exists: {variant_root!s}; "
            "the CLI atomic-rename discipline requires a fresh target path"
        )
    if applicable_source_ids is None:
        applicable_source_ids = (source_id,)

    tmp_variant_root = variant_root.with_name(variant_root.name + ".building")
    if tmp_variant_root.exists():
        # Left over from a crashed prior run; safe to remove because it is
        # never the ``variant_root`` (the atomic rename would have moved it).
        shutil.rmtree(tmp_variant_root)

    try:
        # Copy the baseline tree in one shot so every non-.sp.att category
        # file exists at the same relative path under the variant root —
        # the §3.3 equality gate hashes per-relative-path bytes on both
        # sides, and a full copy makes them byte-identical by construction.
        # Included inside the try/except so a mid-copy failure (disk full /
        # EIO / KeyboardInterrupt) leaves no ``.building`` residue behind.
        shutil.copytree(baseline_root, tmp_variant_root)
        result = _run_gates_and_emit(
            baseline_root=baseline_root,
            tmp_variant_root=tmp_variant_root,
            source_id=source_id,
            grid_id=grid_id,
            grid_snapshot_loader=grid_snapshot_loader,
            snapshot_cells=snapshot_cells,
            grid_snapshot_reference=grid_snapshot_reference,
            mapping_asset_identity=mapping_asset_identity,
            model_input_package_id=model_input_package_id,
            binding_uri=binding_uri,
            sp_att_manifest_path=sp_att_manifest_path,
            category_files=category_files,
            state_schema_bytes=state_schema_bytes,
            solver_config_bytes=solver_config_bytes,
            domain_shp_path=domain_shp_path,
            proj_crs_database_version=proj_crs_database_version,
            approvals=approvals,
            rollback_target=rollback_target,
            distance_qa=distance_qa,
            capacity_report=capacity_report,
            applicable_source_ids=tuple(applicable_source_ids),
            active_forcing_subdir=active_forcing_subdir,
            small_basin_approval=small_basin_approval,
            z_policy_verdict_path=z_policy_verdict_path,
        )
    except BaseException:
        # No-partial-output: any raise (including KeyboardInterrupt /
        # SystemExit) MUST leave zero variant artifacts behind. Errors
        # during cleanup themselves are swallowed via ignore_errors=True
        # so the original build failure propagates un-shadowed.
        shutil.rmtree(tmp_variant_root, ignore_errors=True)
        raise

    # Atomic rename: on POSIX filesystems this is a single directory
    # rename syscall; a concurrent reader either sees the pre-rename
    # (nonexistent) or post-rename (fully-populated) state.
    os.rename(tmp_variant_root, variant_root)

    return dataclasses.replace(result, variant_root=variant_root)


def _run_gates_and_emit(
    *,
    baseline_root: pathlib.Path,
    tmp_variant_root: pathlib.Path,
    source_id: str,
    grid_id: str,
    grid_snapshot_loader: GridSnapshotLoader,
    snapshot_cells: Sequence[CanonicalGridCell],
    grid_snapshot_reference: GridSnapshotReference,
    mapping_asset_identity: str,
    model_input_package_id: str,
    binding_uri: str,
    sp_att_manifest_path: str,
    category_files: Mapping[str, Sequence[str]],
    state_schema_bytes: bytes,
    solver_config_bytes: bytes,
    domain_shp_path: pathlib.Path,
    proj_crs_database_version: str,
    approvals: Approvals,
    rollback_target: RollbackTarget,
    distance_qa: DistanceQA,
    capacity_report: CapacityReport,
    applicable_source_ids: tuple[str, ...],
    active_forcing_subdir: str,
    small_basin_approval: SmallBasinApproval | None,
    z_policy_verdict_path: pathlib.Path | None,
) -> BuildResult:
    """Run every gate and emit artifacts into ``tmp_variant_root``.

    Split out from :func:`build_direct_grid_variant` so the atomic
    rename + cleanup wrapper stays a single try/except block whose
    scope is exactly the mutation of ``tmp_variant_root``.
    """
    # --- G0/G1/CRS -----------------------------------------------------
    g0_report: BaselineIntegrityReport = verify_g0_baseline(baseline_root)
    g1_report: G1NonDegenerateReport = verify_g1_non_degenerate_triangles(baseline_root)
    crs_report: PackageCrsReport = verify_package_crs(baseline_root)

    # --- G2 + ownership ------------------------------------------------
    ownerships = nearest_cell_barycenter_geodesic_v1(
        baseline_root, source_id, grid_id, grid_snapshot_loader
    )

    # --- G3 used-cell subset -------------------------------------------
    used_cells = derive_used_cell_subset(ownerships, snapshot_cells)
    shud_forcing_index = assign_shud_forcing_index(used_cells)
    verify_small_basin_gate(used_cells, approval=small_basin_approval)

    # --- z_policy chain (SUB-1 wiring) --------------------------------
    verdict_resolution = resolve_verdict(explicit_path=z_policy_verdict_path)
    z_policy_skeleton: ZPolicy = build_z_policy(verdict_resolution)
    projection = PackageProjection.from_prj_wkt(crs_report.wkt)
    mesh_nodes = _parse_mesh_nodes(baseline_root)
    used_cells_for_sampler = _adapt_used_cells_for_sampler(used_cells)
    per_cell_z = sample_per_cell_z(used_cells_for_sampler, mesh_nodes, projection)
    z_policy = dataclasses.replace(z_policy_skeleton, per_cell_z=per_cell_z)

    # --- G4 .sp.att rewrite -------------------------------------------
    baseline_sp_att = g0_report.sp_att_path
    # Mirror the baseline layout in the variant tree — copytree already
    # put a copy at the same relative path; overwrite it in-place with
    # the rewritten bytes so downstream category-checksum + fingerprint
    # gates hash the exact new .sp.att.
    variant_sp_att = tmp_variant_root / baseline_sp_att.relative_to(baseline_root)
    sp_att_report: SpAttRewriteReport = copy_and_rewrite_sp_att_forc(
        baseline_att_path=baseline_sp_att,
        variant_att_path=variant_sp_att,
        ownership=ownerships,
        shud_forcing_index=shud_forcing_index,
        used_cell_count=len(used_cells),
    )
    verify_non_forc_columns_unchanged(baseline_sp_att, variant_sp_att)

    # --- G4 category equality + fingerprint + legacy-weather ----------
    verify_non_sp_att_checksums_equal(
        baseline_root,
        tmp_variant_root,
        category_files=category_files,
    )
    hydrologic_fingerprint = verify_hydrologic_core_fingerprint_equal(
        baseline_root,
        tmp_variant_root,
        baseline_sp_att_path=baseline_sp_att,
        variant_sp_att_path=variant_sp_att,
        category_files=category_files,
        baseline_state_schema_bytes=state_schema_bytes,
        variant_state_schema_bytes=state_schema_bytes,
        baseline_solver_config_bytes=solver_config_bytes,
        variant_solver_config_bytes=solver_config_bytes,
    )
    # Active-forcing subtree must exist for the §3.5 gate; create it
    # empty if the baseline / variant clone did not carry one (the
    # keliya fixture has none).
    active_dir = tmp_variant_root / active_forcing_subdir
    active_dir.mkdir(parents=True, exist_ok=True)
    verify_no_legacy_weather_path_in_active_tree(
        tmp_variant_root, active_forcing_subdir=active_forcing_subdir
    )

    # --- G5 manifest + binding emission -------------------------------
    sp_att_bytes = variant_sp_att.read_bytes()
    manifest, binding_artifact = emit_direct_grid_manifest_and_binding(
        used_cells=used_cells,
        snapshot_cells=snapshot_cells,
        shud_forcing_index=shud_forcing_index,
        mapping_asset_identity=mapping_asset_identity,
        model_input_package_id=model_input_package_id,
        sp_att_path=sp_att_manifest_path,
        sp_att_bytes=sp_att_bytes,
        applicable_source_ids=applicable_source_ids,
        grid_id=grid_id,
        grid_signature=grid_snapshot_reference.grid_signature,
        z_policy=z_policy,
        binding_uri=binding_uri,
        model_crs_wkt=crs_report.wkt,
    )

    # Persist the emitted artifacts to disk BEFORE running the
    # written-tree §8.1 scan so the scan sees every file the CLI wrote.
    _write_manifest(tmp_variant_root, manifest)
    _write_binding_artifact(tmp_variant_root, binding_artifact)

    # --- §8.1 forbidden-output scan on the written tree ---------------
    forbidden_scan = _forbidden_output_scan(tmp_variant_root)

    # --- Evidence assembly --------------------------------------------
    # Reuse the header-verified library parser so the ownership evidence
    # bundle records the true FORC column even when the .sp.att carries a
    # legal but non-canonical column order (e.g. FORC not at index 4).
    baseline_forc_rows = parse_sp_att_forc_rows(baseline_sp_att)
    baseline_forc_by_element: dict[int, int] = {
        row.element_id: row.forc for row in baseline_forc_rows
    }
    ownership_rows = _build_ownership_rows(
        ownerships=ownerships,
        shud_forcing_index=shud_forcing_index,
        baseline_forc_by_element=baseline_forc_by_element,
    )
    ownership_images = render_ownership_images(domain_shp_path, ownership_rows)

    baseline_identity = BaselineIdentity(
        package_sha256_hex=g0_report.package_checksum,
        sp_att_sha256_hex=sp_att_report.checksums.baseline_sha256,
        sp_mesh_sha256_hex=_sha256_file(g0_report.sp_mesh_path),
    )

    gate_results = GateResults(
        g0=GateResult(
            gate_id="G0",
            passed=True,
            evidence_ref={
                "kind": "baseline_integrity_report",
                "checksum": g0_report.package_checksum,
            },
        ),
        g1=GateResult(
            gate_id="G1",
            passed=True,
            evidence_ref={
                "kind": "g1_non_degenerate",
                "element_count": g1_report.element_count,
                "min_observed_area": g1_report.min_observed_area,
                "tolerance": g1_report.tolerance,
            },
        ),
        g2=GateResult(
            gate_id="G2",
            passed=True,
            evidence_ref={
                "kind": "ownership_algorithm",
                "algorithm": ALGORITHM_ID,
            },
        ),
        g3=GateResult(
            gate_id="G3",
            passed=True,
            evidence_ref={
                "kind": "hydrologic_core_fingerprint_equal",
                "fingerprint_hash": hydrologic_fingerprint.hash,
            },
        ),
        g4=GateResult(
            gate_id="G4",
            passed=True,
            evidence_ref={
                "kind": "sp_att_rewrite",
                "rewritten_row_count": sp_att_report.rewritten_row_count,
                "used_cell_count": sp_att_report.used_cell_count,
            },
        ),
        g5=GateResult(
            gate_id="G5",
            passed=True,
            evidence_ref={
                "kind": "binding_g5",
                "binding_checksum": binding_artifact.checksum,
                "sampler_rule_id": z_policy_verdict.SAMPLER_RULE_ID,
                "verdict_resolution": {
                    "resolved_path": str(verdict_resolution.resolved_path),
                    "verified_sha256": verdict_resolution.verified_sha256,
                    "override_used": verdict_resolution.override_used,
                },
            },
        ),
    )

    evidence_package: EvidencePackage = assemble_evidence_package(
        baseline_identity=baseline_identity,
        grid_snapshot_reference=grid_snapshot_reference,
        ownership_table=ownership_rows,
        manifest=manifest,
        binding_artifact=binding_artifact,
        sp_att_asset_diff=SpAttAssetDiff(
            old_sha256_hex=sp_att_report.checksums.baseline_sha256,
            new_sha256_hex=sp_att_report.checksums.variant_sha256,
            semantic_diff_summary=sp_att_report.semantic_diff,
        ),
        mapping_algorithm_identity=MappingAlgorithmIdentity(
            algorithm_id=ALGORITHM_ID,
            proj_crs_database_version=proj_crs_database_version,
        ),
        hydrologic_core_fingerprint=hydrologic_fingerprint,
        forbidden_output_scan=forbidden_scan,
        distance_qa=distance_qa,
        capacity_report=capacity_report,
        gate_results=gate_results,
        ownership_images=ownership_images,
        approvals=approvals,
        rollback_target=rollback_target,
        build_timestamp=None,  # §2.4 no-wall-clock discipline
    )

    return BuildResult(
        variant_root=tmp_variant_root,  # replaced with final path by the caller
        manifest=manifest,
        binding_artifact=binding_artifact,
        evidence_package=evidence_package,
        verdict_resolution=verdict_resolution,
    )


# --- argparse scaffold ----------------------------------------------------


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    """Argparse scaffold — full operator surface lands in SUB-3.

    This scaffold parses the future operator flag surface so a callsite
    smoke-invocation returns a stable exit code + JSON diagnostic rather
    than crashing with an argparse abort. The actual snapshot-loader
    wiring + object-store path resolver are §2.2 (SUB-3) scope; this
    scaffold refuses to attempt a build until that lands.
    """
    parser = argparse.ArgumentParser(
        prog="nhms-mapping-build",
        description=(
            "Direct-grid mapping-asset builder CLI. "
            "The operator argv surface lands in SUB-3 (§2.2 path resolver)."
        ),
    )
    parser.add_argument("--baseline-root", required=False, default=None)
    parser.add_argument("--variant-root", required=False, default=None)
    parser.add_argument("--object-store-root", required=False, default=None)
    parser.add_argument("--allow-dev-workspace", action="store_true")
    args = parser.parse_args(argv)

    diagnostic = {
        "status": "unimplemented",
        "reason": (
            "operator argv path-authority resolver + snapshot-loader "
            "wiring are deferred to SUB-3 (§2.2). Direct invocation of "
            "the library orchestration function "
            "`workers.mapping_builder.cli.build_direct_grid_variant` is "
            "available for programmatic callers."
        ),
        "received": {
            "baseline_root": args.baseline_root,
            "variant_root": args.variant_root,
            "object_store_root": args.object_store_root,
            "allow_dev_workspace": args.allow_dev_workspace,
        },
    }
    print(
        json.dumps(diagnostic, ensure_ascii=False, sort_keys=True),
        file=sys.stderr,
    )
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    """Argparse entrypoint — thin wrapper for typed error family catches.

    The typed exception families raised by :func:`build_direct_grid_variant`
    surface here with a non-zero exit code + JSON stderr diagnostic once
    SUB-3 wires the operator argv path.
    """
    try:
        return _argparse_main(argv)
    except (VerdictResolutionError, BindingArtifactError) as error:
        # Reserved catches — VerdictResolutionError is the SUB-1 error
        # family; BindingArtifactError is representative of the G5 family.
        # SUB-3 will extend this to include the full G0..G4 error families
        # once the argv wiring lands.
        print(
            json.dumps(
                {"status": "error", "error": type(error).__name__, "message": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover — scaffold for SUB-3
    raise SystemExit(main())
