"""§3.1 + §3.2 + §3.3 + §3.4 + §3.5 ``.sp.att`` FORC rewrite + G4 gates.

This module implements OpenSpec change ``forcing-mapping-asset-build`` §3.1
through §3.5 (Epic #909 SUB-8 + SUB-9 + SUB-10). It exposes fail-closed
primitives that copy the baseline ``.sp.att`` into a variant package, update
every element's ``FORC`` value **by element ID** via the ownership +
``shud_forcing_index`` produced by ``element-grid-ownership-mapping``, prove
that no non-``FORC`` byte changes in the process, prove that all non-``.sp.att``
hydrologic core files are SHA-256 identical between baseline and variant
(§3.3), compute + prove equality of the ten-surface
``hydrologic_core_fingerprint`` (§3.4, docs §Gate G10), and assert that no
legacy CMFD weather path leaks into the active variant forcing tree (§3.5,
docs §Gate G4 + §8.1/§8.2).

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
* :func:`verify_no_legacy_weather_path_in_active_tree` — §3.5 G4 blocker:
  walks the variant package's active forcing subtree case-insensitively and
  refuses when any legacy CMFD weather CSV (``X<lon>Y<lat>.csv`` per docs
  §8.2 clause A; ``X<n>.csv`` per docs §8.2 clause B) or builder-written
  cycle-dated ``.tsd.forc`` (docs §8.1 boundary — the runtime producer owns
  cycle ``.tsd.forc``, the mapping builder never writes one) appears.
  Green build proof: the active forcing subtree contains only the
  direct-grid binding artifact reference and non-weather ancillary
  ``*.tsd.*`` files inventoried by SUB-2.
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
:func:`verify_no_legacy_weather_path_in_active_tree` (this module, §3.5),
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

Gate orchestration (SUB-8 + SUB-9 + SUB-10 -> SUB-13 deferral)
--------------------------------------------------------------
Several ``verify_*`` gates ship in this module as standalone primitives:
:func:`verify_non_forc_columns_unchanged` (SUB-8, §3.2),
:func:`verify_non_sp_att_checksums_equal` (SUB-9, §3.3),
:func:`verify_hydrologic_core_fingerprint_equal` (SUB-9, §3.4), and
:func:`verify_no_legacy_weather_path_in_active_tree` (SUB-10, §3.5). They
are exported for callers (the mapping evidence bundler in SUB-13,
standalone auditors) but are **not yet** wired into
:func:`copy_and_rewrite_sp_att_forc`'s write pipeline. The §3.1 orchestrator
still only runs its own in-memory non-``FORC`` proof
(``_verify_non_forc_columns_unchanged_in_memory``) before writing the
variant. Orchestrated "fail closed BEFORE write" invocation of the §3.3
file-checksum gate, the §3.4 fingerprint gate, and the §3.5 no-legacy-
weather-path gate lands in SUB-13 when the full variant assembly step is
scaffolded and the mapping evidence package assembles the ordered gate
results (G0–G5) into one immutable bundle.
"""

from __future__ import annotations

import os
import pathlib
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from packages.common.contracts.hydrologic_fingerprint import (
    HYDROLOGIC_CORE_FINGERPRINT_LABELS,
    NON_SP_ATT_CATEGORIES,
    STATE_COMPATIBILITY_SURFACES,
    HydrologicCoreFingerprint,
    HydrologicCoreFingerprintMismatchError,
    MissingCategoryError,
    MissingPackageFileError,
    SpAttRewriteError,
    UnknownCategoryError,
    _parse_sp_att_file,
    _ParsedSpAtt,
    _resolve_and_sha256,
    _sha256_file,
    _validate_category_files,
    compute_hydrologic_core_fingerprint,
    verify_hydrologic_core_fingerprint_equal,
)
from workers.mapping_builder.algorithm import ElementOwnership

__all__ = [
    "HYDROLOGIC_CORE_FINGERPRINT_LABELS",
    "LEGACY_CYCLE_TSD_FORC_PATTERN",
    "LEGACY_STATION_LONLAT_CSV_PATTERN",
    "LEGACY_STATION_NUMBERED_CSV_PATTERN",
    "NON_SP_ATT_CATEGORIES",
    "STATE_COMPATIBILITY_SURFACES",
    "ActiveForcingSubdirNotFoundError",
    "BaselineImmutabilityViolationError",
    "ElementIdSetMismatchError",
    "ForcMultisetMismatchError",
    "ForcNonIntegerError",
    "ForcOutOfRangeError",
    "ForcUnmappedError",
    "HydrologicCoreFingerprint",
    "HydrologicCoreFingerprintMismatchError",
    "LegacyWeatherPathInActiveTreeError",
    "MissingCategoryError",
    "MissingPackageFileError",
    "NonForcColumnChangedError",
    "NonSpAttChecksumMismatchError",
    "RowCountMismatchError",
    "SchemaMismatchError",
    "SemanticDiff",
    "SemanticDiffEntry",
    "SpAttChecksums",
    "SpAttForcRow",
    "SpAttRewriteError",
    "SpAttRewriteReport",
    "UnknownCategoryError",
    "compute_hydrologic_core_fingerprint",
    "copy_and_rewrite_sp_att_forc",
    "emit_semantic_diff",
    "parse_sp_att_forc_rows",
    "record_sp_att_checksums",
    "verify_hydrologic_core_fingerprint_equal",
    "verify_no_legacy_weather_path_in_active_tree",
    "verify_non_forc_columns_unchanged",
    "verify_non_sp_att_checksums_equal",
]

