"""§3.1 + §3.2 + §3.3 + §3.4 ``.sp.att`` FORC rewrite + G4 gates.

This module implements OpenSpec change ``forcing-mapping-asset-build`` §3.1
through §3.4 (Epic #909 SUB-8 + SUB-9). It exposes fail-closed primitives
that copy the baseline ``.sp.att`` into a variant package, update every
element's ``FORC`` value **by element ID** via the ownership +
``shud_forcing_index`` produced by ``element-grid-ownership-mapping``, prove
that no non-``FORC`` byte changes in the process, prove that all non-``.sp.att``
hydrologic core files are SHA-256 identical between baseline and variant
(§3.3), and compute + prove equality of the ten-surface
``hydrologic_core_fingerprint`` (§3.4, docs §Gate G10).

Public entry points
-------------------
* :func:`copy_and_rewrite_sp_att_forc` — §3.1 orchestrator.
    1. Pre-SHA-256 the baseline ``.sp.att`` (INV-1 anchor).
    2. Parse the baseline row-by-row (schema + tokens).
    3. Validate the ``ownership`` sequence and ``shud_forcing_index`` mapping.
    4. Build new rows by looking up
       ``element_id -> ownership[element_id].grid_cell_id ->
       shud_forcing_index[grid_cell_id]``. Association is by element ID
       (INDEX column), never by row order.
    5. Prove non-``FORC`` columns unchanged in-memory (fail-closed BEFORE any
       variant byte hits disk).
    6. Emit a parse-level semantic diff artifact (FORC deltas only, keyed
       by element ID, sorted ascending).
    7. Serialize the new content atomically to a temp path, compute the
       variant SHA-256, then ``os.replace`` into ``variant_att_path``.
    8. Post-SHA-256 the baseline; raise
       :class:`BaselineImmutabilityViolationError` if it differs from the
       pre-SHA-256 (INV-1 hard block). On raise the variant file is removed.
    9. Return :class:`SpAttRewriteReport` carrying old/new SHA-256, sizes,
       semantic diff, and used-cell count.
* :func:`verify_non_forc_columns_unchanged` — G4 standalone gate: parses
  baseline + variant and asserts equal schema, row count, element-ID set,
  and byte + semantic equality of all non-``FORC`` column tokens keyed by
  element ID.
* :func:`verify_non_sp_att_checksums_equal` — §3.3 G4 file-checksum gate:
  asserts the variant's ``.sp.mesh``, river topology, lake topology, soil,
  geol, land, and calibration file SHA-256 checksums are byte-identical to
  the baseline's; fail-closed on any inequality BEFORE any variant asset is
  written.
* :func:`compute_hydrologic_core_fingerprint` — §3.4 computation of the
  domain-separated 10-surface fingerprint (mesh topology, river/lake
  topology, ``.sp.att`` non-``FORC`` fields, soil/geol/land, calibration,
  state vector schema, solver-relevant configuration) per docs §Gate G10.
  Returns :class:`HydrologicCoreFingerprint` for SUB-13 to record verbatim.
* :func:`verify_hydrologic_core_fingerprint_equal` — §3.4 equality gate:
  computes the fingerprint on baseline + variant packages, asserts equality,
  and returns the shared :class:`HydrologicCoreFingerprint` on pass. Raises
  :class:`HydrologicCoreFingerprintMismatchError` on drift.
* :func:`emit_semantic_diff` — parse-level FORC-only diff artifact from
  two sequences of :class:`SpAttForcRow`; deterministic ordering by
  ``element_id`` ascending for byte-identical reproducibility.
* :func:`record_sp_att_checksums` — SHA-256 + size record for both files.
* :func:`parse_sp_att_forc_rows` — helper that reads a ``.sp.att`` and
  yields :class:`SpAttForcRow` records (element_id, FORC) so callers can
  feed :func:`emit_semantic_diff` directly from disk.

Invariants
----------
* INV-1 (baseline read-only): ``baseline_att_path`` is opened read-only
  throughout. Pre + post SHA-256 are computed and compared; any drift
  raises :class:`BaselineImmutabilityViolationError`.
* INV-2 (variant path distinct from baseline): ``variant_att_path`` MUST
  resolve to a path different from ``baseline_att_path``. If callers pass
  the same path (via symlink, ``..``, or literal equality) the function
  refuses loudly rather than overwriting the baseline.

Exception family
----------------
:class:`SpAttRewriteError` is a distinct root — *not* a subclass of
:class:`workers.mapping_builder.integrity.BaselineIntegrityError` (G0/G1)
or :class:`workers.mapping_builder.algorithm.MappingAlgorithmError`
(G2/G3). G4 failures come from a different oracle (baseline file bytes
plus ownership/index consistency) than G0/G1 (baseline package integrity)
or G2/G3 (grid registry + WGS84 coverage). Keeping the roots distinct
lets callers differentiate the three families with dedicated ``except``
clauses.

Gate-naming convention (mapping_builder namespace)
--------------------------------------------------
Fail-closed invariant gates in :mod:`workers.mapping_builder` use the
``verify_*`` prefix uniformly. The return value discriminates outcome:

* ``None`` iff the gate passed with no artifact needed;
* a dataclass / artifact iff the gate passed with a caller-visible payload;
* ``raise`` iff the gate failed.

Applies to:
:func:`verify_non_forc_columns_unchanged` (this module, §3.2),
:func:`verify_non_sp_att_checksums_equal` (this module, §3.3),
:func:`verify_hydrologic_core_fingerprint_equal` (this module, §3.4),
:func:`workers.mapping_builder.verify_grid_identity_precondition`,
:func:`workers.mapping_builder.verify_package_crs`,
:func:`workers.mapping_builder.verify_g0_baseline`,
:func:`workers.mapping_builder.verify_g1_non_degenerate_triangles`,
:func:`workers.mapping_builder.verify_baseline_inv1_end_to_end`,
:func:`workers.mapping_builder.verify_half_cell_diagonal_sanity_bound`,
:func:`workers.mapping_builder.verify_small_basin_gate`.

:func:`compute_hydrologic_core_fingerprint` is a computation (not a gate)
and therefore uses the ``compute_*`` prefix — it never raises on drift, it
only produces the fingerprint. Drift detection is the caller's job (via
:func:`verify_hydrologic_core_fingerprint_equal` or a stored-vs-computed
comparison in SUB-13's evidence bundle).

Historical variants (``check_*``, ``enforce_*``, ``prove_*``) are
retired — the SUB-6/SUB-7/SUB-8/SUB-9 review loops converged on
``verify_*`` as the canonical gate-namespace prefix, and subsequent gates
SHALL adopt it directly at author time.

Gate orchestration (SUB-8 + SUB-9 -> SUB-13 deferral)
-----------------------------------------------------
Several ``verify_*`` gates ship in this module as standalone primitives:
:func:`verify_non_forc_columns_unchanged` (SUB-8, §3.2),
:func:`verify_non_sp_att_checksums_equal` (SUB-9, §3.3), and
:func:`verify_hydrologic_core_fingerprint_equal` (SUB-9, §3.4). They are
exported for callers (the mapping evidence bundler in SUB-13, standalone
auditors) but are **not yet** wired into :func:`copy_and_rewrite_sp_att_forc`'s
write pipeline. The §3.1 orchestrator still only runs its own in-memory
non-``FORC`` proof (``_verify_non_forc_columns_unchanged_in_memory``)
before writing the variant. Orchestrated "fail closed BEFORE write"
invocation of the §3.3 file-checksum gate and the §3.4 fingerprint gate
lands in SUB-13 when the full variant assembly step is scaffolded and the
mapping evidence package assembles the ordered gate results (G0–G5) into
one immutable bundle.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from workers.mapping_builder.algorithm import ElementOwnership

# --- exception family ------------------------------------------------------


class SpAttRewriteError(Exception):
    """Base class for §3.1 + §3.2 ``.sp.att`` rewrite failures.

    Distinct root class (not a subclass of
    :class:`workers.mapping_builder.integrity.BaselineIntegrityError` or
    :class:`workers.mapping_builder.algorithm.MappingAlgorithmError`).
    Callers that catch G4 rewrite failures MUST NOT accidentally absorb
    G0/G1 baseline errors or G2/G3 algorithm errors with the same ``except``
    clause; the mapping builder's fail-closed guarantee is meaningful only
    when callers can tell the three families apart.
    """


class BaselineImmutabilityViolationError(SpAttRewriteError):
    """Baseline ``.sp.att`` bytes changed between pre and post SHA-256.

    INV-1 hard block: the baseline ``.sp.att`` file MUST be treated as
    read-only. A drift between the pre-check SHA-256 (taken before the
    rewrite pipeline runs) and the post-check SHA-256 (taken after the
    variant is atomically written) indicates either an implementation bug
    that writes back to the baseline path, a race condition with an
    external mutator, or a compromised INV-2 check. Any of these is a G4
    blocker; the variant file is unlinked before this exception raises.
    """

    def __init__(
        self,
        *,
        baseline_path: pathlib.Path,
        pre_sha256: str,
        post_sha256: str,
    ) -> None:
        super().__init__(
            f"baseline {baseline_path} mutated during rewrite "
            f"(INV-1 violation): pre_sha256={pre_sha256!r} "
            f"post_sha256={post_sha256!r}"
        )
        self.baseline_path = baseline_path
        self.pre_sha256 = pre_sha256
        self.post_sha256 = post_sha256


class ForcOutOfRangeError(SpAttRewriteError):
    """A rewritten ``FORC`` value is outside ``1..used_cell_count``.

    Per spec §"Every rewritten `FORC` value is an integer in `1..N`"
    (§3.1): the mapping builder MUST fail closed before writing the
    variant when any ``shud_forcing_index`` value falls outside the legal
    binding domain. ``element_id`` and ``grid_cell_id`` are recorded when
    known (per-element path); the pre-validation path leaves ``element_id``
    as ``None`` because the offending value has no element yet.
    """

    def __init__(
        self,
        *,
        new_forc: int,
        valid_range: tuple[int, int],
        element_id: int | None = None,
        grid_cell_id: str | None = None,
    ) -> None:
        parts: list[str] = []
        if element_id is not None:
            parts.append(f"element_id={element_id}")
        if grid_cell_id is not None:
            parts.append(f"grid_cell_id={grid_cell_id!r}")
        parts.append(f"new FORC={new_forc}")
        parts.append(f"out of valid range {valid_range}")
        super().__init__(" ".join(parts))
        self.new_forc = new_forc
        self.valid_range = valid_range
        self.element_id = element_id
        self.grid_cell_id = grid_cell_id


class ForcNonIntegerError(SpAttRewriteError):
    """A ``shud_forcing_index`` value is not an integer.

    Per spec §"Every rewritten `FORC` value is an integer" (§3.1): the
    ``shud_forcing_index`` MUST map ``grid_cell_id -> int``. Float, str,
    None, or bool values are a caller bug — the canonical constructor is
    :func:`workers.mapping_builder.assign_shud_forcing_index`, which
    guarantees int values. Bool is rejected explicitly because ``bool`` is
    a subclass of ``int`` in Python and would otherwise silently pass.
    """

    def __init__(
        self,
        *,
        grid_cell_id: str,
        invalid_value: Any,
    ) -> None:
        super().__init__(
            f"shud_forcing_index[{grid_cell_id!r}]={invalid_value!r} "
            f"(type={type(invalid_value).__name__}) is not an integer"
        )
        self.grid_cell_id = grid_cell_id
        self.invalid_value = invalid_value


class ForcUnmappedError(SpAttRewriteError):
    """A baseline element_id has no ownership entry, or its cell has no index.

    Per spec §"Every baseline element_id is mapped" (§3.1): every element
    row in the baseline ``.sp.att`` MUST have a corresponding ownership
    record whose ``grid_cell_id`` is present in ``shud_forcing_index``.
    Missing ownership means the mapping algorithm silently dropped the
    element; missing index means the algorithm's ownership + index outputs
    are inconsistent. Either is a G4 blocker.
    """

    def __init__(
        self,
        *,
        element_id: int,
        grid_cell_id: str | None = None,
        detail: str = "no ownership entry for baseline element_id",
    ) -> None:
        if grid_cell_id is None:
            super().__init__(f"element_id={element_id}: {detail}")
        else:
            super().__init__(
                f"element_id={element_id} grid_cell_id={grid_cell_id!r}: "
                f"{detail}"
            )
        self.element_id = element_id
        self.grid_cell_id = grid_cell_id
        self.detail = detail


class ForcMultisetMismatchError(SpAttRewriteError):
    """The multiset of ``shud_forcing_index`` values is not ``1..used_cell_count``.

    Per spec §"The multiset of rewritten `FORC` values equals the ownership
    table's mapped `shud_forcing_index` list" (§3.1): the
    ``shud_forcing_index`` values MUST be exactly ``{1, 2, ..., N}`` where
    ``N == used_cell_count``. Duplicates, gaps, or missing values are a G4
    blocker even when every individual value is in range (a duplicate
    binding would silently map two cells to the same forcing index).
    """

    def __init__(
        self,
        *,
        expected_values: tuple[int, ...],
        observed_values: tuple[int, ...],
    ) -> None:
        super().__init__(
            f"shud_forcing_index values mismatch: expected sorted "
            f"{list(expected_values)!r}, observed sorted "
            f"{list(observed_values)!r}"
        )
        self.expected_values = expected_values
        self.observed_values = observed_values


class NonForcColumnChangedError(SpAttRewriteError):
    """A non-``FORC`` column token differs between baseline and variant.

    Per spec §"For all columns except FORC, old_att equals new_att at parse
    level (G4 proof)" (§3.2): the G4 non-``FORC``-unchanged proof compares
    every non-``FORC`` column token for every element (keyed by element ID,
    not row position). Any inequality is a G4 blocker; the variant is not
    written by :func:`copy_and_rewrite_sp_att_forc` when the in-memory
    proof detects a violation.
    """

    def __init__(
        self,
        *,
        element_id: int,
        column_name: str,
        baseline_value: str,
        variant_value: str,
    ) -> None:
        super().__init__(
            f"element_id={element_id} non-FORC column {column_name!r} "
            f"changed: baseline={baseline_value!r} variant={variant_value!r} "
            "(G4 non-FORC-unchanged proof violation)"
        )
        self.element_id = element_id
        self.column_name = column_name
        self.baseline_value = baseline_value
        self.variant_value = variant_value


class RowCountMismatchError(SpAttRewriteError):
    """Baseline and variant ``.sp.att`` have different row counts."""

    def __init__(
        self,
        *,
        baseline_count: int,
        variant_count: int,
    ) -> None:
        super().__init__(
            f"row count mismatch: baseline={baseline_count} "
            f"variant={variant_count} (G4 proof violation)"
        )
        self.baseline_count = baseline_count
        self.variant_count = variant_count


class ElementIdSetMismatchError(SpAttRewriteError):
    """Baseline and variant element-ID sets differ."""

    def __init__(
        self,
        *,
        baseline_only: tuple[int, ...],
        variant_only: tuple[int, ...],
    ) -> None:
        super().__init__(
            f"element_id sets differ: baseline_only={list(baseline_only)} "
            f"variant_only={list(variant_only)} (G4 proof violation)"
        )
        self.baseline_only = baseline_only
        self.variant_only = variant_only


class SchemaMismatchError(SpAttRewriteError):
    """Baseline and variant schemas (column names) differ."""

    def __init__(
        self,
        *,
        baseline_schema: tuple[str, ...],
        variant_schema: tuple[str, ...],
    ) -> None:
        super().__init__(
            f"schema mismatch: baseline={list(baseline_schema)} "
            f"variant={list(variant_schema)} (G4 proof violation)"
        )
        self.baseline_schema = baseline_schema
        self.variant_schema = variant_schema


class NonSpAttChecksumMismatchError(SpAttRewriteError):
    """A non-``.sp.att`` hydrologic core file differs between baseline and variant.

    Per spec §"Mesh, river, lake, soil, geol, land, and calibration files are
    byte-identical to baseline" (§3.3): the variant model input package MUST
    contain these files byte-identical to the baseline. Any per-category
    SHA-256 inequality is a G4 blocker.

    ``category`` is the failing category label (one of
    :data:`NON_SP_ATT_CATEGORIES`). ``relative_path`` identifies the specific
    file (relative to each package root — the file MUST exist at the same
    relative path in both packages, otherwise :class:`MissingPackageFileError`
    is raised BEFORE this check runs). The SHA-256 hex digests carry the
    fail-closed evidence: a downstream evidence bundler (SUB-13) records the
    exception attributes verbatim so the review trail names the specific
    file and the two conflicting digests.
    """

    def __init__(
        self,
        *,
        category: str,
        relative_path: str,
        baseline_sha256: str,
        variant_sha256: str,
    ) -> None:
        super().__init__(
            f"non-sp.att file checksum mismatch (category={category!r}, "
            f"path={relative_path!r}): baseline_sha256={baseline_sha256!r} "
            f"variant_sha256={variant_sha256!r} (G4 asset delta violation)"
        )
        self.category = category
        self.relative_path = relative_path
        self.baseline_sha256 = baseline_sha256
        self.variant_sha256 = variant_sha256


class MissingPackageFileError(SpAttRewriteError):
    """A category file declared for §3.3/§3.4 is missing from a package root.

    Per §3.3 the variant MUST contain the same hydrologic core files as the
    baseline. If a declared relative path is missing under either package
    root, the checksum comparison cannot proceed — fail closed with an
    actionable message identifying the missing file and which side it's
    missing from.

    ``missing_side`` vocabulary:

    * ``"baseline"`` / ``"variant"`` — raised by the paired equality gates
      (:func:`verify_non_sp_att_checksums_equal`,
      :func:`verify_hydrologic_core_fingerprint_equal`), which know which
      of the two package roots owns the missing file.
    * ``"package"`` — the default when :func:`compute_hydrologic_core_fingerprint`
      is called standalone (no baseline/variant pairing context). Callers
      can override this by passing an explicit label to
      :func:`compute_hydrologic_core_fingerprint`'s ``side_label`` kwarg.
    """

    def __init__(
        self,
        *,
        category: str,
        relative_path: str,
        missing_side: str,  # "baseline" | "variant" | "package"
        package_root: pathlib.Path,
    ) -> None:
        super().__init__(
            f"non-sp.att category={category!r} declared file "
            f"{relative_path!r} missing from {missing_side} package root "
            f"{package_root} (G4 asset delta violation — the variant MUST "
            "carry the same hydrologic core files as the baseline)"
        )
        self.category = category
        self.relative_path = relative_path
        self.missing_side = missing_side
        self.package_root = package_root


class UnknownCategoryError(SpAttRewriteError):
    """A supplied category label is not in :data:`NON_SP_ATT_CATEGORIES`.

    Guards against typos and out-of-scope categories: the §3.3 gate covers
    exactly the seven declared categories, and the §3.4 fingerprint covers
    those seven plus three (``state_schema``, ``solver_config``,
    ``sp_att_non_forc``). Any other label indicates a caller bug and is
    refused before file bytes are read, so downstream SUB-13 evidence never
    binds to an off-schema fingerprint.
    """

    def __init__(
        self,
        *,
        supplied_category: str,
        allowed_categories: tuple[str, ...],
    ) -> None:
        super().__init__(
            f"unknown category {supplied_category!r}; expected one of "
            f"{list(allowed_categories)!r}"
        )
        self.supplied_category = supplied_category
        self.allowed_categories = allowed_categories


class MissingCategoryError(SpAttRewriteError):
    """A required category is absent from the caller-supplied mapping.

    :func:`verify_non_sp_att_checksums_equal` requires the full set of
    :data:`NON_SP_ATT_CATEGORIES`; :func:`compute_hydrologic_core_fingerprint`
    similarly requires the full seven for the file-based portion of the
    fingerprint. Missing coverage is a caller bug that would silently
    exclude an in-scope surface from the G4 proof — refuse loudly.
    """

    def __init__(
        self,
        *,
        missing_categories: tuple[str, ...],
        required_categories: tuple[str, ...],
    ) -> None:
        super().__init__(
            f"missing category coverage: {list(missing_categories)!r}; "
            f"required={list(required_categories)!r}"
        )
        self.missing_categories = missing_categories
        self.required_categories = required_categories


class HydrologicCoreFingerprintMismatchError(SpAttRewriteError):
    """Baseline and variant hydrologic core fingerprints differ.

    Per spec §"hydrologic_core_fingerprint equals the baseline's" (§3.4)
    and docs §Gate G10: the ten-surface fingerprint (mesh topology,
    river/lake topology, ``.sp.att`` non-``FORC`` fields, soil/geol/land,
    calibration, state vector schema, solver-relevant configuration) MUST
    be byte-identical between baseline and variant. Any drift is a G4
    blocker with no variant asset written.

    Both fingerprint attributes are supplied so a downstream evidence
    bundler (SUB-13) can record the two SHA-256 digests verbatim and, via
    :attr:`baseline_covered_paths` / :attr:`variant_covered_paths`, prove
    the mismatch was not caused by a coverage discrepancy but by a bytes
    drift in one of the covered surfaces. The attribute names mirror the
    :class:`HydrologicCoreFingerprint` ``covered_paths`` field so the
    exception carries the raw ``covered_paths`` tuples for both sides.
    """

    def __init__(
        self,
        *,
        baseline_fingerprint_hash: str,
        variant_fingerprint_hash: str,
        baseline_covered_paths: tuple[str, ...],
        variant_covered_paths: tuple[str, ...],
    ) -> None:
        super().__init__(
            f"hydrologic_core_fingerprint mismatch: "
            f"baseline={baseline_fingerprint_hash!r} "
            f"variant={variant_fingerprint_hash!r} "
            f"(baseline_paths={list(baseline_covered_paths)}, "
            f"variant_paths={list(variant_covered_paths)}) "
            "(G4 asset delta / docs §G10 violation)"
        )
        self.baseline_fingerprint_hash = baseline_fingerprint_hash
        self.variant_fingerprint_hash = variant_fingerprint_hash
        self.baseline_covered_paths = baseline_covered_paths
        self.variant_covered_paths = variant_covered_paths


# --- category constants (§3.3 + §3.4 coverage sets) ----------------------

# The 7 non-``.sp.att`` hydrologic core file categories covered by the G4
# checksum-equality gate (§3.3). Alphabetized so the coverage set is a
# stable, review-friendly tuple.
NON_SP_ATT_CATEGORIES: tuple[str, ...] = (
    "calibration",
    "geol",
    "lake",
    "land",
    "mesh",
    "river",
    "soil",
)

# The 10 covered surfaces of the ``hydrologic_core_fingerprint`` (§3.4 +
# docs §Gate G10): the 7 file categories above plus ``.sp.att`` non-``FORC``
# fields, state vector schema, and solver-relevant configuration.
# Alphabetized for deterministic domain-separated hashing.
HYDROLOGIC_CORE_FINGERPRINT_LABELS: tuple[str, ...] = (
    "calibration",
    "geol",
    "lake",
    "land",
    "mesh",
    "river",
    "soil",
    "solver_config",
    "sp_att_non_forc",
    "state_schema",
)


# --- structured output dataclasses ---------------------------------------


@dataclass(frozen=True)
class SpAttChecksums:
    """SHA-256 hex + size (bytes) record for baseline + variant ``.sp.att``.

    All fields are immutable so downstream evidence can bind the record
    byte-for-byte. Sizes are in bytes as reported by ``os.stat``. The
    two SHA-256 hex strings are 64 lowercase hex characters each.
    """

    baseline_sha256: str
    variant_sha256: str
    baseline_size: int
    variant_size: int


@dataclass(frozen=True)
class SemanticDiffEntry:
    """One ``FORC`` delta: ``element_id`` + old FORC + new FORC.

    Frozen so downstream evidence can bind the row byte-for-byte. Only
    rows whose ``FORC`` actually changed appear in a :class:`SemanticDiff`;
    rows where ``old_forc == new_forc`` are omitted (the diff artifact
    records only the deltas, per spec §"only FORC changes").
    """

    element_id: int
    old_forc: int
    new_forc: int


@dataclass(frozen=True)
class SemanticDiff:
    """Parse-level ``FORC``-only diff artifact.

    ``entries`` is sorted by ``element_id`` ascending for byte-identical
    reproducibility across runs (spec §7 determinism requirement). Empty
    ``entries`` is legal — it means the rewrite made no ``FORC`` changes
    (e.g. a re-run of an already-applied mapping).
    """

    entries: tuple[SemanticDiffEntry, ...]


@dataclass(frozen=True)
class SpAttForcRow:
    """One ``.sp.att`` row's ``element_id`` + ``FORC`` value.

    Input type for :func:`emit_semantic_diff`. Callers can construct a
    tuple of these directly (e.g. from an in-memory model) or via
    :func:`parse_sp_att_forc_rows` (from disk).
    """

    element_id: int
    forc: int


@dataclass(frozen=True)
class SpAttRewriteReport:
    """Top-level return of :func:`copy_and_rewrite_sp_att_forc`.

    Frozen so downstream evidence (SUB-13 mapping evidence package) can
    bind the record byte-for-byte to the variant asset checksum.
    """

    checksums: SpAttChecksums
    semantic_diff: SemanticDiff
    rewritten_row_count: int
    used_cell_count: int


@dataclass(frozen=True)
class HydrologicCoreFingerprint:
    """Result of :func:`compute_hydrologic_core_fingerprint` (§3.4 / docs §G10).

    Frozen so downstream evidence (SUB-13 mapping evidence package) can
    record the two fields verbatim — SUB-13 does not recompute the
    fingerprint from its inputs; it stores what the builder produced,
    with the covered_paths tuple providing the audit trail of every
    surface that fed the hash.

    Attributes
    ----------
    hash:
        The SHA-256 hex digest (64 lowercase hex chars) computed over the
        domain-separated concatenation of per-surface entries in
        :data:`HYDROLOGIC_CORE_FINGERPRINT_LABELS` order.
    covered_paths:
        Immutable tuple of ``f"{label}:{descriptor}"`` strings, one per
        entry that fed the hash. For file categories the descriptor is
        the relative path (or a semicolon-joined list of sorted relative
        paths for multi-file categories). For ``state_schema`` and
        ``solver_config`` the descriptor is ``"<bytes>"``. For
        ``sp_att_non_forc`` the descriptor is the ``.sp.att`` relative
        path. Sorted alphabetically for determinism, so two runs on the
        same inputs produce a byte-identical dataclass.
    """

    hash: str
    covered_paths: tuple[str, ...]


# --- internal parser ------------------------------------------------------


@dataclass(frozen=True)
class _ParsedSpAtt:
    """Internal parse-level record of a ``.sp.att`` file.

    Preserves line terminators so :func:`copy_and_rewrite_sp_att_forc` can
    reconstruct byte-identical non-``FORC`` content when writing the variant.
    """

    schema: tuple[str, ...]
    forc_col_index: int
    n_rows: int
    header_line: str  # line 1 content (no line terminator)
    column_header_line: str  # line 2 content (no line terminator)
    header_terminator: str  # line 1 terminator, preserved verbatim
    column_header_terminator: str  # line 2 terminator, preserved verbatim
    raw_data_lines: tuple[str, ...]  # data rows with their line terminators
    parsed_rows: tuple[tuple[str, ...], ...]  # tokenized rows in file order
    trailing_content: str  # any bytes after the last data row


def _sha256_file(path: pathlib.Path) -> str:
    """Compute SHA-256 hex digest of a file, chunked read (INV-1)."""
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _read_bytes_and_split_lines(path: pathlib.Path) -> list[str]:
    """Read a file as bytes, decode UTF-8, return keepends-True line list.

    Preserves line terminators so :func:`_parse_sp_att_file` can reconstruct
    the file byte-for-byte when writing the variant.
    """
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SpAttRewriteError(
            f"{path}: non-utf-8 bytes: {exc}"
        ) from exc
    return text.splitlines(keepends=True)


def _parse_header_counts(header_line: str) -> tuple[int, int]:
    """Parse ``<N_rows>\\t<N_cols>`` header line, tolerating trailing whitespace."""
    tokens = header_line.split()
    if len(tokens) < 2:
        raise SpAttRewriteError(
            f"header line {header_line!r} does not carry <N_rows> <N_cols>"
        )
    try:
        n_rows = int(tokens[0])
        n_cols = int(tokens[1])
    except ValueError as exc:
        raise SpAttRewriteError(
            f"header line {header_line!r} tokens are not integers: {exc}"
        ) from exc
    if n_rows <= 0 or n_cols <= 0:
        raise SpAttRewriteError(
            f"header counts must be positive, got n_rows={n_rows} n_cols={n_cols}"
        )
    return n_rows, n_cols


def _parse_sp_att_file(path: pathlib.Path) -> _ParsedSpAtt:
    """Parse a ``.sp.att`` file into an immutable :class:`_ParsedSpAtt`.

    Follows the SHUD ``.sp.att`` layout documented in
    :mod:`workers.mapping_builder.integrity`:

    * Line 1: ``<N_rows>\\t<N_cols>`` (may carry trailing whitespace).
    * Line 2: column header (first column ``INDEX``, must include ``FORC``).
    * Lines 3..N+2: data rows.

    Preserves raw line bytes so the variant writer can reconstruct
    byte-identical non-``FORC`` content by only surgically replacing the
    ``FORC`` token in each row.
    """
    if not path.exists() or not path.is_file():
        raise SpAttRewriteError(
            f".sp.att does not exist or is not a file: {path}"
        )
    lines_with_terms = _read_bytes_and_split_lines(path)
    if len(lines_with_terms) < 2:
        raise SpAttRewriteError(
            f"{path.name}: too short — needs at least header + column header"
        )

    header_full = lines_with_terms[0]
    column_header_full = lines_with_terms[1]

    header_content = header_full.rstrip("\r\n")
    header_terminator = header_full[len(header_content):]
    column_header_content = column_header_full.rstrip("\r\n")
    column_header_terminator = column_header_full[len(column_header_content):]

    n_rows, n_cols = _parse_header_counts(header_content)

    header_tokens = column_header_content.split()
    if len(header_tokens) < n_cols:
        raise SpAttRewriteError(
            f"{path.name}: column header row has {len(header_tokens)} tokens, "
            f"expected >= {n_cols}"
        )
    schema = tuple(header_tokens[:n_cols])
    schema_upper = tuple(t.upper() for t in schema)
    if schema_upper[0] != "INDEX":
        raise SpAttRewriteError(
            f"{path.name}: expected first column 'INDEX', got {schema[0]!r}"
        )
    try:
        forc_col_index = schema_upper.index("FORC")
    except ValueError as exc:
        raise SpAttRewriteError(
            f"{path.name}: no 'FORC' column found in header {list(schema)!r}"
        ) from exc

    row_start = 2
    row_end = row_start + n_rows
    if len(lines_with_terms) < row_end:
        raise SpAttRewriteError(
            f"{path.name}: expected {n_rows} data rows, only "
            f"{len(lines_with_terms) - row_start} present"
        )

    raw_data_lines: list[str] = []
    parsed_rows: list[tuple[str, ...]] = []
    for row_index in range(row_start, row_end):
        raw_line = lines_with_terms[row_index]
        content = raw_line.rstrip("\r\n")
        if not content.strip():
            raise SpAttRewriteError(
                f"{path.name}: blank data row at line {row_index + 1}"
            )
        tokens = content.split()
        if len(tokens) < len(schema):
            raise SpAttRewriteError(
                f"{path.name}: data row {row_index + 1} has {len(tokens)} "
                f"tokens, expected >= {len(schema)}"
            )
        # Fail-closed on extra tokens per row: the schema declares exactly
        # ``len(schema)`` columns, so any additional non-whitespace token in
        # a data row is a schema violation that would otherwise be silently
        # truncated by ``tokens[: len(schema)]`` below. A drifted schema is
        # a G4 blocker — we cannot claim byte-level non-``FORC`` equality
        # if the parser silently drops trailing columns.
        if len(tokens) > len(schema):
            raise SpAttRewriteError(
                f"{path.name}: data row {row_index + 1} has {len(tokens)} "
                f"tokens, expected exactly {len(schema)} per declared "
                f"schema {list(schema)!r} — extra tokens rejected to "
                "prevent silent schema drift"
            )
        try:
            int(tokens[0])
        except ValueError as exc:
            raise SpAttRewriteError(
                f"{path.name}: data row {row_index + 1} first token "
                f"{tokens[0]!r} is not an integer: {exc}"
            ) from exc
        raw_data_lines.append(raw_line)
        parsed_rows.append(tuple(tokens[: len(schema)]))

    # Fail-closed on extra data rows past ``n_rows``: any additional
    # non-blank line past the declared row count is a structural
    # inconsistency that would otherwise be stashed away in
    # ``trailing_content`` and silently preserved in the variant. The
    # header declares n_rows; the file MUST NOT contain undeclared data.
    for extra_index in range(row_end, len(lines_with_terms)):
        extra_line = lines_with_terms[extra_index]
        if extra_line.rstrip("\r\n").strip():
            raise SpAttRewriteError(
                f"{path.name}: found non-blank content at line "
                f"{extra_index + 1} past declared n_rows={n_rows} "
                "(row_end); extra data rows rejected to prevent silent "
                "row-count drift"
            )

    trailing_content = "".join(lines_with_terms[row_end:])

    return _ParsedSpAtt(
        schema=schema,
        forc_col_index=forc_col_index,
        n_rows=n_rows,
        header_line=header_content,
        column_header_line=column_header_content,
        header_terminator=header_terminator,
        column_header_terminator=column_header_terminator,
        raw_data_lines=tuple(raw_data_lines),
        parsed_rows=tuple(parsed_rows),
        trailing_content=trailing_content,
    )


def _replace_forc_token_in_row(
    raw_line: str,
    forc_col_index: int,
    new_forc: int,
) -> str:
    """Return ``raw_line`` with the ``FORC`` token surgically replaced.

    Uses :func:`re.findall` with ``r'\\S+|\\s+'`` to yield alternating token
    and whitespace runs, then replaces only the token at position
    ``forc_col_index`` (0-based among tokens). Every other token AND every
    whitespace run (including the trailing line terminator) is preserved
    byte-for-byte. This is the mechanism that guarantees non-``FORC`` column
    bytes stay identical between baseline and variant.
    """
    parts = re.findall(r'\S+|\s+', raw_line)
    token_indices = [i for i, p in enumerate(parts) if not p.isspace()]
    if forc_col_index >= len(token_indices):
        raise SpAttRewriteError(
            f"row {raw_line!r} has {len(token_indices)} tokens, "
            f"cannot replace FORC at index {forc_col_index}"
        )
    parts[token_indices[forc_col_index]] = str(new_forc)
    return "".join(parts)


# --- public entry points --------------------------------------------------


def record_sp_att_checksums(
    baseline_att_path: pathlib.Path,
    variant_att_path: pathlib.Path,
) -> SpAttChecksums:
    """Record SHA-256 hex + size (bytes) for baseline + variant ``.sp.att``.

    Reads both files read-only (INV-1). Neither path is modified.

    Parameters
    ----------
    baseline_att_path, variant_att_path:
        Paths to the baseline and variant ``.sp.att`` files. Both MUST exist.

    Returns
    -------
    SpAttChecksums
        Immutable record with baseline_sha256, variant_sha256,
        baseline_size, variant_size.

    Raises
    ------
    SpAttRewriteError
        Either path does not exist or is not a regular file.
    """
    if not baseline_att_path.exists() or not baseline_att_path.is_file():
        raise SpAttRewriteError(
            f"baseline .sp.att does not exist or is not a file: {baseline_att_path}"
        )
    if not variant_att_path.exists() or not variant_att_path.is_file():
        raise SpAttRewriteError(
            f"variant .sp.att does not exist or is not a file: {variant_att_path}"
        )
    return SpAttChecksums(
        baseline_sha256=_sha256_file(baseline_att_path),
        variant_sha256=_sha256_file(variant_att_path),
        baseline_size=baseline_att_path.stat().st_size,
        variant_size=variant_att_path.stat().st_size,
    )


def parse_sp_att_forc_rows(path: pathlib.Path) -> tuple[SpAttForcRow, ...]:
    """Parse a ``.sp.att`` file and return ``(element_id, forc)`` rows.

    Rows are returned in file order — the caller may reorder them if
    needed. Useful for feeding :func:`emit_semantic_diff` directly from
    disk.
    """
    parsed = _parse_sp_att_file(path)
    rows: list[SpAttForcRow] = []
    for token_row in parsed.parsed_rows:
        try:
            element_id = int(token_row[0])
            forc = int(token_row[parsed.forc_col_index])
        except (ValueError, IndexError) as exc:
            raise SpAttRewriteError(
                f"{path.name}: cannot parse element_id/FORC from row "
                f"{token_row!r}: {exc}"
            ) from exc
        rows.append(SpAttForcRow(element_id=element_id, forc=forc))
    return tuple(rows)


def emit_semantic_diff(
    baseline_rows: Sequence[SpAttForcRow],
    variant_rows: Sequence[SpAttForcRow],
) -> SemanticDiff:
    """Emit a parse-level semantic diff artifact showing only ``FORC`` changes.

    Per spec §"A parse-level semantic diff artifact is produced showing
    only FORC changes" (§3.2): the diff MUST contain only
    ``element_id`` + ``old_forc`` + ``new_forc`` for each row where the
    ``FORC`` value changed. No whitespace, column-header, or other noise.

    Entries are sorted by ``element_id`` ascending for byte-identical
    reproducibility (spec §7 determinism).

    Parameters
    ----------
    baseline_rows, variant_rows:
        Sequences of :class:`SpAttForcRow`. Must have equal element-ID
        sets; otherwise raises :class:`ElementIdSetMismatchError`.

    Returns
    -------
    SemanticDiff
        Immutable diff artifact with entries sorted by ``element_id``
        ascending. Empty entries tuple is legal — it means no ``FORC``
        changed (e.g. a re-run of an already-applied mapping).

    Raises
    ------
    ElementIdSetMismatchError
        ``baseline_rows`` and ``variant_rows`` have different element-ID
        sets — the diff is undefined in that case.
    SpAttRewriteError
        Either sequence contains a duplicate ``element_id`` (a caller bug
        — ``.sp.att`` guarantees unique element IDs).
    """
    baseline_by_id: dict[int, int] = {}
    for row in baseline_rows:
        if row.element_id in baseline_by_id:
            raise SpAttRewriteError(
                f"baseline_rows contains duplicate element_id={row.element_id}"
            )
        baseline_by_id[row.element_id] = row.forc
    variant_by_id: dict[int, int] = {}
    for row in variant_rows:
        if row.element_id in variant_by_id:
            raise SpAttRewriteError(
                f"variant_rows contains duplicate element_id={row.element_id}"
            )
        variant_by_id[row.element_id] = row.forc

    baseline_ids = set(baseline_by_id)
    variant_ids = set(variant_by_id)
    if baseline_ids != variant_ids:
        raise ElementIdSetMismatchError(
            baseline_only=tuple(sorted(baseline_ids - variant_ids)),
            variant_only=tuple(sorted(variant_ids - baseline_ids)),
        )
    entries: list[SemanticDiffEntry] = []
    for element_id in sorted(baseline_by_id):
        old = baseline_by_id[element_id]
        new = variant_by_id[element_id]
        if old != new:
            entries.append(
                SemanticDiffEntry(
                    element_id=element_id,
                    old_forc=old,
                    new_forc=new,
                )
            )
    return SemanticDiff(entries=tuple(entries))


def verify_non_forc_columns_unchanged(
    baseline_att_path: pathlib.Path,
    variant_att_path: pathlib.Path,
) -> None:
    """Verify baseline and variant ``.sp.att`` differ only in ``FORC``.

    G4 non-``FORC``-unchanged gate — callable standalone for post-hoc
    verification (e.g. by an evidence bundler). Parses both files and
    asserts:

    * Equal schema (column names + order).
    * Equal row count.
    * Equal element-ID set.
    * For every element ID, all non-``FORC`` column tokens are byte
      identical between baseline and variant.

    Per spec §"For all columns except FORC, old_att equals new_att at parse
    level (G4 proof)" (§3.2): raises the corresponding
    :class:`SpAttRewriteError` subclass on the first violation.

    Parameters
    ----------
    baseline_att_path, variant_att_path:
        Paths to the baseline and variant ``.sp.att`` files.

    Raises
    ------
    SchemaMismatchError
        Column names or order differ.
    RowCountMismatchError
        Row counts differ.
    ElementIdSetMismatchError
        Element-ID sets differ.
    NonForcColumnChangedError
        A non-``FORC`` column token differs (byte + semantic).
    SpAttRewriteError
        Baseline or variant is unparseable.
    """
    baseline = _parse_sp_att_file(baseline_att_path)
    variant = _parse_sp_att_file(variant_att_path)

    if baseline.schema != variant.schema:
        raise SchemaMismatchError(
            baseline_schema=baseline.schema,
            variant_schema=variant.schema,
        )
    if baseline.n_rows != variant.n_rows:
        raise RowCountMismatchError(
            baseline_count=baseline.n_rows,
            variant_count=variant.n_rows,
        )

    baseline_by_id = {int(row[0]): row for row in baseline.parsed_rows}
    variant_by_id = {int(row[0]): row for row in variant.parsed_rows}
    baseline_ids = set(baseline_by_id)
    variant_ids = set(variant_by_id)
    if baseline_ids != variant_ids:
        raise ElementIdSetMismatchError(
            baseline_only=tuple(sorted(baseline_ids - variant_ids)),
            variant_only=tuple(sorted(variant_ids - baseline_ids)),
        )
    forc_idx = baseline.forc_col_index
    for element_id in sorted(baseline_ids):
        b_row = baseline_by_id[element_id]
        v_row = variant_by_id[element_id]
        for col_idx in range(len(baseline.schema)):
            if col_idx == forc_idx:
                continue
            if b_row[col_idx] != v_row[col_idx]:
                raise NonForcColumnChangedError(
                    element_id=element_id,
                    column_name=baseline.schema[col_idx],
                    baseline_value=b_row[col_idx],
                    variant_value=v_row[col_idx],
                )


# --- §3.3 non-.sp.att file-checksum equality gate (G4) --------------------


def _validate_category_files(
    category_files: Mapping[str, Sequence[str]],
    *,
    required_categories: tuple[str, ...],
) -> None:
    """Validate the caller-supplied category-to-relative-paths mapping.

    Raises
    ------
    UnknownCategoryError
        A supplied category is not in ``required_categories``.
    MissingCategoryError
        A required category is absent from ``category_files``.
    SpAttRewriteError
        A category maps to an empty sequence (the gate has no file to hash).
    """
    supplied = set(category_files)
    required = set(required_categories)
    unknown = supplied - required
    if unknown:
        # Deterministic first-unknown reporting: sort to make the failing
        # label reproducible across runs / OS iteration orders.
        raise UnknownCategoryError(
            supplied_category=sorted(unknown)[0],
            allowed_categories=required_categories,
        )
    missing = required - supplied
    if missing:
        raise MissingCategoryError(
            missing_categories=tuple(sorted(missing)),
            required_categories=required_categories,
        )
    # Empty per-category coverage would silently skip the check.
    for label in required_categories:
        if not tuple(category_files[label]):
            raise SpAttRewriteError(
                f"category {label!r} has no declared files; the §3.3/§3.4 "
                "gate requires at least one file per covered category"
            )


def _resolve_and_sha256(
    package_root: pathlib.Path,
    relative_path: str,
    *,
    category: str,
    missing_side: str,
) -> str:
    """Resolve ``relative_path`` under ``package_root`` and return its SHA-256.

    Raises
    ------
    MissingPackageFileError
        The resolved path does not exist or is not a regular file.
    """
    target = package_root / relative_path
    if not target.exists() or not target.is_file():
        raise MissingPackageFileError(
            category=category,
            relative_path=relative_path,
            missing_side=missing_side,
            package_root=package_root,
        )
    return _sha256_file(target)


def verify_non_sp_att_checksums_equal(
    baseline_package_root: pathlib.Path,
    variant_package_root: pathlib.Path,
    *,
    category_files: Mapping[str, Sequence[str]],
) -> None:
    """§3.3 G4 file-checksum equality gate for the 7 non-``.sp.att`` categories.

    Asserts that every declared file in every declared category has an equal
    SHA-256 between the baseline and variant packages. Per spec §"Mesh,
    river, lake, soil, geol, land, and calibration files are byte-identical
    to baseline" (§3.3): the variant model input package MUST NOT alter any
    of these files relative to the baseline; only the ``.sp.att`` rewrite,
    the direct-grid binding artifact, and the manifest are allowed to
    change.

    Fail-closed guarantee: on any inequality (or on any missing/undeclared
    category) this function raises BEFORE any downstream variant assembly
    step runs. Callers MUST invoke this gate *before* writing any variant
    package output; a G4 blocker raised here means the variant assembly
    aborts with no partial artifact on disk.

    Parameters
    ----------
    baseline_package_root, variant_package_root:
        Absolute paths to the baseline and variant model input package
        roots. Both MUST exist and be directories.
    category_files:
        Mapping from category label (must be exactly the set
        :data:`NON_SP_ATT_CATEGORIES`) to a non-empty sequence of relative
        file paths under each package root. The same relative paths are
        used for both baseline and variant lookups — the gate proves the
        variant carries the *same* files as the baseline, not a
        substituted set.

    Raises
    ------
    UnknownCategoryError
        ``category_files`` contains a category not in
        :data:`NON_SP_ATT_CATEGORIES`.
    MissingCategoryError
        ``category_files`` is missing a required category from
        :data:`NON_SP_ATT_CATEGORIES`.
    MissingPackageFileError
        A declared relative path is absent from either package root.
    NonSpAttChecksumMismatchError
        A category file's SHA-256 differs between baseline and variant.
    SpAttRewriteError
        ``baseline_package_root`` or ``variant_package_root`` is not a
        directory, or a category maps to an empty sequence.
    """
    if not baseline_package_root.is_dir():
        raise SpAttRewriteError(
            f"baseline_package_root {baseline_package_root} is not a directory"
        )
    if not variant_package_root.is_dir():
        raise SpAttRewriteError(
            f"variant_package_root {variant_package_root} is not a directory"
        )
    _validate_category_files(
        category_files, required_categories=NON_SP_ATT_CATEGORIES
    )
    # Iterate categories in deterministic order so the first-mismatch report
    # is reproducible across runs (spec §7 determinism).
    for category in NON_SP_ATT_CATEGORIES:
        for relative_path in sorted(category_files[category]):
            baseline_sha = _resolve_and_sha256(
                baseline_package_root,
                relative_path,
                category=category,
                missing_side="baseline",
            )
            variant_sha = _resolve_and_sha256(
                variant_package_root,
                relative_path,
                category=category,
                missing_side="variant",
            )
            if baseline_sha != variant_sha:
                raise NonSpAttChecksumMismatchError(
                    category=category,
                    relative_path=relative_path,
                    baseline_sha256=baseline_sha,
                    variant_sha256=variant_sha,
                )


# --- §3.4 hydrologic_core_fingerprint (G10 coverage) ----------------------


def _sha256_bytes(data: bytes) -> str:
    """SHA-256 hex digest of an in-memory bytes buffer."""
    return hashlib.sha256(data).hexdigest()


def _compute_file_category_hash(
    package_root: pathlib.Path,
    category: str,
    relative_paths: Sequence[str],
    *,
    side_label: str,
) -> str:
    """SHA-256 over ``f"{path}\\t{file_sha256}\\n"`` per file, sorted by path.

    Multi-file categories (e.g. river = river.riv + river.rivseg) fold the
    per-file hashes into a per-category hash before entering the top-level
    fingerprint. Sorted-path order is the domain separator that prevents
    collisions between adjacent files in the same category.

    ``side_label`` propagates to :class:`MissingPackageFileError` so the
    paired equality gate can report ``"baseline"`` / ``"variant"`` while a
    standalone call uses the default ``"package"``.
    """
    per_file_entries: list[str] = []
    for relative_path in sorted(relative_paths):
        file_sha = _resolve_and_sha256(
            package_root,
            relative_path,
            category=category,
            missing_side=side_label,
        )
        per_file_entries.append(f"{relative_path}\t{file_sha}\n")
    joined = "".join(per_file_entries).encode("utf-8")
    return _sha256_bytes(joined)


def _canonicalize_sp_att_non_forc(sp_att_path: pathlib.Path) -> str:
    """Return SHA-256 over the ``.sp.att`` non-``FORC`` columns, canonicalized.

    The ``.sp.att`` file cannot be hashed whole because the FORC column
    changes intentionally between baseline and variant. Instead:

    1. Parse the file with :func:`_parse_sp_att_file` to obtain the schema
       and per-row tokens (already-validated for schema drift).
    2. Serialize each row's non-``FORC`` tokens keyed by ``element_id``,
       sorted by ``element_id`` ascending for determinism.
    3. Prefix the schema (all column names minus ``FORC``, in the schema's
       original order) so a column-name drift (e.g. renaming a non-FORC
       column while preserving its byte payload) cannot silently produce
       the same digest.
    4. SHA-256 the canonicalized string.

    This is the fingerprint contribution of the ``sp_att_non_forc``
    surface. It is symmetric to :func:`verify_non_forc_columns_unchanged`
    (which asserts equality at parse level between baseline and variant),
    but here we produce a compact hash suitable for the top-level
    fingerprint entry.
    """
    parsed = _parse_sp_att_file(sp_att_path)
    non_forc_col_indices = [
        i for i in range(len(parsed.schema)) if i != parsed.forc_col_index
    ]
    non_forc_schema = tuple(parsed.schema[i] for i in non_forc_col_indices)
    lines: list[str] = [
        "schema\t" + "\t".join(non_forc_schema) + "\n",
    ]
    rows_by_id: dict[int, tuple[str, ...]] = {}
    for row in parsed.parsed_rows:
        eid = int(row[0])
        rows_by_id[eid] = row
    for eid in sorted(rows_by_id):
        row = rows_by_id[eid]
        non_forc_tokens = [row[i] for i in non_forc_col_indices]
        lines.append(f"{eid}\t" + "\t".join(non_forc_tokens) + "\n")
    joined = "".join(lines).encode("utf-8")
    return _sha256_bytes(joined)


def compute_hydrologic_core_fingerprint(
    package_root: pathlib.Path,
    *,
    sp_att_path: pathlib.Path,
    category_files: Mapping[str, Sequence[str]],
    state_schema_bytes: bytes,
    solver_config_bytes: bytes,
    side_label: str = "package",
) -> HydrologicCoreFingerprint:
    """§3.4 / docs §G10 ten-surface ``hydrologic_core_fingerprint`` computation.

    Computes a domain-separated SHA-256 fingerprint over the ten covered
    surfaces enumerated in :data:`HYDROLOGIC_CORE_FINGERPRINT_LABELS`:

    * ``calibration``, ``geol``, ``lake``, ``land``, ``mesh``, ``river``,
      ``soil`` — file categories under ``category_files`` (same shape as
      :func:`verify_non_sp_att_checksums_equal`).
    * ``sp_att_non_forc`` — non-``FORC`` columns of ``sp_att_path``,
      canonicalized by element ID (see :func:`_canonicalize_sp_att_non_forc`).
    * ``state_schema`` — SHA-256 of ``state_schema_bytes`` (pluggable so
      SUB-13 can wire in the concrete state schema bytes when the runtime
      piece lands; per docs §G10 this is a shared platform-level surface).
    * ``solver_config`` — SHA-256 of ``solver_config_bytes`` (same
      rationale as ``state_schema_bytes``).

    Domain separation
    -----------------
    Each surface contributes exactly one line ``f"{label}\\t{hash}\\n"`` to
    the top-level buffer, sorted by ``label`` alphabetically. The final
    fingerprint is the SHA-256 of this UTF-8-encoded buffer. Because each
    entry carries an in-hex sub-hash (not raw bytes) and terminates with
    ``\\n``, adjacent-surface content shifting cannot collide the digest
    (raw-bytes concatenation would be collision-attackable — the docs
    require the fingerprint be a real invariance proof, not a hand-wave).

    The returned :class:`HydrologicCoreFingerprint`'s ``covered_paths``
    field enumerates ``f"{label}:{descriptor}"`` per surface so SUB-13 can
    record verbatim which surfaces fed the hash. Multi-file categories
    join their sorted relative paths with ``";"``; pluggable byte
    surfaces use ``"<bytes>"`` as the descriptor.

    Parameters
    ----------
    package_root:
        Absolute path to a model input package root (baseline or variant).
        MUST be an existing directory.
    sp_att_path:
        Absolute path to the package's ``.sp.att`` file (may be under
        ``package_root`` or a variant path; only its bytes matter here).
        MUST exist and parse cleanly.
    category_files:
        Mapping from each of the seven :data:`NON_SP_ATT_CATEGORIES` to a
        non-empty sequence of relative paths under ``package_root``.
    state_schema_bytes, solver_config_bytes:
        In-memory bytes for the state vector schema and solver-relevant
        configuration surfaces (per docs §G10). Empty bytes ARE legal
        (they hash to the SHA-256 of the empty string) but a caller
        supplying empty bytes on both packages will pass the equality
        gate — the intent is that SUB-13 supplies real bytes.
    side_label:
        Label carried into :class:`MissingPackageFileError`'s
        ``missing_side`` attribute when a declared file is absent under
        ``package_root``. Defaults to ``"package"`` for standalone
        callers; :func:`verify_hydrologic_core_fingerprint_equal` overrides
        this to ``"baseline"`` / ``"variant"`` so paired equality errors
        name the correct side.

    Returns
    -------
    HydrologicCoreFingerprint
        Immutable record with the fingerprint SHA-256 hex and the sorted
        covered_paths tuple.

    Raises
    ------
    UnknownCategoryError
        ``category_files`` contains a category not in
        :data:`NON_SP_ATT_CATEGORIES`.
    MissingCategoryError
        ``category_files`` is missing a required category from
        :data:`NON_SP_ATT_CATEGORIES`.
    MissingPackageFileError
        A declared relative path is absent from ``package_root``.
        ``missing_side`` reflects ``side_label``.
    SpAttRewriteError
        ``package_root`` is not a directory, ``sp_att_path`` is missing
        or unparseable, or a category maps to an empty sequence.
    """
    if not package_root.is_dir():
        raise SpAttRewriteError(
            f"package_root {package_root} is not a directory"
        )
    if not sp_att_path.exists() or not sp_att_path.is_file():
        raise SpAttRewriteError(
            f"sp_att_path {sp_att_path} does not exist or is not a file"
        )
    _validate_category_files(
        category_files, required_categories=NON_SP_ATT_CATEGORIES
    )
    # Per-surface hashes and descriptors. Descriptors feed covered_paths.
    per_surface_hash: dict[str, str] = {}
    per_surface_descriptor: dict[str, str] = {}
    for category in NON_SP_ATT_CATEGORIES:
        relative_paths = tuple(sorted(category_files[category]))
        per_surface_hash[category] = _compute_file_category_hash(
            package_root,
            category,
            relative_paths,
            side_label=side_label,
        )
        per_surface_descriptor[category] = ";".join(relative_paths)
    per_surface_hash["sp_att_non_forc"] = _canonicalize_sp_att_non_forc(
        sp_att_path
    )
    per_surface_descriptor["sp_att_non_forc"] = str(sp_att_path.name)
    per_surface_hash["state_schema"] = _sha256_bytes(state_schema_bytes)
    per_surface_descriptor["state_schema"] = "<bytes>"
    per_surface_hash["solver_config"] = _sha256_bytes(solver_config_bytes)
    per_surface_descriptor["solver_config"] = "<bytes>"

    # Sanity: every declared label appears exactly once.
    assert set(per_surface_hash) == set(HYDROLOGIC_CORE_FINGERPRINT_LABELS)

    # Domain-separated top-level hash. Iterate labels in alphabetical order
    # (already the case in HYDROLOGIC_CORE_FINGERPRINT_LABELS) so the
    # fingerprint is byte-identical across runs.
    top_lines = [
        f"{label}\t{per_surface_hash[label]}\n"
        for label in HYDROLOGIC_CORE_FINGERPRINT_LABELS
    ]
    fingerprint_hash = _sha256_bytes("".join(top_lines).encode("utf-8"))
    covered_paths = tuple(
        sorted(
            f"{label}:{per_surface_descriptor[label]}"
            for label in HYDROLOGIC_CORE_FINGERPRINT_LABELS
        )
    )
    return HydrologicCoreFingerprint(
        hash=fingerprint_hash, covered_paths=covered_paths
    )


def verify_hydrologic_core_fingerprint_equal(
    baseline_package_root: pathlib.Path,
    variant_package_root: pathlib.Path,
    *,
    baseline_sp_att_path: pathlib.Path,
    variant_sp_att_path: pathlib.Path,
    category_files: Mapping[str, Sequence[str]],
    baseline_state_schema_bytes: bytes,
    variant_state_schema_bytes: bytes,
    baseline_solver_config_bytes: bytes,
    variant_solver_config_bytes: bytes,
) -> HydrologicCoreFingerprint:
    """§3.4 G4 fingerprint-equality gate + shared-fingerprint return.

    Computes :func:`compute_hydrologic_core_fingerprint` on the baseline
    and variant packages, asserts equality, and returns the shared
    fingerprint on pass. Per spec §"hydrologic_core_fingerprint equals the
    baseline's" (§3.4) and docs §Gate G10, drift in any of the ten covered
    surfaces indicates the variant mutated a hydrologic core surface it
    was not supposed to touch, and is a G4 blocker with no variant asset
    written.

    Both packages use the SAME ``category_files`` mapping — the invariant
    is that the SAME relative paths under both roots hash the same. The
    ``state_schema_bytes`` and ``solver_config_bytes`` are per-package
    inputs so callers can inject drift for negative testing (in production
    both callers supply the same platform-level bytes to both fingerprint
    computations).

    Parameters
    ----------
    baseline_package_root, variant_package_root:
        Absolute paths to the baseline and variant model input package
        roots. Both MUST exist and be directories.
    baseline_sp_att_path, variant_sp_att_path:
        Absolute paths to the baseline and variant ``.sp.att`` files.
        Both MUST exist.
    category_files:
        Mapping from each of the seven :data:`NON_SP_ATT_CATEGORIES` to a
        non-empty sequence of relative paths. Same relative paths are
        used for both roots.
    baseline_state_schema_bytes, variant_state_schema_bytes:
        Per-package state vector schema bytes (see
        :func:`compute_hydrologic_core_fingerprint`).
    baseline_solver_config_bytes, variant_solver_config_bytes:
        Per-package solver-relevant configuration bytes (see
        :func:`compute_hydrologic_core_fingerprint`).

    Returns
    -------
    HydrologicCoreFingerprint
        The shared fingerprint (equal for baseline and variant on pass).
        SUB-13 records this verbatim in the mapping evidence.

    Raises
    ------
    HydrologicCoreFingerprintMismatchError
        Baseline and variant fingerprints differ (G4 blocker per §3.4 /
        docs §G10).
    UnknownCategoryError, MissingCategoryError, SpAttRewriteError:
        Propagated from the underlying
        :func:`compute_hydrologic_core_fingerprint` invocations.
    MissingPackageFileError
        A declared relative path is absent from either package root.
        ``missing_side`` is ``"baseline"`` when the baseline root is
        missing the file and ``"variant"`` when the variant root is
        missing it — the paired gate overrides the standalone
        ``"package"`` default so evidence trails name the correct side.
    """
    baseline_fp = compute_hydrologic_core_fingerprint(
        baseline_package_root,
        sp_att_path=baseline_sp_att_path,
        category_files=category_files,
        state_schema_bytes=baseline_state_schema_bytes,
        solver_config_bytes=baseline_solver_config_bytes,
        side_label="baseline",
    )
    variant_fp = compute_hydrologic_core_fingerprint(
        variant_package_root,
        sp_att_path=variant_sp_att_path,
        category_files=category_files,
        state_schema_bytes=variant_state_schema_bytes,
        solver_config_bytes=variant_solver_config_bytes,
        side_label="variant",
    )
    if baseline_fp.hash != variant_fp.hash:
        raise HydrologicCoreFingerprintMismatchError(
            baseline_fingerprint_hash=baseline_fp.hash,
            variant_fingerprint_hash=variant_fp.hash,
            baseline_covered_paths=baseline_fp.covered_paths,
            variant_covered_paths=variant_fp.covered_paths,
        )
    # Both fingerprints match — return one (they are byte-identical).
    return baseline_fp


def copy_and_rewrite_sp_att_forc(
    baseline_att_path: pathlib.Path,
    variant_att_path: pathlib.Path,
    ownership: Sequence[ElementOwnership],
    shud_forcing_index: Mapping[str, int],
    *,
    used_cell_count: int,
) -> SpAttRewriteReport:
    """Copy baseline ``.sp.att`` and rewrite ``FORC`` by element ID.

    Fail-closed guarantee: on any error the variant ``.sp.att`` is NOT
    written; if a variant temp file was created it is removed. The
    baseline ``.sp.att`` is opened read-only throughout and its pre/post
    SHA-256 are verified equal (INV-1). ``variant_att_path`` MUST differ
    from ``baseline_att_path`` (INV-2).

    Association is by element ID (the ``INDEX`` column), never by row
    order. A baseline whose rows are in scrambled order (e.g. 3, 1, 4, 2)
    receives the correct per-element ``FORC`` because the lookup key is
    the parsed ``element_id``.

    Pipeline (fail-closed at every gate):

    1. Resolve both paths and refuse if variant resolves to baseline
       (INV-2).
    2. Compute baseline pre-SHA-256 (INV-1 anchor).
    3. Parse the baseline ``.sp.att`` (schema + rows).
    4. Validate ``ownership`` (no duplicate ``element_id``) and
       ``shud_forcing_index`` (every value int, every value in
       ``[1, used_cell_count]``, sorted values equal
       ``(1, 2, ..., used_cell_count)``).
    5. For each baseline row, look up
       ``element_id -> ownership.grid_cell_id -> shud_forcing_index[grid_cell_id]``
       to obtain the new ``FORC``. Missing ownership or missing index
       raises :class:`ForcUnmappedError`.
    6. Prove non-``FORC`` columns unchanged in-memory (fail-closed before
       any variant byte hits disk).
    7. Emit the parse-level semantic diff (FORC deltas keyed by
       ``element_id``, sorted ascending).
    8. Serialize the new content to a temp file next to
       ``variant_att_path``, compute the variant SHA-256, then atomically
       ``os.replace`` into ``variant_att_path``.
    9. Compute baseline post-SHA-256 and assert equal to pre-SHA-256
       (INV-1 hard block); on mismatch, unlink the variant and raise
       :class:`BaselineImmutabilityViolationError`.
    10. Return :class:`SpAttRewriteReport`.

    Parameters
    ----------
    baseline_att_path:
        Path to the baseline ``.sp.att`` file. MUST exist and be readable.
    variant_att_path:
        Path where the variant ``.sp.att`` will be written. MUST resolve
        to a path different from ``baseline_att_path`` (INV-2). The parent
        directory is created if missing.
    ownership:
        Sequence of :class:`ElementOwnership` records produced by
        :func:`workers.mapping_builder.nearest_cell_barycenter_geodesic_v1`.
        Every baseline ``element_id`` MUST have a corresponding record.
    shud_forcing_index:
        Mapping ``grid_cell_id -> shud_forcing_index`` (int) produced by
        :func:`workers.mapping_builder.assign_shud_forcing_index`. Values
        MUST be the contiguous set ``{1, 2, ..., used_cell_count}``.
    used_cell_count:
        The used-cell count derived by
        :func:`workers.mapping_builder.assign_shud_forcing_index`. Used to
        validate the ``FORC`` range ``1..used_cell_count`` and echoed in
        the report.

    Returns
    -------
    SpAttRewriteReport
        Immutable report with baseline/variant checksums + sizes, the
        semantic diff, rewritten row count, and ``used_cell_count``.

    Raises
    ------
    SpAttRewriteError (and subclasses)
        Any G4 blocker: INV-2 violation, unparseable baseline, ownership
        duplicate, integer/range/multiset failure on ``shud_forcing_index``,
        unmapped element, non-``FORC`` column change (defense in depth),
        or INV-1 violation (:class:`BaselineImmutabilityViolationError`).
    """
    # Step 1: INV-2 — refuse if variant path resolves to baseline path.
    # ``Path.resolve(strict=False)`` (default) returns the absolute path
    # even when the target does not exist, AND follows symlinks along
    # any existing prefix. This catches literal-equal paths, symlink
    # aliases pointing at the baseline, and ``..`` traversal aliases in a
    # single check.
    baseline_resolved = baseline_att_path.resolve()
    try:
        variant_resolved = variant_att_path.resolve()
    except (OSError, RuntimeError):  # pragma: no cover - defensive fallback
        variant_resolved = variant_att_path.absolute()
    if baseline_resolved == variant_resolved:
        raise SpAttRewriteError(
            f"variant_att_path {variant_att_path} resolves to the same path "
            f"as baseline_att_path {baseline_att_path}; INV-2 requires "
            "distinct paths (baseline MUST NOT be overwritten)"
        )

    if not baseline_att_path.exists() or not baseline_att_path.is_file():
        raise SpAttRewriteError(
            f"baseline .sp.att does not exist or is not a file: {baseline_att_path}"
        )

    # Step 1b: INV-2 case-insensitive filesystem guard. ``Path.resolve()``
    # normalizes ``..`` and symlinks but does NOT case-fold, so ``foo.att``
    # and ``FOO.att`` compare non-equal as strings while ``os.replace``
    # would overwrite the baseline via the same inode on macOS APFS/HFS+
    # (default case-insensitive) or Windows NTFS. ``os.path.samefile``
    # compares by (device, inode) and catches the alias regardless of
    # spelling. Only meaningful when both paths already exist on disk;
    # the resolve() string check above is the pre-write fallback.
    if variant_att_path.exists():
        try:
            if os.path.samefile(baseline_att_path, variant_att_path):
                raise SpAttRewriteError(
                    f"variant_att_path {variant_att_path} aliases "
                    f"baseline_att_path {baseline_att_path} on a "
                    "case-insensitive filesystem; INV-2 requires "
                    "distinct paths (baseline MUST NOT be overwritten)"
                )
        except FileNotFoundError:  # pragma: no cover - race between exists() and samefile()
            pass

    # Step 2: pre-SHA-256 (INV-1 anchor).
    baseline_pre_sha = _sha256_file(baseline_att_path)
    baseline_size = baseline_att_path.stat().st_size

    # Step 3: parse baseline .sp.att.
    parsed = _parse_sp_att_file(baseline_att_path)

    # Step 4: validate ownership + shud_forcing_index.
    ownership_by_id: dict[int, ElementOwnership] = {}
    for o in ownership:
        if o.element_id in ownership_by_id:
            raise SpAttRewriteError(
                f"ownership sequence contains duplicate element_id={o.element_id}"
            )
        ownership_by_id[o.element_id] = o

    # 4a: every value in shud_forcing_index is int (reject bool explicitly
    # because bool is a subclass of int in Python).
    for grid_cell_id, forc_value in shud_forcing_index.items():
        if isinstance(forc_value, bool) or not isinstance(forc_value, int):
            raise ForcNonIntegerError(
                grid_cell_id=grid_cell_id, invalid_value=forc_value
            )
    # 4b: every value is in [1, used_cell_count].
    for grid_cell_id, forc_value in shud_forcing_index.items():
        if not (1 <= forc_value <= used_cell_count):
            raise ForcOutOfRangeError(
                new_forc=forc_value,
                valid_range=(1, used_cell_count),
                grid_cell_id=grid_cell_id,
            )
    # 4c: multiset equals {1, 2, ..., used_cell_count}.
    values_sorted = tuple(sorted(shud_forcing_index.values()))
    expected = tuple(range(1, used_cell_count + 1))
    if values_sorted != expected:
        raise ForcMultisetMismatchError(
            expected_values=expected,
            observed_values=values_sorted,
        )

    # Step 5: build new rows via element_id -> grid_cell_id -> forcing_index.
    baseline_forc_rows: list[SpAttForcRow] = []
    variant_forc_rows: list[SpAttForcRow] = []
    new_raw_data_lines: list[str] = []
    for raw_line, token_row in zip(
        parsed.raw_data_lines, parsed.parsed_rows, strict=True
    ):
        element_id = int(token_row[0])
        try:
            old_forc = int(token_row[parsed.forc_col_index])
        except ValueError as exc:
            raise SpAttRewriteError(
                f"baseline row element_id={element_id} FORC token "
                f"{token_row[parsed.forc_col_index]!r} is not an integer: {exc}"
            ) from exc
        if element_id not in ownership_by_id:
            raise ForcUnmappedError(
                element_id=element_id,
                detail="no ownership entry for baseline element_id",
            )
        grid_cell_id = ownership_by_id[element_id].grid_cell_id
        if grid_cell_id not in shud_forcing_index:
            raise ForcUnmappedError(
                element_id=element_id,
                grid_cell_id=grid_cell_id,
                detail="ownership.grid_cell_id not in shud_forcing_index",
            )
        new_forc = shud_forcing_index[grid_cell_id]
        baseline_forc_rows.append(
            SpAttForcRow(element_id=element_id, forc=old_forc)
        )
        variant_forc_rows.append(
            SpAttForcRow(element_id=element_id, forc=new_forc)
        )
        new_raw_data_lines.append(
            _replace_forc_token_in_row(
                raw_line, parsed.forc_col_index, new_forc
            )
        )

    # Step 6: verify non-FORC columns unchanged in-memory (defense in depth).
    _verify_non_forc_columns_unchanged_in_memory(parsed, new_raw_data_lines)

    # Step 7: emit semantic diff.
    semantic_diff = emit_semantic_diff(baseline_forc_rows, variant_forc_rows)

    # Step 8: serialize new content to temp path, compute SHA, atomic move.
    new_content = (
        parsed.header_line
        + parsed.header_terminator
        + parsed.column_header_line
        + parsed.column_header_terminator
        + "".join(new_raw_data_lines)
        + parsed.trailing_content
    )
    variant_att_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(variant_att_path.parent),
        prefix=f".{variant_att_path.name}.",
        suffix=".tmp",
    )
    tmp_path = pathlib.Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as tmp_handle:
            tmp_handle.write(new_content.encode("utf-8"))
        variant_sha = _sha256_file(tmp_path)
        variant_size = tmp_path.stat().st_size
        os.replace(tmp_path, variant_att_path)
    finally:
        # If tmp still present (rename failed), cleanup — best effort.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass

    # Step 9: post-SHA-256 (INV-1 hard block). On mismatch, unlink variant.
    baseline_post_sha = _sha256_file(baseline_att_path)
    if baseline_pre_sha != baseline_post_sha:
        try:
            variant_att_path.unlink()
        except OSError:  # pragma: no cover - best-effort cleanup
            pass
        raise BaselineImmutabilityViolationError(
            baseline_path=baseline_att_path,
            pre_sha256=baseline_pre_sha,
            post_sha256=baseline_post_sha,
        )

    # Step 10: return report.
    checksums = SpAttChecksums(
        baseline_sha256=baseline_pre_sha,
        variant_sha256=variant_sha,
        baseline_size=baseline_size,
        variant_size=variant_size,
    )
    return SpAttRewriteReport(
        checksums=checksums,
        semantic_diff=semantic_diff,
        rewritten_row_count=parsed.n_rows,
        used_cell_count=used_cell_count,
    )


def _verify_non_forc_columns_unchanged_in_memory(
    baseline: _ParsedSpAtt,
    variant_raw_data_lines: Sequence[str],
) -> None:
    """In-memory equivalent of :func:`verify_non_forc_columns_unchanged`.

    Runs BEFORE any variant byte hits disk (spec §"the builder fails
    closed when any non-FORC value differs"). Parses the newly-built
    variant data lines using the baseline schema and asserts per-row
    non-``FORC`` column token equality keyed by ``element_id``.
    """
    if len(variant_raw_data_lines) != baseline.n_rows:
        raise RowCountMismatchError(
            baseline_count=baseline.n_rows,
            variant_count=len(variant_raw_data_lines),
        )
    baseline_by_id = {int(row[0]): row for row in baseline.parsed_rows}
    variant_by_id: dict[int, tuple[str, ...]] = {}
    for raw_line in variant_raw_data_lines:
        content = raw_line.rstrip("\r\n")
        all_tokens = content.split()
        # Mirror the disk parser's "extra tokens per row" rejection: if the
        # variant row somehow carries more tokens than the baseline schema
        # declares, silently truncating via ``[: len(schema)]`` would hide a
        # schema drift injected between parse and write. Fail closed instead.
        if len(all_tokens) > len(baseline.schema):
            raise SpAttRewriteError(
                f"variant data row {raw_line!r} has {len(all_tokens)} "
                f"tokens, expected exactly {len(baseline.schema)} per "
                f"baseline schema {list(baseline.schema)!r} — extra "
                "tokens rejected to prevent silent schema drift"
            )
        tokens = tuple(all_tokens[: len(baseline.schema)])
        variant_by_id[int(tokens[0])] = tokens
    baseline_ids = set(baseline_by_id)
    variant_ids = set(variant_by_id)
    if baseline_ids != variant_ids:
        raise ElementIdSetMismatchError(
            baseline_only=tuple(sorted(baseline_ids - variant_ids)),
            variant_only=tuple(sorted(variant_ids - baseline_ids)),
        )
    forc_idx = baseline.forc_col_index
    for element_id in sorted(baseline_by_id):
        b_row = baseline_by_id[element_id]
        v_row = variant_by_id[element_id]
        for col_idx in range(len(baseline.schema)):
            if col_idx == forc_idx:
                continue
            if b_row[col_idx] != v_row[col_idx]:
                raise NonForcColumnChangedError(
                    element_id=element_id,
                    column_name=baseline.schema[col_idx],
                    baseline_value=b_row[col_idx],
                    variant_value=v_row[col_idx],
                )
