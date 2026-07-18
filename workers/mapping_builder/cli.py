"""§2.1 + §2.2 mapping builder CLI: chain G0-G5 stages + input-authority resolver.

Thin operator entrypoint (Epic #973 SUB-2 and SUB-3, OpenSpec change
``direct-grid-build-enablement``). Chains the existing
``workers.mapping_builder`` library stages plus the SUB-1
:mod:`workers.mapping_builder.z_policy_verdict` module. This module
NEVER re-implements any G0..G5 stage; every gate is delegated to the
library import. The import-site + no-local-def guardrail is enforced
by ``tests/test_mapping_builder_cli.py::test_no_duplicated_stage_logic_all_stage_functions_come_from_stage_modules``.

SUB-3 (§2.2) adds an operator-argv input-authority resolver
(:func:`resolve_package_path`) that validates the operator-supplied
``--package-path`` against the object-store release-frozen shape
``<object-store-root>/models/basins_<basin>_shud/<release>/package/``
(root defaults to ``/home/ghdc/nwm/object-store`` and is overridable
via ``--object-store-root``) and refuses dev-workspace paths (node-27
``/home/ghdc/nwm/Basins/...`` and node-22 ``/volume/nwm/Basins/...``)
unless ``--allow-dev-workspace`` is set with a non-empty
``--dev-workspace-rationale``. Any override is recorded on the
returned :class:`ResolvedPackagePath.input_authority_evidence`
mapping, which the caller merges into G0's ``evidence_ref`` via
:func:`build_direct_grid_variant`'s ``input_authority_evidence``
kwarg. No filesystem read happens during resolution — a rejected
path fails closed with no read and no output. The resolver validates
the path shape/authority only; it does not re-implement package
loading (SUB-4/SUB-5 keep that boundary).

Deferred: SUB-4 (§2.3 written-tree forbidden-output scan) and SUB-5
(§2.4 keliya deterministic 2-run byte compare routed through the
SUB-3 resolver). The SUB-3 argparse ``main`` resolves the path and
emits the resolution JSON on stdout — it does NOT drive a build (the
full ``build_direct_grid_variant`` invocation from argv is SUB-5
territory once the keliya fixture is staged under a tmp
``--object-store-root``). Programmatic callers (tests, downstream
orchestrators) invoke :func:`build_direct_grid_variant` directly.

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
import math
import os
import pathlib
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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
    ForbiddenOutputClass,
    ForbiddenOutputScanResult,
    ForbiddenRuntimeProducerArtifactError,
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

# --- §2.2 input-authority resolver constants -------------------------------

#: Default object-store root under which operator-supplied package paths must
#: sit unless ``--object-store-root`` overrides. Hardcoded at the node-27
#: production layout; does not exist on local/CI runners — tests point the
#: root at a tmp directory via the sanctioned ``--object-store-root`` channel.
DEFAULT_OBJECT_STORE_ROOT = pathlib.Path("/home/ghdc/nwm/object-store")

#: Recognized dev-workspace path prefixes that :func:`resolve_package_path`
#: REJECTS by default. ``/home/ghdc/nwm/Basins`` is the node-27 dev tree;
#: ``/volume/nwm/Basins`` is the node-22 dev tree. Only unblocked when the
#: operator opts in via ``--allow-dev-workspace`` with a non-empty rationale.
_DEV_WORKSPACE_PREFIXES: tuple[pathlib.Path, ...] = (
    pathlib.Path("/home/ghdc/nwm/Basins"),
    pathlib.Path("/volume/nwm/Basins"),
)


# --- §2.2 input-authority resolver ----------------------------------------


class PackagePathAuthorityError(RuntimeError):
    """Raised when the operator-supplied package path fails §2.2 authority validation.

    Distinct root (not a subclass of any G0..G5 or z-policy exception
    family) — path-authority failures come from the argv resolver, not
    from any downstream gate. The resolver fails closed with NO
    filesystem read and NO output written; the caller can catch this
    error to emit a top-level operator diagnostic without leaking any
    partial artifact.
    """


@dataclass(frozen=True)
class ResolvedPackagePath:
    """Return record from :func:`resolve_package_path`.

    Attributes
    ----------
    baseline_root:
        The validated package path (identical to the operator-supplied
        ``package_path``). Returned as a distinct field so downstream
        callers see a clearly-named "baseline_root" boundary rather than
        threading the raw operator argv value through gate calls.
    basin_name:
        Basin identifier extracted from the ``basins_<basin>_shud``
        segment for object-store shapes, or the first path segment
        under the dev-workspace prefix.
    release_id:
        Release identifier from the object-store shape's ``<release>``
        segment. For dev-workspace overrides this is the sentinel
        ``"dev-workspace"`` (no release channel exists in the dev tree).
    input_authority_evidence:
        Structured pointer to the input-authority decision, mergeable
        into G0's ``evidence_ref`` via
        :func:`build_direct_grid_variant`'s ``input_authority_evidence``
        kwarg. Contains ``kind``, ``package_path``, ``basin_name``,
        ``release_id``, ``channel`` (``"object_store"`` or
        ``"dev_workspace"``), ``object_store_root``,
        ``object_store_root_override`` (bool), and
        ``dev_workspace_override`` (``None`` or ``{"path": ...,
        "rationale": ...}``).
    """

    baseline_root: pathlib.Path
    basin_name: str
    release_id: str
    input_authority_evidence: Mapping[str, Any]


def resolve_package_path(
    *,
    package_path: pathlib.Path,
    object_store_root: pathlib.Path | None = None,
    allow_dev_workspace: bool = False,
    dev_workspace_rationale: str | None = None,
) -> ResolvedPackagePath:
    """Validate operator-supplied package path against §2.2 input-authority discipline.

    Accepted shapes:

    * Object-store release-frozen:
      ``<effective_root>/models/basins_<basin>_shud/<release>/package``
      where ``effective_root`` is ``object_store_root`` if supplied, else
      :data:`DEFAULT_OBJECT_STORE_ROOT`. A non-default root is recorded
      on the evidence as ``object_store_root_override=True``.
    * Dev-workspace path under any prefix in :data:`_DEV_WORKSPACE_PREFIXES`
      — REJECTED unless ``allow_dev_workspace`` is True AND
      ``dev_workspace_rationale`` is a non-empty string; the override
      path + rationale are recorded on the evidence.

    Any other path fails closed with :class:`PackagePathAuthorityError`
    and NO filesystem read (the resolver only inspects path segments).

    Parameters
    ----------
    package_path:
        Operator-supplied package path (typically from ``--package-path``).
    object_store_root:
        Override for :data:`DEFAULT_OBJECT_STORE_ROOT`. When ``None``,
        the default is used; when non-``None``, the override is recorded
        on the returned evidence dict.
    allow_dev_workspace:
        Whether to accept dev-workspace paths. Defaults to ``False``.
    dev_workspace_rationale:
        Required non-empty string when ``allow_dev_workspace`` is True.

    Raises
    ------
    PackagePathAuthorityError:
        On any path that is neither object-store-shaped under the
        configured root nor a recognized dev-workspace path; on a
        dev-workspace path when ``allow_dev_workspace`` is not set; on
        a dev-workspace override without a non-empty rationale.
    """
    effective_root = (
        object_store_root
        if object_store_root is not None
        else DEFAULT_OBJECT_STORE_ROOT
    )
    is_override = object_store_root is not None
    effective_root_str = str(effective_root)

    # Object-store shape check via pathlib parts (no regex, no filesystem read).
    try:
        rel = package_path.relative_to(effective_root)
    except ValueError:
        rel = None

    if rel is not None:
        parts = rel.parts
        # Expected relative shape: ("models", "basins_<basin>_shud", "<release>", "package")
        if (
            len(parts) == 4
            and parts[0] == "models"
            and parts[1].startswith("basins_")
            and parts[1].endswith("_shud")
            and len(parts[1]) > len("basins__shud")  # non-empty basin name
            and parts[3] == "package"
        ):
            basin_name = parts[1][len("basins_"):-len("_shud")]
            release_id = parts[2]
            evidence = {
                "kind": "input_authority",
                "package_path": str(package_path),
                "basin_name": basin_name,
                "release_id": release_id,
                "channel": "object_store",
                "object_store_root": effective_root_str,
                "object_store_root_override": is_override,
                "dev_workspace_override": None,
            }
            return ResolvedPackagePath(
                baseline_root=package_path,
                basin_name=basin_name,
                release_id=release_id,
                input_authority_evidence=evidence,
            )

    # Dev-workspace check.
    for prefix in _DEV_WORKSPACE_PREFIXES:
        try:
            rel_dev = package_path.relative_to(prefix)
        except ValueError:
            continue
        # Path is under a dev-workspace prefix — authority decision below.
        if not allow_dev_workspace:
            raise PackagePathAuthorityError(
                f"package_path {package_path!s}: dev-workspace paths under "
                f"{prefix!s} are rejected by default; supply "
                "--allow-dev-workspace with --dev-workspace-rationale to "
                "override (the override is recorded on the evidence bundle)"
            )
        if dev_workspace_rationale is None or not dev_workspace_rationale.strip():
            raise PackagePathAuthorityError(
                f"package_path {package_path!s}: --allow-dev-workspace "
                "requires a non-empty --dev-workspace-rationale so the "
                "override is auditable on the evidence bundle"
            )
        basin_name = rel_dev.parts[0] if rel_dev.parts else ""
        release_id = "dev-workspace"
        evidence = {
            "kind": "input_authority",
            "package_path": str(package_path),
            "basin_name": basin_name,
            "release_id": release_id,
            "channel": "dev_workspace",
            "object_store_root": effective_root_str,
            "object_store_root_override": is_override,
            "dev_workspace_override": {
                "path": str(package_path),
                "rationale": dev_workspace_rationale,
            },
        }
        return ResolvedPackagePath(
            baseline_root=package_path,
            basin_name=basin_name,
            release_id=release_id,
            input_authority_evidence=evidence,
        )

    # Neither shape — fail closed with no read, no output.
    raise PackagePathAuthorityError(
        f"package_path {package_path!s}: is neither an object-store "
        f"release-frozen path under "
        f"{effective_root_str}/models/basins_<basin>_shud/<release>/package/ "
        f"nor a recognized dev-workspace path under "
        f"{tuple(str(p) for p in _DEV_WORKSPACE_PREFIXES)!r}; §2.2 fails "
        "closed with no read, no output"
    )


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
        raw_elevation = tokens[elevation_col_index]
        try:
            elevation = float(raw_elevation)
        except ValueError:
            if raw_elevation.strip().upper() in {"NA", "N/A", "NULL", "NAN"}:
                continue
            raise
        if not math.isfinite(elevation):
            continue
        nodes.append(
            MeshNode(
                node_id=node_id,
                x=float(x),
                y=float(y),
                elevation=elevation,
            )
        )
    if not nodes:
        raise ValueError(f"{mesh_path.name}: no finite Elevation values are available for z sampling")
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

    SUB-4 (§2.3) lands the injected-forbidden-artifact negative path
    alongside the happy-path scan: any offending on-disk file surfaces
    via the library's own
    :class:`ForbiddenRuntimeProducerArtifactError` (raised inline
    before this function returns), and the caller's outer try/except
    catches it and removes ``<variant_root>.building`` so no partial
    variant is committed.
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


def _raise_forbidden_output_from_scan_result(
    scan_result: ForbiddenOutputScanResult,
) -> None:
    """Re-raise the §8.1 fail-closed exception from a red scan result.

    Reserved for the defense-in-depth branch in
    :func:`_run_gates_and_emit`: if a future refactor of
    :func:`verify_no_forbidden_runtime_producer_artifacts` returns
    ``passed=False`` without raising, this helper reconstructs the
    same :class:`ForbiddenRuntimeProducerArtifactError` the library
    would have raised, picking the first offender across the three
    non-empty tuples for a concrete diagnostic. The full 4-class
    breakdown remains on :attr:`ForbiddenOutputScanResult` for
    downstream evidence.
    """
    if scan_result.offending_paths:
        first_class, first_path = scan_result.offending_paths[0]
        raise ForbiddenRuntimeProducerArtifactError(
            offending_class=first_class,
            offending_evidence=first_path,
            scan_summary=scan_result,
        )
    if scan_result.offending_db_writes:
        first_table, first_row = scan_result.offending_db_writes[0]
        raise ForbiddenRuntimeProducerArtifactError(
            offending_class=ForbiddenOutputClass.MET_ROW_WRITE.value,
            offending_evidence=(first_table, first_row),
            scan_summary=scan_result,
        )
    # cycle_lineage_records — the only remaining path.
    raise ForbiddenRuntimeProducerArtifactError(
        offending_class=ForbiddenOutputClass.CYCLE_LINEAGE_RECORD.value,
        offending_evidence=scan_result.cycle_lineage_records[0],
        scan_summary=scan_result,
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
    input_authority_evidence: Mapping[str, Any] | None = None,
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

    Parameters
    ----------
    input_authority_evidence:
        Optional §2.2 input-authority record (produced by
        :func:`resolve_package_path`). When supplied, its contents are
        merged into G0's ``evidence_ref`` under the ``"input_authority"``
        key so the evidence bundle carries the operator's argv-path
        decision (channel, object-store root override, dev-workspace
        override) alongside the baseline integrity checksum. When
        ``None`` (programmatic callers that bypass the argv resolver),
        the G0 evidence_ref keeps its SUB-2 shape unchanged.
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
            input_authority_evidence=input_authority_evidence,
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
    input_authority_evidence: Mapping[str, Any] | None,
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
    # Defense-in-depth SUB-4 (§2.3) fail-closed re-raise. The library
    # :func:`verify_no_forbidden_runtime_producer_artifacts` already
    # raises :class:`ForbiddenRuntimeProducerArtifactError` on any
    # violation before returning — so ``forbidden_scan.passed`` is
    # always ``True`` here today. This explicit check guards against a
    # future library refactor that returns ``passed=False`` without
    # raising: the CLI's §2.3 promise (no variant + no `.building`
    # residue when any forbidden runtime-producer artifact appears in
    # the written tree) MUST NOT depend on whether the library raises
    # inline or returns a red result. The raise falls into the outer
    # try/except in :func:`build_direct_grid_variant` which removes
    # ``<variant_root>.building`` before the exception propagates.
    if forbidden_scan.passed is False:
        _raise_forbidden_output_from_scan_result(forbidden_scan)

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

    # G0 evidence_ref: merge SUB-3 §2.2 input-authority record when the
    # caller resolved the path via :func:`resolve_package_path`. When
    # ``input_authority_evidence`` is None (programmatic tests that
    # bypass argv), the evidence_ref keeps its SUB-2 shape.
    g0_evidence_ref: dict[str, Any] = {
        "kind": "baseline_integrity_report",
        "checksum": g0_report.package_checksum,
    }
    if input_authority_evidence is not None:
        g0_evidence_ref["input_authority"] = dict(input_authority_evidence)

    gate_results = GateResults(
        g0=GateResult(
            gate_id="G0",
            passed=True,
            evidence_ref=g0_evidence_ref,
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


# --- §2.2 argparse entrypoint (SUB-3) -------------------------------------


def _argparse_main(argv: Sequence[str] | None = None) -> int:
    """Argparse main — SUB-3 (§2.2) input-authority resolver + JSON emission.

    Parses the operator argv surface, drives :func:`resolve_package_path`,
    and emits the resolution decision as JSON on stdout. Does NOT drive
    a build — the full ``build_direct_grid_variant`` invocation from
    argv (loader wiring, snapshot construction, approvals) lands in
    SUB-5 alongside the keliya deterministic 2-run fixture. The
    resolution JSON stdout stream is the contract SUB-5 consumes when
    it drives builds through this same argv surface.

    Exit codes
    ----------
    0
        Path resolution succeeded; JSON payload on stdout carries the
        resolved basin, release, effective object-store root, and the
        ``input_authority_evidence`` dict ready to be merged into G0.
    1
        Path resolution failed (:class:`PackagePathAuthorityError`);
        JSON diagnostic on stderr carries the error class + message.
    2
        Argparse itself rejected the argv (unknown flag, missing
        required flag). argparse writes its own diagnostic to stderr.
    """
    parser = argparse.ArgumentParser(
        prog="nhms-mapping-build",
        description=(
            "Direct-grid mapping-asset builder CLI. "
            "§2.2 input-authority resolver: validates --package-path "
            "against the object-store release-frozen shape "
            "<object-store-root>/models/basins_<basin>_shud/<release>/package."
        ),
    )
    parser.add_argument("--package-path", required=True, type=pathlib.Path)
    parser.add_argument("--variant-root", required=True, type=pathlib.Path)
    parser.add_argument(
        "--object-store-root",
        required=False,
        default=None,
        type=pathlib.Path,
        help=(
            "Override the default object-store root "
            f"({DEFAULT_OBJECT_STORE_ROOT!s}); non-default use is recorded "
            "on the evidence bundle."
        ),
    )
    parser.add_argument(
        "--allow-dev-workspace",
        action="store_true",
        help=(
            "Accept a dev-workspace package path (node-27 "
            "/home/ghdc/nwm/Basins/... or node-22 /volume/nwm/Basins/...); "
            "requires --dev-workspace-rationale."
        ),
    )
    parser.add_argument(
        "--dev-workspace-rationale",
        required=False,
        default=None,
        help=(
            "Non-empty rationale string recorded on the evidence bundle "
            "when --allow-dev-workspace is set."
        ),
    )
    args = parser.parse_args(argv)

    try:
        resolved = resolve_package_path(
            package_path=args.package_path,
            object_store_root=args.object_store_root,
            allow_dev_workspace=args.allow_dev_workspace,
            dev_workspace_rationale=args.dev_workspace_rationale,
        )
    except PackagePathAuthorityError as error:
        print(
            json.dumps(
                {
                    "status": "error",
                    "error": type(error).__name__,
                    "message": str(error),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    resolution_payload = {
        "status": "resolved",
        "baseline_root": str(resolved.baseline_root),
        "basin_name": resolved.basin_name,
        "release_id": resolved.release_id,
        "variant_root": str(args.variant_root),
        "input_authority_evidence": dict(resolved.input_authority_evidence),
    }
    print(json.dumps(resolution_payload, ensure_ascii=False, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Argparse entrypoint — thin wrapper for typed error family catches.

    The typed exception families raised by :func:`build_direct_grid_variant`
    surface here with a non-zero exit code + JSON stderr diagnostic once
    SUB-5 wires the argv path all the way through a build. Today SUB-3's
    argparse main handles :class:`PackagePathAuthorityError` inline
    (returning 1); the reserved catches here remain for the future
    build-driving wiring.
    """
    try:
        return _argparse_main(argv)
    except (VerdictResolutionError, BindingArtifactError) as error:
        # Reserved catches — VerdictResolutionError is the SUB-1 error
        # family; BindingArtifactError is representative of the G5 family.
        # SUB-5 will extend this to include the full G0..G4 error families
        # once the argv wiring drives a full build.
        print(
            json.dumps(
                {"status": "error", "error": type(error).__name__, "message": str(error)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover — driven by SUB-5 fixture
    raise SystemExit(main())