# --- exception family ------------------------------------------------------


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


class LegacyWeatherPathInActiveTreeError(SpAttRewriteError):
    """A legacy CMFD weather path leaked into the variant's active forcing tree.

    Per spec §"No legacy CMFD weather path appears in the active forcing
    tree" (§3.5) and docs §Gate G4 + §8.1/§8.2: the mapping builder's
    variant package MUST NOT carry any legacy CMFD weather CSV in the active
    forcing subtree (``X<lon>Y<lat>.csv`` per docs §8.2 clause A;
    ``X<n>.csv`` per docs §8.2 clause B) and MUST NOT carry any
    builder-written cycle-dated ``.tsd.forc`` — the runtime forcing producer
    owns cycle ``.tsd.forc``, the mapping builder never writes one (§8.1).

    ``pattern_name`` discriminates the three forbidden-filename categories
    so downstream evidence (SUB-13) records which class of legacy artifact
    slipped in:

    * ``"legacy_lonlat_csv"`` — a ``X<lon>Y<lat>.csv`` CMFD station-lat-lon
      weather CSV (e.g. ``X100.75Y37.65.csv``);
    * ``"legacy_numbered_csv"`` — a ``X<n>.csv`` CMFD numbered station
      weather CSV (e.g. ``X6.csv``);
    * ``"builder_written_cycle_tsd_forc"`` — a cycle-dated ``.tsd.forc``
      file the mapping builder MUST NOT write (e.g.
      ``cycle_2020010100.tsd.forc``).

    The scan is case-insensitive on filenames — an uppercase ``X100Y50.CSV``
    aliases a lowercase ``x100y50.csv`` on any case-insensitive filesystem
    (macOS APFS/HFS+, Windows NTFS default), so both spellings block.
    ``matched_path`` records the offending path (absolute, from the walk)
    so evidence and error messages name the specific file, not just its
    directory.
    """

    def __init__(
        self,
        *,
        matched_path: pathlib.Path,
        pattern_name: str,
    ) -> None:
        super().__init__(
            f"legacy weather path {matched_path} matches forbidden pattern "
            f"{pattern_name!r} in active variant forcing tree "
            "(G4 asset delta / docs §8.1 + §8.2 violation)"
        )
        self.matched_path = matched_path
        self.pattern_name = pattern_name


class ActiveForcingSubdirNotFoundError(SpAttRewriteError):
    """The active forcing subdirectory does not exist under the variant root.

    Per §3.5 the gate must scan the variant's active forcing tree. If the
    caller-supplied ``active_forcing_subdir`` does not resolve to an
    existing directory under ``package_root``, the gate cannot prove
    anything and MUST refuse loudly rather than silently passing on a
    missing tree — a missing active-forcing subtree means the variant
    package is malformed, and silently declaring "no legacy weather here"
    would let a downstream evidence bundler bind to a nonexistent tree.
    """

    def __init__(
        self,
        *,
        package_root: pathlib.Path,
        active_forcing_subdir: str,
        resolved_path: pathlib.Path,
    ) -> None:
        super().__init__(
            f"active_forcing_subdir {active_forcing_subdir!r} under "
            f"package_root {package_root} resolves to {resolved_path} which "
            "does not exist or is not a directory; the §3.5 gate cannot "
            "prove absence of legacy weather in a nonexistent tree"
        )
        self.package_root = package_root
        self.active_forcing_subdir = active_forcing_subdir
        self.resolved_path = resolved_path


# --- §3.5 legacy weather path patterns (G4 no-legacy-weather-path) --------

