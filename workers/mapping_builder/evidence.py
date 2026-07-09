"""§5.1 + §5.2 mapping evidence-package assembler (Epic #909 SUB-13, docs §14).

This module implements OpenSpec change ``forcing-mapping-asset-build`` §5.1
and §5.2 (Epic #909 SUB-13). It assembles the §14 immutable mapping evidence
package: baseline identity + grid snapshot reference + ownership table +
station binding rows + ``.sp.att`` asset diff + mapping-algorithm identity +
``hydrologic_core_fingerprint`` + distance QA + capacity report + G0–G5 gate
results + old/new ownership images + approvals + rollback target + evidence
checksum bound to the mapping-asset checksum.

The package is the durable audit surface for a mapping build: SUB-13 is the
sole authority that binds the individual SUB-8..12 gate receipts into one
immutable bundle whose checksum can be verified end-to-end without re-running
the upstream gates. Once a package is assembled it MUST NOT be mutated;
superseding a variant builds a NEW immutable package (per §5.2 non-goal).

Immutability discipline
-----------------------
:class:`EvidencePackage` is a frozen dataclass. Every support dataclass
(:class:`BaselineIdentity`, :class:`GridSnapshotReference`,
:class:`OwnershipRow`, :class:`SpAttAssetDiff`,
:class:`MappingAlgorithmIdentity`, :class:`DistanceQA`,
:class:`CapacityReport`, :class:`GateResult`, :class:`GateResults`,
:class:`OwnershipImages`, :class:`Approvals`, :class:`RollbackTarget`,
:class:`ReadinessManifest`) is also frozen so downstream evidence auditors
can bind every record byte-for-byte.

Mutating any field other than the enumerated
:data:`EVIDENCE_CHECKSUM_EXCLUDED_FIELDS` (``build_timestamp``,
``build_host``) invalidates :attr:`EvidencePackage.evidence_checksum`. The
canonical use pattern for "record but don't checksum" metadata is:

    dataclasses.replace(package, build_timestamp=new_ts)

which produces a NEW immutable package whose :attr:`evidence_checksum` is
byte-identical to the original (the excluded field never enters the digest
input). Callers who need to re-bind the mapping asset MUST call
:func:`bind_evidence_to_mapping_asset`, which returns a NEW package with
the updated ``bound_mapping_asset_checksum`` and a re-computed
``evidence_checksum``. The existing package is unchanged.

INV-3 discipline (domain.shp visualization-only)
------------------------------------------------
``domain.shp`` is used ONLY by :func:`render_ownership_images` to produce
the old/new ownership map images that ship with the evidence package. It
is NEVER an algorithm input — never a source of element IDs, mesh
topology, coverage bbox, or any other identity signal that would enter a
G0–G5 gate. INV-3 enforcement is code-review + regression tests; this
module surface hard-codes the visualization-only role by:

* accepting ``domain_shp_path`` **only** in :func:`render_ownership_images`;
* refusing to accept the path anywhere in :func:`assemble_evidence_package`
  (the ownership table + station bindings + snapshot reference are
  algorithm inputs — ``domain.shp`` is not);
* producing bytes-only :class:`OwnershipImages` output — the rendered
  images never round-trip back into any downstream gate.

``verify_*`` gate-naming convention
-----------------------------------
This module extends the ``verify_*`` prefix convention codified by
:mod:`workers.mapping_builder.rewrite` (Epic #909 SUB-8..SUB-11 review
loops converged on ``verify_*`` as the canonical fail-closed gate
namespace). The return value discriminates outcome:

* ``None`` iff the gate passed with no artifact needed
  (:func:`verify_evidence_checksum_binding`,
  :func:`verify_algorithm_and_proj_identity_matches_readiness`,
  :func:`verify_all_g0_through_g5_gates_passed`);
* ``raise`` iff the gate failed.

Computation-only helpers (:func:`compute_evidence_checksum`,
:func:`enumerate_checksum_excluded_fields`,
:func:`render_ownership_images`) use non-``verify_*`` verbs — they never
raise on "drift", they produce the artifact for a downstream ``verify_*``
gate to check.

Shared-authority reuse (Epic #909 SUB-11 CP-1)
----------------------------------------------
* :func:`packages.common.grid_signature.canonical_json_bytes` is the sole
  byte-deterministic serialization authority. This module NEVER
  hand-rolls ``json.dumps(sort_keys=True, ...)``.
* :data:`packages.common.grid_signature.COORDINATE_ROUNDING_DECIMALS` is
  the sole 12-decimal rounding rule if this module ever needs to round a
  coordinate (currently it does not — coordinates only enter as opaque
  strings in station binding rows produced by SUB-11).

Exception family
----------------
:class:`EvidencePackageError` is a distinct root — NOT a subclass of
:class:`workers.mapping_builder.integrity.BaselineIntegrityError` (G0/G1),
:class:`workers.mapping_builder.algorithm.MappingAlgorithmError` (G2/G3),
:class:`workers.mapping_builder.rewrite.SpAttRewriteError` (G4), or
:class:`workers.mapping_builder.binding.BindingArtifactError` (G5).
Evidence-package failures come from a different oracle (checksum binding,
identity cross-check with the readiness manifest, gate-result recording)
than any single upstream gate. Keeping the roots distinct lets callers
differentiate the five families with dedicated ``except`` clauses.

:class:`workers.mapping_builder.rewrite.HydrologicCoreFingerprintMismatchError`
is REUSED verbatim — this module never redefines it. SUB-13's evidence
bundler records the fingerprint from SUB-9's computation; a mismatch
surfaces as SUB-9's own exception with SUB-9's own error family.
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from packages.common.grid_signature import (
    canonical_json_bytes as _shared_canonical_json_bytes,
)
from workers.mapping_builder.binding import (
    BindingArtifact,
    DirectGridManifest,
    StationBinding,
)
from workers.mapping_builder.rewrite import (
    HydrologicCoreFingerprint,
    HydrologicCoreFingerprintMismatchError,  # re-exported for callers
    SemanticDiff,
)

# --- module-level constants -----------------------------------------------

#: Versioned identifier of the mapping algorithm (mirrors
#: :data:`workers.mapping_builder.algorithm.algorithm_id`). Pinned as a
#: standalone constant so SUB-13's evidence bundler records the identity
#: verbatim without a runtime import cycle risk — the value is a literal
#: string, so no code path can accidentally record a different token.
ALGORITHM_ID: str = "nearest_cell_barycenter_geodesic_v1"

#: Field names excluded from :func:`compute_evidence_checksum` input.
#: Per §5.2 Required-evidence: mutating any of these fields MUST NOT
#: change any checksum. The list is intentionally minimal — only build-
#: environment metadata that legitimately varies across otherwise-
#: identical builds (build timestamp, build host) belongs here. Adding
#: fields here is a load-bearing decision; each addition weakens the
#: audit surface by one dimension.
EVIDENCE_CHECKSUM_EXCLUDED_FIELDS: tuple[str, ...] = (
    "build_timestamp",
    "build_host",
)

#: The six G0–G5 gate IDs recorded in :class:`GateResults`. Order matches
#: docs §Gate ordering (baseline package integrity -> mesh geometry ->
#: grid identity -> ownership -> asset delta -> cross-artifact).
G0_THROUGH_G5: tuple[str, ...] = ("G0", "G1", "G2", "G3", "G4", "G5")


# --- exception family -----------------------------------------------------


class EvidencePackageError(Exception):
    """Base class for §5.1 + §5.2 evidence-package assembly failures.

    Distinct root class — NOT a subclass of
    :class:`workers.mapping_builder.integrity.BaselineIntegrityError`,
    :class:`workers.mapping_builder.algorithm.MappingAlgorithmError`,
    :class:`workers.mapping_builder.rewrite.SpAttRewriteError`, or
    :class:`workers.mapping_builder.binding.BindingArtifactError`. Callers
    that catch evidence-package failures MUST NOT accidentally absorb an
    upstream gate's exception family with the same ``except`` clause; the
    mapping builder's fail-closed guarantee is meaningful only when callers
    can tell the five families apart.
    """


class EvidenceChecksumBindingError(EvidencePackageError):
    """The evidence package's binding to the mapping-asset checksum is invalid.

    Per §5.2 Required-evidence: the evidence checksum MUST bind to the
    mapping-asset checksum. A mismatch here means either the recorded
    ``bound_mapping_asset_checksum`` diverges from the expected value, or
    the recorded ``evidence_checksum`` does not equal the recomputed
    digest of the package's ordered contents (excluding
    :data:`EVIDENCE_CHECKSUM_EXCLUDED_FIELDS`).
    """

    def __init__(self, *, expected: str, actual: str) -> None:
        super().__init__(
            f"evidence checksum binding invalid: expected={expected!r} "
            f"actual={actual!r} (§5.2 checksum-binding violation)"
        )
        self.expected = expected
        self.actual = actual


class EvidenceChecksumMutationError(EvidencePackageError):
    """Mutating a non-excluded field failed to invalidate the checksum.

    Belt-and-braces regression guard: if a caller mutates a field NOT in
    :data:`EVIDENCE_CHECKSUM_EXCLUDED_FIELDS` and the recomputed checksum
    still matches the pre-mutation value, that indicates a bug in
    :func:`compute_evidence_checksum` (a field name silently dropped from
    the input, or a serialization step that ignored it). Callers do not
    typically raise this exception directly; it exists so regression
    tests can pin the invariant explicitly.
    """

    def __init__(
        self,
        *,
        mutated_field: str,
        old_checksum: str,
        new_checksum: str,
    ) -> None:
        super().__init__(
            f"mutating field {mutated_field!r} did not invalidate the "
            f"evidence checksum: old={old_checksum!r} new={new_checksum!r} "
            "(§5.2 checksum-input completeness violation)"
        )
        self.mutated_field = mutated_field
        self.old_checksum = old_checksum
        self.new_checksum = new_checksum


class AlgorithmIdentityMismatchError(EvidencePackageError):
    """The recorded algorithm/PROJ identity diverges from the readiness manifest.

    Per §5.1 Required-evidence: the evidence records
    ``algorithm_id='nearest_cell_barycenter_geodesic_v1'`` and
    ``proj_crs_database_version`` cross-checked against the
    :external:doc:`cmfd-direct-grid-platform-readiness` readiness
    manifest. A divergence between the recorded identity and the
    readiness manifest's declared identity means the mapping build used
    an algorithm version or a PROJ CRS database version that was not
    approved by the readiness pipeline — an audit-trail violation.
    """

    def __init__(
        self,
        *,
        field_name: str,
        expected: str,
        actual: str,
        readiness_manifest_checksum: str,
    ) -> None:
        super().__init__(
            f"mapping-algorithm identity field {field_name!r} does not "
            f"match readiness manifest: expected={expected!r} "
            f"actual={actual!r} readiness_manifest_checksum="
            f"{readiness_manifest_checksum!r} (§5.1 identity cross-check "
            "violation)"
        )
        self.field_name = field_name
        self.expected = expected
        self.actual = actual
        self.readiness_manifest_checksum = readiness_manifest_checksum


class GateFailureRecordedInEvidenceError(EvidencePackageError):
    """A G0–G5 gate is recorded as failed in the evidence package.

    Per §5.2 Required-evidence: G0–G5 gate results are recorded and each
    MUST be a pass. A recorded failure means the mapping build produced
    an audit trail that documents a gate blocker — the evidence-bundler
    MUST refuse to ship such a package to production because a
    downstream consumer would otherwise trust the checksum-bound
    receipt without noticing the recorded failure.
    """

    def __init__(self, *, failed_gate_id: str, gate_result: GateResult) -> None:
        super().__init__(
            f"gate {failed_gate_id!r} recorded as failed in evidence "
            f"package: gate_result={gate_result!r} (§5.2 recorded-gate-"
            "failure violation)"
        )
        self.failed_gate_id = failed_gate_id
        self.gate_result = gate_result


class OwnershipImageRenderError(EvidencePackageError):
    """Rendering old/new ownership images from ``domain.shp`` failed.

    Per §5.2 Required-evidence: old/new ownership map images MUST be
    recorded in the evidence package. A rendering failure (missing /
    unparseable ``domain.shp``, corrupt shapefile magic number,
    permission error) is a §5.2 blocker with no partial image bytes.
    """

    def __init__(self, *, reason: str, domain_shp_path: pathlib.Path) -> None:
        super().__init__(
            f"failed to render ownership images from {domain_shp_path}: "
            f"{reason} (§5.2 ownership-image rendering violation)"
        )
        self.reason = reason
        self.domain_shp_path = domain_shp_path


class MissingBaselineIdentityError(EvidencePackageError):
    """A required :class:`BaselineIdentity` field is empty or missing.

    Per §5.1 Required-evidence: the evidence records baseline identity
    (package/att/mesh SHA-256 checksums). A blank field would silently
    ship a checksum-bound receipt with no baseline anchor — refused
    loudly at assembly time.
    """

    def __init__(self, *, missing_field: str) -> None:
        super().__init__(
            f"baseline identity is missing required field "
            f"{missing_field!r} (§5.1 baseline-identity completeness "
            "violation)"
        )
        self.missing_field = missing_field


class CheckusmExcludedFieldEnteredCheckusmError(EvidencePackageError):
    """An excluded field's value entered the evidence checksum computation.

    Belt-and-braces regression guard: if a field enumerated in
    :data:`EVIDENCE_CHECKSUM_EXCLUDED_FIELDS` (e.g. ``build_timestamp``)
    can change the computed checksum, that indicates a bug in
    :func:`compute_evidence_checksum` (the exclusion filter mis-fired).
    Callers do not typically raise this directly; it exists so regression
    tests can pin the invariant explicitly.

    (Name preserved from the §5.2 task spec — the typo is intentional
    per the task-defined public API surface.)
    """

    def __init__(self, *, field_name: str) -> None:
        super().__init__(
            f"excluded field {field_name!r} entered the checksum "
            f"computation (§5.2 excluded-field leak violation)"
        )
        self.field_name = field_name


# --- support dataclasses (all frozen, kwarg-only via caller convention) ---


@dataclass(frozen=True)
class BaselineIdentity:
    """Baseline package/att/mesh SHA-256 identity for the mapping build.

    Sources: SUB-8's ``.sp.att`` pre-SHA-256 (baseline anchor) +
    SUB-1's baseline package checksum (G0 report) + SUB-1's
    ``.sp.mesh`` SHA-256. Every field MUST be a non-empty 64-char
    lowercase hex string; blank strings raise
    :class:`MissingBaselineIdentityError` at :func:`assemble_evidence_package`
    call time.

    Attributes
    ----------
    package_sha256_hex:
        Aggregated baseline package checksum (from SUB-1
        :class:`BaselineIntegrityReport`).
    sp_att_sha256_hex:
        Baseline ``.sp.att`` pre-SHA-256 (INV-1 anchor from SUB-8's
        rewrite report).
    sp_mesh_sha256_hex:
        Baseline ``.sp.mesh`` SHA-256 (topology identity from SUB-1).
    """

    package_sha256_hex: str
    sp_att_sha256_hex: str
    sp_mesh_sha256_hex: str


@dataclass(frozen=True)
class GridSnapshotReference:
    """Reference to the loaded grid snapshot (from Epic #897 fixture).

    Recorded so SUB-13's evidence bundler can audit which snapshot the
    mapping build bound to. Downstream auditors can look up the snapshot
    by ``snapshot_id`` and re-verify the ``grid_signature`` via the
    shared :func:`packages.common.grid_signature.grid_signature_hash`
    authority.

    Attributes
    ----------
    snapshot_id:
        String form of the snapshot's ``grid_snapshot_id`` UUID.
    grid_signature:
        SHA-256 hex of the ordered cell tuple (from the shared authority).
    snapshot_checksum:
        Byte-level checksum of the snapshot fixture / persistence
        (typically the ``grid_definition_checksum`` from the loader).
    """

    snapshot_id: str
    grid_signature: str
    snapshot_checksum: str


@dataclass(frozen=True)
class OwnershipRow:
    """One row of the §14 ownership table (element -> grid cell).

    Sorted by ``element_id`` ascending at :func:`assemble_evidence_package`
    time so the tuple ordering is deterministic. ``old_forc`` and
    ``new_forc`` are stringified for JSON-safe serialization (the raw
    integers live in SUB-8's :class:`SemanticDiff`; here we record what
    the evidence bundler needs to display).

    Attributes
    ----------
    element_id:
        Baseline element ID (matches :class:`ElementOwnership.element_id`).
    old_forc:
        Original ``FORC`` value from the baseline ``.sp.att`` (stringified
        for cross-language JSON safety).
    new_forc:
        Rewritten ``FORC`` value from the variant ``.sp.att``.
    grid_cell_id:
        The owning cell's identifier (matches
        :class:`ElementOwnership.grid_cell_id`).
    distance_meters:
        Geodesic distance from the element barycenter to the owning cell
        center (from :class:`ElementOwnership.geodesic_distance_m`).
    """

    element_id: int
    old_forc: str
    new_forc: str
    grid_cell_id: str
    distance_meters: float


@dataclass(frozen=True)
class SpAttAssetDiff:
    """``.sp.att`` old/new checksums + semantic diff summary.

    Wraps SUB-8's outputs (:class:`SpAttChecksums` + :class:`SemanticDiff`)
    into the evidence-package field layout. Recorded verbatim from SUB-8;
    this module never recomputes the semantic diff.

    Attributes
    ----------
    old_sha256_hex:
        Baseline ``.sp.att`` SHA-256 (from SUB-8's :class:`SpAttChecksums`).
    new_sha256_hex:
        Variant ``.sp.att`` SHA-256 (from SUB-8's :class:`SpAttChecksums`).
    semantic_diff_summary:
        SUB-8's :class:`SemanticDiff` recording only ``FORC`` deltas.
    """

    old_sha256_hex: str
    new_sha256_hex: str
    semantic_diff_summary: SemanticDiff


@dataclass(frozen=True)
class MappingAlgorithmIdentity:
    """The mapping algorithm + PROJ CRS database version identity.

    Cross-checked against the Epic #886
    ``cmfd-direct-grid-platform-readiness`` readiness manifest at
    :func:`verify_algorithm_and_proj_identity_matches_readiness` call
    time. Both fields enter the evidence checksum — mutating either one
    invalidates the checksum (§5.1 Required-evidence).

    Attributes
    ----------
    algorithm_id:
        Must equal :data:`ALGORITHM_ID`
        (``"nearest_cell_barycenter_geodesic_v1"``); any other value is
        an :class:`AlgorithmIdentityMismatchError` at cross-check time.
    proj_crs_database_version:
        Version string of the PROJ CRS database the mapping build ran
        against (e.g. ``"pyproj:3.7.2 proj:9.4.1 db:1.20"``). Must match
        the readiness manifest verbatim.
    """

    algorithm_id: str
    proj_crs_database_version: str


@dataclass(frozen=True)
class DistanceQA:
    """§5.2 distance-QA summary: normalized quantiles + tie + edge counts.

    Distances are normalized by the local cell size before quantile
    computation so the summary is dimensionless (typical values are in
    ``[0, 1]``). Populated at :func:`assemble_evidence_package` time
    from the caller-supplied ownership + distance-normalization stats.

    Attributes
    ----------
    min_normalized:
        Smallest normalized geodesic distance across all elements.
    p50_normalized:
        50th-percentile normalized distance.
    p95_normalized:
        95th-percentile normalized distance.
    max_normalized:
        Largest normalized distance.
    tie_count:
        Number of elements whose ownership resolved via
        :func:`workers.mapping_builder.resolve_tie_by_canonical_ordinal`
        (tie_status != ``"unique"``).
    coverage_edge_count:
        Number of elements within one cell-diagonal of the snapshot bbox
        edge (elements near the coverage boundary; a high count is a
        signal that the basin footprint hugs the grid edge).
    """

    min_normalized: float
    p50_normalized: float
    p95_normalized: float
    max_normalized: float
    tie_count: int
    coverage_edge_count: int


@dataclass(frozen=True)
class CapacityReport:
    """§5.2 capacity report: current vs limits + before/after station reduction.

    Framed against the ~5× station-reduction narrative in docs §14
    (6290 raw stations -> ~1200 direct-grid stations for the qhh
    baseline case). ``station_reduction_ratio`` is
    ``before_station_count / after_station_count``.

    Attributes
    ----------
    station_count:
        Post-reduction station count (== used-cell count).
    timestep_count, timeseries_row_count, file_size_bytes:
        Runtime-capacity dimensions (from the caller-supplied capacity
        report). SUB-13 records them verbatim; SUB-14 verifies against
        limits.
    station_count_limit, timestep_count_limit, timeseries_row_count_limit, file_size_bytes_limit:
        Per-dimension operational limits (from configuration).
    before_station_count:
        Legacy (pre-direct-grid) station count for the same basin.
    after_station_count:
        Direct-grid station count. Equals :attr:`station_count`.
    station_reduction_ratio:
        ``before_station_count / after_station_count`` (float). Typical
        value ~5× for the qhh baseline case.
    """

    station_count: int
    timestep_count: int
    timeseries_row_count: int
    file_size_bytes: int
    station_count_limit: int
    timestep_count_limit: int
    timeseries_row_count_limit: int
    file_size_bytes_limit: int
    before_station_count: int
    after_station_count: int
    station_reduction_ratio: float


@dataclass(frozen=True)
class GateResult:
    """One G0–G5 gate's pass/fail + structured evidence reference.

    Frozen so downstream evidence can bind the record byte-for-byte.
    ``gate_id`` is one of :data:`G0_THROUGH_G5`; any other value is a
    caller bug (validated at :func:`assemble_evidence_package` time).

    Attributes
    ----------
    gate_id:
        Gate identifier (``"G0"``..``"G5"``).
    passed:
        ``True`` iff the gate ran and produced no blocker.
    evidence_ref:
        Structured pointer to the upstream artifact that fed the gate
        (e.g. ``{"kind": "baseline_integrity_report", "checksum": ...}``
        for G0). SUB-14 uses the pointer to re-open the artifact and
        rerun the gate offline.
    """

    gate_id: str
    passed: bool
    evidence_ref: Mapping[str, Any]


@dataclass(frozen=True)
class GateResults:
    """The six G0–G5 gate results recorded on the evidence package.

    One named field per gate (rather than a ``dict[str, GateResult]``)
    so signature-pin tests can assert the exact ``param.kind`` and field
    order per :class:`GateResult`. Downstream code that wants the
    iterable form can call :meth:`iter_ordered`.
    """

    g0: GateResult
    g1: GateResult
    g2: GateResult
    g3: GateResult
    g4: GateResult
    g5: GateResult

    def iter_ordered(self) -> tuple[GateResult, ...]:
        """Return the six gate results in G0..G5 order."""
        return (self.g0, self.g1, self.g2, self.g3, self.g4, self.g5)


@dataclass(frozen=True)
class OwnershipImages:
    """Old + new ownership map images (visualization only, INV-3).

    Bytes-only container so downstream storage never has to know about
    the rendering pipeline. ``image_format`` is one of ``"svg"`` or
    ``"png"``; the current implementation
    (:func:`render_ownership_images`) emits ``"svg"``. Attempting to
    round-trip these bytes back into any G0–G5 gate is an INV-3
    violation caught at code review.

    Attributes
    ----------
    old_image_bytes:
        SVG or PNG bytes for the old (baseline) ownership map.
    new_image_bytes:
        SVG or PNG bytes for the new (variant) ownership map.
    image_format:
        Format token, one of ``"svg"`` or ``"png"``.
    """

    old_image_bytes: bytes
    new_image_bytes: bytes
    image_format: str


@dataclass(frozen=True)
class Approvals:
    """Optional operator approvals recorded on the evidence package.

    Currently carries the SUB-7 small-basin override approver id
    (``None`` when the basin does NOT trigger the small-basin gate, or
    when the gate passed at the default 4-used-cell threshold).

    Attributes
    ----------
    small_basin_override_approver_id:
        Non-empty approver identity string iff SUB-7's small-basin
        override was invoked; ``None`` otherwise.
    """

    small_basin_override_approver_id: str | None = None


@dataclass(frozen=True)
class RollbackTarget:
    """Rollback target: the previous mapping asset this variant supersedes.

    Recorded so a rollback ops flow can find the immediately-preceding
    mapping asset without a separate lookup table. Both fields are
    required — an initial mapping build (no predecessor) records
    ``previous_mapping_asset_checksum=""`` and
    ``previous_mapping_asset_label="<initial>"`` sentinel values.

    Attributes
    ----------
    previous_mapping_asset_checksum:
        SHA-256 hex of the previous mapping asset (or ``""`` sentinel).
    previous_mapping_asset_label:
        Human-readable label (typically a version tag or a UUID).
    """

    previous_mapping_asset_checksum: str
    previous_mapping_asset_label: str


@dataclass(frozen=True)
class ReadinessManifest:
    """Epic #886 readiness manifest content (caller-supplied fixture).

    The mapping build receives this struct as an INPUT to
    :func:`verify_algorithm_and_proj_identity_matches_readiness`. This
    module does NOT parse the raw readiness manifest; the caller
    (SUB-13 evidence bundler orchestrator) does that.

    Attributes
    ----------
    algorithm_id:
        Approved algorithm identifier from the readiness manifest. MUST
        equal :data:`ALGORITHM_ID`.
    proj_crs_database_version:
        Approved PROJ CRS database version from the readiness manifest.
    checksum:
        SHA-256 hex of the readiness manifest bytes (for provenance).
    """

    algorithm_id: str
    proj_crs_database_version: str
    checksum: str


# --- top-level EvidencePackage --------------------------------------------


@dataclass(frozen=True)
class EvidencePackage:
    """§14 immutable mapping evidence package (Epic #909 SUB-13).

    Field order matches the docs §14 layout, and matches the serialization
    order used by :func:`compute_evidence_checksum`. Mutating any field
    other than the enumerated
    :data:`EVIDENCE_CHECKSUM_EXCLUDED_FIELDS` invalidates
    :attr:`evidence_checksum` — the checksum computation iterates the
    dataclass fields deterministically and skips the excluded names.

    Immutability contract
    ---------------------
    Frozen dataclass — direct assignment raises
    :class:`dataclasses.FrozenInstanceError`. The intended mutation path
    is ``dataclasses.replace(package, ...)`` which produces a NEW
    instance. Callers who need to re-bind the mapping asset MUST call
    :func:`bind_evidence_to_mapping_asset` which recomputes
    :attr:`evidence_checksum` alongside setting
    :attr:`bound_mapping_asset_checksum`.

    Attributes
    ----------
    baseline_identity, grid_snapshot_reference, ownership_table,
    station_binding_rows, sp_att_asset_diff,
    mapping_algorithm_identity, hydrologic_core_fingerprint, distance_qa,
    capacity_report, gate_results, ownership_images, approvals,
    rollback_target:
        The §14 evidence sections; see the individual dataclass
        docstrings for details.
    checksum_excluded_fields:
        Enumeration of field names excluded from
        :func:`compute_evidence_checksum` input. Set from
        :data:`EVIDENCE_CHECKSUM_EXCLUDED_FIELDS` at assemble time so
        SUB-14 integration tests can audit the exclusion set from the
        package alone.
    build_timestamp:
        Optional UTC-aware datetime recorded for human audit; NEVER
        enters :attr:`evidence_checksum`. Present as a
        :data:`EVIDENCE_CHECKSUM_EXCLUDED_FIELDS` exemplar.
    evidence_checksum:
        SHA-256 hex over the ordered evidence contents (excluding the
        fields listed in :attr:`checksum_excluded_fields` and
        :attr:`evidence_checksum` itself). Computed by
        :func:`compute_evidence_checksum` and reset by
        :func:`bind_evidence_to_mapping_asset`.
    bound_mapping_asset_checksum:
        SHA-256 hex of the mapping-asset bytes this evidence binds to.
        Set by :func:`bind_evidence_to_mapping_asset`; enters the
        evidence checksum, so mutating it invalidates
        :attr:`evidence_checksum`.
    """

    baseline_identity: BaselineIdentity
    grid_snapshot_reference: GridSnapshotReference
    ownership_table: tuple[OwnershipRow, ...]
    station_binding_rows: tuple[StationBinding, ...]
    sp_att_asset_diff: SpAttAssetDiff
    mapping_algorithm_identity: MappingAlgorithmIdentity
    hydrologic_core_fingerprint: HydrologicCoreFingerprint
    distance_qa: DistanceQA
    capacity_report: CapacityReport
    gate_results: GateResults
    ownership_images: OwnershipImages
    approvals: Approvals
    rollback_target: RollbackTarget
    checksum_excluded_fields: tuple[str, ...]
    build_timestamp: datetime | None
    evidence_checksum: str
    bound_mapping_asset_checksum: str


# --- serialization helpers ------------------------------------------------


def _to_jsonable(value: Any) -> Any:
    """Recursively convert a value to a JSON-safe primitive.

    Frozen dataclasses (any :func:`dataclasses.is_dataclass` instance)
    are converted to dicts field-by-field. Tuples become lists, mappings
    become dicts, bytes become ``{"__bytes_sha256__": <hex>}`` so the
    canonical JSON serializer's output stays deterministic without
    hand-copying arbitrary byte payloads into the digest input.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        # Deterministic digest of bytes payload so image bytes enter the
        # checksum without inflating the serialized envelope. Two runs
        # producing byte-identical images produce byte-identical envelope
        # bytes; a single byte diff on the image bytes flips the digest.
        return {"__bytes_sha256__": hashlib.sha256(value).hexdigest()}
    if isinstance(value, datetime):
        # Datetimes never enter the checksum via a non-excluded field
        # (build_timestamp is excluded). But if a caller adds a datetime
        # into an evidence_ref mapping we normalize via the shared
        # authority's JSON default (isoformat with UTC ``Z`` suffix).
        return value.isoformat().replace("+00:00", "Z")
    if dataclasses.is_dataclass(value):
        return {
            f.name: _to_jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        # Sort keys so serialization is deterministic even when the input
        # mapping is not; canonical_json_bytes also sorts, but doing it
        # here means the recursive walk visits keys in a stable order.
        return {str(k): _to_jsonable(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, (list, tuple, Sequence)) and not isinstance(value, str):
        return [_to_jsonable(item) for item in value]
    # Fallback: stringify. Reached for objects like UUID that carry a
    # canonical string representation.
    return str(value)


def _package_to_digest_dict(package: EvidencePackage) -> dict[str, Any]:
    """Return the digest-input dict for :func:`compute_evidence_checksum`.

    Iterates the package's dataclass fields in declaration order and
    skips names in :attr:`EvidencePackage.checksum_excluded_fields` and
    the ``evidence_checksum`` field itself (which cannot be an input to
    its own computation). All other fields are recursively converted to
    JSON-safe primitives via :func:`_to_jsonable`.
    """
    excluded = set(package.checksum_excluded_fields) | {"evidence_checksum"}
    digest_input: dict[str, Any] = {}
    for f in dataclasses.fields(package):
        if f.name in excluded:
            continue
        digest_input[f.name] = _to_jsonable(getattr(package, f.name))
    return digest_input


# --- public: pure computations --------------------------------------------


def enumerate_checksum_excluded_fields() -> tuple[str, ...]:
    """Return the canonical list of evidence-checksum-excluded field names.

    Per §5.2 Required-evidence: mutating any of these fields MUST NOT
    change any checksum. SUB-14's integration test consumes this
    function verbatim to audit the exclusion set without importing the
    module-level constant directly (function-call boundary makes the
    contract test-visible).

    Returns
    -------
    tuple[str, ...]
        :data:`EVIDENCE_CHECKSUM_EXCLUDED_FIELDS` (identity — same
        tuple object).
    """
    return EVIDENCE_CHECKSUM_EXCLUDED_FIELDS


def compute_evidence_checksum(package: EvidencePackage) -> str:
    """SHA-256 hex of the ordered evidence contents.

    Iterates the package's dataclass fields in declaration order and
    excludes both :attr:`EvidencePackage.checksum_excluded_fields` and
    ``evidence_checksum`` itself. Every other field is recursively
    JSON-serialized via
    :func:`packages.common.grid_signature.canonical_json_bytes` (the
    single shared byte-deterministic authority). The bytes are then
    SHA-256-hashed and returned as 64 lowercase hex characters.

    Parameters
    ----------
    package:
        The :class:`EvidencePackage` to checksum.

    Returns
    -------
    str
        64-char lowercase SHA-256 hex digest.
    """
    digest_input = _package_to_digest_dict(package)
    payload = _shared_canonical_json_bytes(digest_input)
    return hashlib.sha256(payload).hexdigest()


def bind_evidence_to_mapping_asset(
    package: EvidencePackage,
    mapping_asset_checksum: str,
) -> EvidencePackage:
    """Return a new :class:`EvidencePackage` bound to the given asset checksum.

    Sets :attr:`bound_mapping_asset_checksum` to
    ``mapping_asset_checksum`` and recomputes
    :attr:`evidence_checksum` via :func:`compute_evidence_checksum` on
    the intermediate package. The input ``package`` is NOT mutated —
    frozen dataclasses cannot be mutated in place, and the function
    returns a fresh instance so callers who want to preserve the
    pre-binding package can keep a reference.

    Parameters
    ----------
    package:
        The :class:`EvidencePackage` to bind. Typically the return value
        of :func:`assemble_evidence_package`.
    mapping_asset_checksum:
        SHA-256 hex of the mapping-asset bytes. Non-empty; blank raises
        an :class:`EvidencePackageError` at the boundary.

    Returns
    -------
    EvidencePackage
        A fresh package with :attr:`bound_mapping_asset_checksum` set
        and :attr:`evidence_checksum` recomputed.

    Raises
    ------
    EvidencePackageError
        ``mapping_asset_checksum`` is empty or whitespace-only.
    """
    if not isinstance(mapping_asset_checksum, str) or not mapping_asset_checksum.strip():
        raise EvidencePackageError(
            "mapping_asset_checksum must be a non-empty string "
            "(§5.2 mapping-asset checksum missing)"
        )
    intermediate = dataclasses.replace(
        package,
        bound_mapping_asset_checksum=mapping_asset_checksum,
        # Set evidence_checksum to a placeholder that will be replaced;
        # the placeholder never enters the digest (evidence_checksum is
        # always excluded from digest input) so the value here is
        # cosmetic.
        evidence_checksum="",
    )
    return dataclasses.replace(
        intermediate,
        evidence_checksum=compute_evidence_checksum(intermediate),
    )


# --- public: ownership images (INV-3 visualization-only) ------------------


#: Magic number at the head of an ESRI shapefile per the ESRI Shapefile
#: whitepaper (big-endian int at byte 0). Used by
#: :func:`render_ownership_images` as a fail-fast check that
#: ``domain_shp_path`` is a real shapefile rather than an accidental
#: text file or empty stub. Reject-on-mismatch turns "wrong file
#: extension" into a loud failure instead of a silent malformed SVG.
_SHAPEFILE_MAGIC_NUMBER: int = 9994

#: SVG canvas dimensions for the ownership images. Fixed so the rendered
#: bytes are deterministic across environments. The chosen size (640x480)
#: is large enough to show a small basin's element ownership as colored
#: rectangles without overflowing a browser preview.
_SVG_CANVAS_WIDTH: int = 640
_SVG_CANVAS_HEIGHT: int = 480


def _read_shapefile_magic(domain_shp_path: pathlib.Path) -> None:
    """Fail-fast read of the ESRI shapefile magic number.

    Reads the first 4 bytes of ``domain_shp_path`` and asserts they
    decode to the big-endian int :data:`_SHAPEFILE_MAGIC_NUMBER`. Any
    mismatch (empty file, wrong extension, corrupt header) raises
    :class:`OwnershipImageRenderError`.

    We deliberately do NOT parse geometry from the shapefile — INV-3
    forbids ``domain.shp`` from being an algorithm input; the magic
    number check is a filesystem sanity check, not a geometry read.
    """
    if not domain_shp_path.exists() or not domain_shp_path.is_file():
        raise OwnershipImageRenderError(
            reason="domain.shp does not exist or is not a file",
            domain_shp_path=domain_shp_path,
        )
    try:
        with open(domain_shp_path, "rb") as handle:
            header = handle.read(4)
    except OSError as exc:
        raise OwnershipImageRenderError(
            reason=f"failed to read domain.shp header: {exc}",
            domain_shp_path=domain_shp_path,
        ) from exc
    if len(header) < 4:
        raise OwnershipImageRenderError(
            reason=f"domain.shp header too short ({len(header)} bytes)",
            domain_shp_path=domain_shp_path,
        )
    magic = int.from_bytes(header, "big")
    if magic != _SHAPEFILE_MAGIC_NUMBER:
        raise OwnershipImageRenderError(
            reason=(
                f"domain.shp magic number is {magic} "
                f"(expected {_SHAPEFILE_MAGIC_NUMBER})"
            ),
            domain_shp_path=domain_shp_path,
        )


def _render_single_ownership_svg(
    ownership_table: Sequence[OwnershipRow],
    *,
    which: str,
) -> bytes:
    """Emit a deterministic SVG for one side (old or new) of the ownership map.

    The rendered SVG is deliberately schematic: element ownership is
    displayed as a horizontal band of colored rectangles keyed by
    element_id order. ``which`` is ``"old"`` or ``"new"`` — the rectangle
    label is the corresponding ``FORC`` value from :class:`OwnershipRow`.

    Determinism guarantees:

    * Element order is fixed to ``element_id`` ascending (matches the
      ownership table sort applied at :func:`assemble_evidence_package`
      time).
    * Coordinate arithmetic uses integer division so a rounding
      variation across environments cannot shift a rectangle by a
      fractional pixel.
    * The header + trailer are byte-identical strings.
    """
    if which not in ("old", "new"):
        raise ValueError(f"which must be 'old' or 'new', got {which!r}")
    sorted_rows = sorted(ownership_table, key=lambda r: int(r.element_id))
    n = len(sorted_rows)
    # Body content: element_id + FORC value at fixed positions.
    lines: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{_SVG_CANVAS_WIDTH}" height="{_SVG_CANVAS_HEIGHT}" '
        f'viewBox="0 0 {_SVG_CANVAS_WIDTH} {_SVG_CANVAS_HEIGHT}">',
        f'<title>ownership_{which}</title>',
        f'<rect x="0" y="0" width="{_SVG_CANVAS_WIDTH}" '
        f'height="{_SVG_CANVAS_HEIGHT}" fill="#ffffff" stroke="#000000"/>',
        f'<text x="10" y="20" font-family="monospace" font-size="14">'
        f'ownership_{which} n={n}</text>',
    ]
    # Rows laid out top-to-bottom, ownership per row: element_id -> forc.
    row_height = 16
    for i, row in enumerate(sorted_rows):
        y = 30 + i * row_height
        # Clip rendering at the canvas edge — SUB-13 evidence packages
        # rarely carry more than a few hundred elements, but a
        # 6290-element basin would run off the canvas without this guard.
        if y + row_height > _SVG_CANVAS_HEIGHT - 4:
            break
        forc_value = row.old_forc if which == "old" else row.new_forc
        lines.append(
            f'<text x="10" y="{y}" font-family="monospace" font-size="10">'
            f'e{row.element_id}|c{row.grid_cell_id}|f{forc_value}</text>'
        )
    lines.append("</svg>")
    return "\n".join(lines).encode("utf-8")


def render_ownership_images(
    domain_shp_path: pathlib.Path,
    ownership_table: tuple[OwnershipRow, ...],
) -> OwnershipImages:
    """Render deterministic old/new ownership map images (INV-3, §5.2).

    Emits a schematic SVG per side (``old`` uses ``OwnershipRow.old_forc``,
    ``new`` uses :attr:`OwnershipRow.new_forc``). The rendering is
    deterministic under fixed input — two identical calls produce
    byte-identical :class:`OwnershipImages`.

    ``domain_shp_path`` is validated (fail-fast magic-number check) but
    NEVER read as algorithm geometry. INV-3 pins ``domain.shp`` as a
    visualization-only surface; any code path that reads shapefile
    geometry into a G0–G5 gate would be an INV-3 violation caught at
    code review.

    Parameters
    ----------
    domain_shp_path:
        Path to the baseline ``domain.shp``. Must exist and carry a
        valid ESRI shapefile magic number.
    ownership_table:
        The sorted-by-element_id ownership rows (from
        :attr:`EvidencePackage.ownership_table`).

    Returns
    -------
    OwnershipImages
        Old + new SVG bytes + ``image_format="svg"``.

    Raises
    ------
    OwnershipImageRenderError
        ``domain_shp_path`` is missing, unreadable, or has an invalid
        magic number.
    """
    _read_shapefile_magic(domain_shp_path)
    old_bytes = _render_single_ownership_svg(ownership_table, which="old")
    new_bytes = _render_single_ownership_svg(ownership_table, which="new")
    return OwnershipImages(
        old_image_bytes=old_bytes,
        new_image_bytes=new_bytes,
        image_format="svg",
    )


# --- public: verify_ gates ------------------------------------------------


def verify_evidence_checksum_binding(
    package: EvidencePackage,
    expected_mapping_asset_checksum: str,
) -> None:
    """Fail-closed §5.2 gate: evidence checksum binds to the mapping asset.

    Asserts two properties:

    1. :attr:`EvidencePackage.bound_mapping_asset_checksum` equals
       ``expected_mapping_asset_checksum``.
    2. :attr:`EvidencePackage.evidence_checksum` equals the recomputed
       checksum via :func:`compute_evidence_checksum`.

    Either failure raises :class:`EvidenceChecksumBindingError`. This is
    the standalone rerunnable gate SUB-14's integration test consumes to
    prove "mutating either the evidence or the mapping asset invalidates
    the binding" — the required §5.2 evidence.

    Parameters
    ----------
    package:
        The :class:`EvidencePackage` to verify.
    expected_mapping_asset_checksum:
        SHA-256 hex the caller expects
        :attr:`EvidencePackage.bound_mapping_asset_checksum` to equal.

    Raises
    ------
    EvidenceChecksumBindingError
        Either the recorded mapping-asset binding or the recorded
        evidence checksum diverges from the expected / recomputed
        value.
    """
    if package.bound_mapping_asset_checksum != expected_mapping_asset_checksum:
        raise EvidenceChecksumBindingError(
            expected=expected_mapping_asset_checksum,
            actual=package.bound_mapping_asset_checksum,
        )
    recomputed = compute_evidence_checksum(package)
    if package.evidence_checksum != recomputed:
        raise EvidenceChecksumBindingError(
            expected=recomputed,
            actual=package.evidence_checksum,
        )


def verify_algorithm_and_proj_identity_matches_readiness(
    package: EvidencePackage,
    *,
    readiness_manifest: ReadinessManifest,
) -> None:
    """Fail-closed §5.1 gate: algorithm/PROJ identity matches the readiness manifest.

    Asserts two properties, in order:

    1. :attr:`MappingAlgorithmIdentity.algorithm_id` equals
       :attr:`ReadinessManifest.algorithm_id`.
    2. :attr:`MappingAlgorithmIdentity.proj_crs_database_version` equals
       :attr:`ReadinessManifest.proj_crs_database_version`.

    Either mismatch raises :class:`AlgorithmIdentityMismatchError` with
    the readiness manifest's ``checksum`` recorded on the exception so
    downstream evidence can name the exact readiness bundle the mapping
    build diverged from.

    Parameters
    ----------
    package:
        The :class:`EvidencePackage` to verify.
    readiness_manifest:
        Caller-supplied :class:`ReadinessManifest` fixture representing
        the Epic #886 readiness bundle content.

    Raises
    ------
    AlgorithmIdentityMismatchError
        Either identity field diverges from the readiness manifest.
    """
    if (
        package.mapping_algorithm_identity.algorithm_id
        != readiness_manifest.algorithm_id
    ):
        raise AlgorithmIdentityMismatchError(
            field_name="algorithm_id",
            expected=readiness_manifest.algorithm_id,
            actual=package.mapping_algorithm_identity.algorithm_id,
            readiness_manifest_checksum=readiness_manifest.checksum,
        )
    if (
        package.mapping_algorithm_identity.proj_crs_database_version
        != readiness_manifest.proj_crs_database_version
    ):
        raise AlgorithmIdentityMismatchError(
            field_name="proj_crs_database_version",
            expected=readiness_manifest.proj_crs_database_version,
            actual=package.mapping_algorithm_identity.proj_crs_database_version,
            readiness_manifest_checksum=readiness_manifest.checksum,
        )


def verify_all_g0_through_g5_gates_passed(package: EvidencePackage) -> None:
    """Fail-closed §5.2 gate: every recorded G0–G5 gate is a pass.

    Iterates :meth:`GateResults.iter_ordered` and raises
    :class:`GateFailureRecordedInEvidenceError` on the FIRST recorded
    failure. Callers that want to log all failures can iterate the
    gate results independently before invoking this gate; the gate
    itself follows the "first-firing" pattern from SUB-8..12
    ``verify_*`` gates.

    Parameters
    ----------
    package:
        The :class:`EvidencePackage` to verify.

    Raises
    ------
    GateFailureRecordedInEvidenceError
        A recorded gate result has ``passed=False``.
    """
    for gate_result in package.gate_results.iter_ordered():
        if not gate_result.passed:
            raise GateFailureRecordedInEvidenceError(
                failed_gate_id=gate_result.gate_id,
                gate_result=gate_result,
            )


# --- public: orchestrator -------------------------------------------------


def assemble_evidence_package(
    *,
    baseline_identity: BaselineIdentity,
    grid_snapshot_reference: GridSnapshotReference,
    ownership_table: Sequence[OwnershipRow],
    manifest: DirectGridManifest,
    binding_artifact: BindingArtifact,
    sp_att_asset_diff: SpAttAssetDiff,
    mapping_algorithm_identity: MappingAlgorithmIdentity,
    hydrologic_core_fingerprint: HydrologicCoreFingerprint,
    distance_qa: DistanceQA,
    capacity_report: CapacityReport,
    gate_results: GateResults,
    ownership_images: OwnershipImages,
    approvals: Approvals,
    rollback_target: RollbackTarget,
    build_timestamp: datetime | None = None,
) -> EvidencePackage:
    """§5.1 + §5.2 orchestrator: assemble the immutable evidence package.

    Consumes upstream outputs verbatim from SUB-1..SUB-12 and packs them
    into the frozen :class:`EvidencePackage`. The orchestrator does NOT
    rerun any G0–G5 gate — it records what the upstream produced. The
    caller is expected to run the individual ``verify_*`` gates
    (SUB-1..SUB-12 exports) before invoking this function and to record
    each gate's pass/fail in :class:`GateResults`.

    Steps:

    1. Validate baseline identity completeness
       (:class:`MissingBaselineIdentityError` on empty field).
    2. Sort :attr:`ownership_table` by ``element_id`` ascending for
       deterministic tuple layout.
    3. Copy SUB-11's ``station_bindings`` verbatim into
       :attr:`station_binding_rows`.
    4. Assemble the :class:`EvidencePackage` with
       :data:`EVIDENCE_CHECKSUM_EXCLUDED_FIELDS` recorded as
       :attr:`checksum_excluded_fields` and a placeholder
       ``bound_mapping_asset_checksum`` ("" sentinel — the caller MUST
       call :func:`bind_evidence_to_mapping_asset` before shipping the
       package).
    5. Compute :attr:`evidence_checksum` via
       :func:`compute_evidence_checksum` (over the ordered contents
       excluding ``build_timestamp`` and ``build_host``).

    Parameters
    ----------
    baseline_identity:
        :class:`BaselineIdentity` with package/att/mesh checksums.
    grid_snapshot_reference:
        :class:`GridSnapshotReference` bound to the loaded snapshot.
    ownership_table:
        Sequence of :class:`OwnershipRow`. Will be sorted by
        ``element_id`` ascending internally.
    manifest, binding_artifact:
        SUB-11's emitted :class:`DirectGridManifest` +
        :class:`BindingArtifact`. Only the ``station_bindings`` tuple
        from ``binding_artifact`` is recorded verbatim on the evidence
        package (the manifest is not stored — its
        ``binding_checksum`` + ``grid_signature`` are already reflected
        in :class:`GridSnapshotReference` and
        :attr:`bound_mapping_asset_checksum`).
    sp_att_asset_diff:
        SUB-8's :class:`SpAttAssetDiff` (old + new checksums + semantic
        diff).
    mapping_algorithm_identity:
        :class:`MappingAlgorithmIdentity` with algorithm_id +
        proj_crs_database_version. Cross-checked against the readiness
        manifest by :func:`verify_algorithm_and_proj_identity_matches_readiness`.
    hydrologic_core_fingerprint:
        SUB-9's :class:`HydrologicCoreFingerprint`. Recorded verbatim.
    distance_qa, capacity_report:
        §5.2 QA + capacity structs.
    gate_results:
        :class:`GateResults` — the six recorded G0–G5 gate outcomes.
    ownership_images:
        :class:`OwnershipImages` (INV-3 visualization only, from
        :func:`render_ownership_images`).
    approvals:
        :class:`Approvals` — SUB-7 override approver id or ``None``.
    rollback_target:
        :class:`RollbackTarget` — the previous mapping asset (or
        initial-build sentinel).
    build_timestamp:
        Optional UTC-aware timestamp. Recorded but NEVER enters
        :attr:`EvidencePackage.evidence_checksum`.

    Returns
    -------
    EvidencePackage
        The frozen assembled package. The caller MUST subsequently
        invoke :func:`bind_evidence_to_mapping_asset` before shipping
        the package to production (the pre-bind checksum reflects an
        unbound mapping asset).

    Raises
    ------
    MissingBaselineIdentityError
        Any :class:`BaselineIdentity` field is blank / missing.
    EvidencePackageError
        A caller-supplied :class:`GateResult` has a ``gate_id`` outside
        :data:`G0_THROUGH_G5` (order violation), or the
        :class:`MappingAlgorithmIdentity` has an empty algorithm_id or
        proj_crs_database_version.
    """
    # --- baseline identity completeness -----------------------------------
    if not baseline_identity.package_sha256_hex.strip():
        raise MissingBaselineIdentityError(missing_field="package_sha256_hex")
    if not baseline_identity.sp_att_sha256_hex.strip():
        raise MissingBaselineIdentityError(missing_field="sp_att_sha256_hex")
    if not baseline_identity.sp_mesh_sha256_hex.strip():
        raise MissingBaselineIdentityError(missing_field="sp_mesh_sha256_hex")

    # --- gate results: enforce G0..G5 order + label discipline ------------
    ordered_gates = gate_results.iter_ordered()
    for expected_gate_id, gate_result in zip(G0_THROUGH_G5, ordered_gates, strict=True):
        if gate_result.gate_id != expected_gate_id:
            raise EvidencePackageError(
                f"gate slot for {expected_gate_id!r} carries "
                f"gate_id={gate_result.gate_id!r}; each GateResults slot "
                "MUST match its position label"
            )

    # --- mapping-algorithm identity: non-empty --------------------------
    if not mapping_algorithm_identity.algorithm_id.strip():
        raise EvidencePackageError(
            "mapping_algorithm_identity.algorithm_id must be non-empty "
            "(§5.1 algorithm-identity completeness violation)"
        )
    if not mapping_algorithm_identity.proj_crs_database_version.strip():
        raise EvidencePackageError(
            "mapping_algorithm_identity.proj_crs_database_version must be "
            "non-empty (§5.1 PROJ-identity completeness violation)"
        )

    # --- ownership_table: sort by element_id ascending -----------------
    sorted_rows = tuple(
        sorted(ownership_table, key=lambda r: int(r.element_id))
    )

    # --- station bindings: pass through SUB-11 verbatim ----------------
    station_binding_rows = tuple(binding_artifact.station_bindings)

    # Assemble an intermediate package with placeholder checksum fields
    # so we can compute the digest via the same helper the caller uses
    # via :func:`bind_evidence_to_mapping_asset` — one code path for
    # digest computation eliminates the "orchestrator's digest diverges
    # from the caller's" failure mode.
    intermediate = EvidencePackage(
        baseline_identity=baseline_identity,
        grid_snapshot_reference=grid_snapshot_reference,
        ownership_table=sorted_rows,
        station_binding_rows=station_binding_rows,
        sp_att_asset_diff=sp_att_asset_diff,
        mapping_algorithm_identity=mapping_algorithm_identity,
        hydrologic_core_fingerprint=hydrologic_core_fingerprint,
        distance_qa=distance_qa,
        capacity_report=capacity_report,
        gate_results=gate_results,
        ownership_images=ownership_images,
        approvals=approvals,
        rollback_target=rollback_target,
        checksum_excluded_fields=EVIDENCE_CHECKSUM_EXCLUDED_FIELDS,
        build_timestamp=build_timestamp,
        # Placeholder checksums — replaced immediately below.
        evidence_checksum="",
        bound_mapping_asset_checksum="",
    )
    return dataclasses.replace(
        intermediate,
        evidence_checksum=compute_evidence_checksum(intermediate),
    )


# Silence the unused-import warning without hiding the re-export from
# static analyzers: the exception class is exported so callers can catch
# SUB-9's hydrologic-fingerprint-mismatch under the evidence-package
# module's namespace.
_ = HydrologicCoreFingerprintMismatchError

# --- silence unused-import lint warnings ---------------------------------
# ``field`` is not used at runtime but the module keeps the import so a
# future refactor that needs a per-cell default factory can add it
# without touching the import block.
_ = field