# Compiled once at import for cheap per-file matching. Case-insensitive
# because macOS APFS/HFS+ and Windows NTFS default to case-insensitive
# filesystems — an uppercase ``X100Y50.CSV`` aliases a lowercase
# ``x100y50.csv`` at the OS layer, so a case-sensitive regex would silently
# miss the alias and let the legacy path leak through the gate.
#
# Pattern rationale:
#
# * :data:`_LEGACY_LONLAT_CSV_PATTERN` — docs §8.2 clause A: the CMFD legacy
#   station-lat-lon-keyed CSV (e.g. ``X100.75Y37.65.csv``). Requires ``X``
#   then digits (with optional decimal), ``Y``, digits (with optional
#   decimal), then ``.csv``. The ``Y`` is what distinguishes this from the
#   numbered pattern below — the two are non-overlapping.
# * :data:`_LEGACY_NUMBERED_CSV_PATTERN` — docs §8.2 clause B: the CMFD
#   legacy numbered CSV (e.g. ``X6.csv`` for zhaochen_mc's ``X6..X9`` or
#   ``X1.csv`` for zhaochen_wem's ``X1..X5``). Requires ``X`` then digits
#   then ``.csv`` with no ``Y`` in between.
# * :data:`_CYCLE_TSD_FORC_PATTERN` — docs §8.1 boundary: the runtime
#   forcing producer owns cycle ``.tsd.forc`` (with an embedded forecast
#   cycle startdate). The mapping builder MUST NOT write one. The pattern
#   requires an 8-digit run whose leading two digits are ``19``, ``20``,
#   or ``21`` (a plausible cycle-year prefix) so the marker keys off a
#   real date-shape ``YYYYMMDD`` / ``YYYYMMDDHH`` cycle stamp instead of
#   any arbitrary 8-digit substring. A bare ``\d{8}`` marker would
#   false-block realistic non-cycle names — e.g. ``basin12345678.tsd.forc``
#   (embedded basin ID) — which are legal in the active tree. It still
#   never collides with baseline standard filenames like the canonical
#   station-index ``stations.tsd.forc`` or the legacy ``qhh.tsd.forc``
#   (no digits) or per-basin ``.tsd.forc`` with short numeric suffixes
#   (< 8 digits), and it retains coverage for both the pure date name
#   ``20200101.tsd.forc`` and the prefixed cycle name
#   ``cycle_2020010100.tsd.forc``. See
#   ``test_no_legacy_weather_path_basin_id_with_8_digits_passes`` for the
#   collision-avoidance regression test.
_LEGACY_LONLAT_CSV_PATTERN = re.compile(
    r"^X\d+(?:\.\d+)?Y\d+(?:\.\d+)?\.csv$", re.IGNORECASE
)
_LEGACY_NUMBERED_CSV_PATTERN = re.compile(r"^X\d+\.csv$", re.IGNORECASE)
_CYCLE_TSD_FORC_PATTERN = re.compile(
    r"^.*(?:19|20|21)\d{6}.*\.tsd\.forc$", re.IGNORECASE
)


# Public aliases for the compiled regex objects above (Epic #909 SUB-12
# CP-1 shared-authority discipline). Both this module's
# :func:`verify_no_legacy_weather_path_in_active_tree` (§3.5) and
# :mod:`workers.mapping_builder.binding`'s
# :func:`verify_no_forbidden_runtime_producer_artifacts` (§4.3) reference
# the *same* compiled regex object — no drift is possible because both
# call sites bind to the same identity. Renaming here follows the
# ``LEGACY_*_PATTERN`` naming shape SUB-11 established for grid-signature
# constants, and preserves the underscore-prefixed private names so
# existing SUB-10 call sites in this file keep working verbatim.
LEGACY_STATION_LONLAT_CSV_PATTERN: re.Pattern[str] = _LEGACY_LONLAT_CSV_PATTERN
LEGACY_STATION_NUMBERED_CSV_PATTERN: re.Pattern[str] = _LEGACY_NUMBERED_CSV_PATTERN
LEGACY_CYCLE_TSD_FORC_PATTERN: re.Pattern[str] = _CYCLE_TSD_FORC_PATTERN


# Ordered list of (pattern_name, compiled regex). Order determines which
# pattern name is reported when multiple patterns could match the same
# filename — the lonlat pattern is checked before the numbered pattern
# because ``X100Y50.csv`` is more specific than ``X100.csv`` (though they
# never share a filename in practice due to the required ``Y``).
_LEGACY_WEATHER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("legacy_lonlat_csv", _LEGACY_LONLAT_CSV_PATTERN),
    ("legacy_numbered_csv", _LEGACY_NUMBERED_CSV_PATTERN),
    ("builder_written_cycle_tsd_forc", _CYCLE_TSD_FORC_PATTERN),
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


# --- internal parser ------------------------------------------------------


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


# --- §3.5 no-legacy-weather-path-in-active-tree gate (G4 blocker) --------


def verify_no_legacy_weather_path_in_active_tree(
    package_root: pathlib.Path,
    *,
    active_forcing_subdir: str | pathlib.Path,
) -> None:
    """§3.5 G4 gate: refuse when any legacy weather path lives in the active tree.

    Walks the variant package's active forcing subtree recursively and
    fail-closed on the first filename matching any of the three forbidden
    patterns declared in :data:`_LEGACY_WEATHER_PATTERNS`:

    1. ``X<lon>Y<lat>.csv`` (docs §8.2 clause A) — the legacy CMFD
       station-lat-lon weather CSV. Runtime SHUD station CSVs live under
       object-store, not inside the model input package.
    2. ``X<n>.csv`` (docs §8.2 clause B) — the legacy CMFD numbered station
       weather CSV (zhaochen_mc / zhaochen_wem baseline shape). Any
       compatibility copy of these MUST live in a non-runtime directory
       marked inactive — never in the active forcing tree.
    3. Builder-written cycle-dated ``.tsd.forc`` (docs §8.1 boundary) — the
       runtime forcing producer owns cycle ``.tsd.forc``. The mapping
       builder MUST NOT write one; if it did, the variant would ship a
       fixed-cycle forcing that shadows the producer's canonical output.

    Match is case-insensitive on filenames (uppercase / mixed-case aliases
    like ``X100Y50.CSV`` or ``x50Y100.csv`` block equally). The scan
    descends every subdirectory under ``active_forcing_subdir`` — a nested
    ``forcing/subdir/X6.csv`` still blocks.

    Green-build proof: the active forcing subtree contains only the
    direct-grid binding artifact reference plus non-weather ancillary
    ``*.tsd.*`` files (per SUB-2's ancillary inventory). None of the three
    patterns match a non-weather ``*.tsd.*`` (e.g. ``precip.tsd.mrms.forc``,
    ``pet.tsd.hourly.csv``) or the binding artifact — so a clean variant
    passes with ``None``.

    Fail-closed guarantee: raises :class:`LegacyWeatherPathInActiveTreeError`
    on first match BEFORE any variant package assembly step commits. SUB-13
    calls this gate as one of the G4 pre-write checks in the mapping
    evidence pipeline.

    Parameters
    ----------
    package_root:
        Absolute path to the variant model input package root. MUST be an
        existing directory.
    active_forcing_subdir:
        Relative-path (``str`` or :class:`pathlib.Path`) to the active
        forcing subtree under ``package_root``. Accepted as a kwarg because
        docs §8.2 does not fix a canonical layout — the caller (SUB-13
        orchestrator) knows the variant's forcing subdir per basin / per
        change. Follows the same "pluggable kwarg" convention SUB-9
        established for ``state_schema_bytes`` / ``solver_config_bytes``.
        The resolved path MUST exist under ``package_root`` and be a
        directory; a missing subtree raises
        :class:`ActiveForcingSubdirNotFoundError`.

    Raises
    ------
    LegacyWeatherPathInActiveTreeError
        A filename in the active forcing subtree matches one of the three
        forbidden patterns. ``matched_path`` carries the absolute path;
        ``pattern_name`` identifies which category.
    ActiveForcingSubdirNotFoundError
        ``active_forcing_subdir`` does not resolve to an existing directory
        under ``package_root``.
    SpAttRewriteError
        ``package_root`` is not a :class:`pathlib.Path` (type refusal at
        the boundary), or ``package_root`` is not an existing directory.
    """
    if not isinstance(package_root, pathlib.Path):
        raise SpAttRewriteError(
            f"package_root expects pathlib.Path, got "
            f"{type(package_root).__name__}"
        )
    if not package_root.is_dir():
        raise SpAttRewriteError(
            f"package_root {package_root} is not a directory"
        )
    active_root = package_root / active_forcing_subdir
    if not active_root.exists() or not active_root.is_dir():
        raise ActiveForcingSubdirNotFoundError(
            package_root=package_root,
            active_forcing_subdir=str(active_forcing_subdir),
            resolved_path=active_root,
        )
    # ``rglob("*")`` recursively enumerates every file + dir under the active
    # tree. Sort to make the first-mismatch report reproducible across runs /
    # OS iteration orders (spec §7 determinism). Only regular files are
    # candidates — directories and symlinks-to-directories are skipped.
    # ``rglob("*")`` follows symlinks by default; symlinks-to-files
    # participate in the filename scan (their basenames are matched against
    # the legacy patterns), so a symlink named ``X100Y50.csv`` is caught
    # just like a real file.
    for candidate in sorted(active_root.rglob("*")):
        if not candidate.is_file():
            continue
        name = candidate.name
        for pattern_name, pattern in _LEGACY_WEATHER_PATTERNS:
            if pattern.fullmatch(name):
                raise LegacyWeatherPathInActiveTreeError(
                    matched_path=candidate,
                    pattern_name=pattern_name,
                )
