"""§4.1 + §4.2 direct-grid manifest + station-binding emission (G5 contract).

This module implements OpenSpec change ``forcing-mapping-asset-build`` §4.1
and §4.2 (Epic #909 SUB-11). It emits the direct-grid **manifest** (placed
under the ``resource_profile.direct_grid_forcing`` nested section per docs
§7.1) plus the standalone **binding artifact** (JSON blob referenced by the
manifest's ``binding_uri``), and enforces the G5 cross-artifact contract:

* manifest carries the ten manifest-level fields required by the parser
  contract in :mod:`workers.forcing_producer.direct_grid_contract`
  (``forcing_mapping_mode`` + nine identity fields including
  ``station_bindings``);
* each per-station binding carries the ten fields required by the parser
  (``station_id``, ``shud_forcing_index``, ``forcing_filename``,
  ``longitude``, ``latitude``, ``x``, ``y``, ``z``, ``grid_id``,
  ``grid_cell_id``);
* every ``grid_cell_id`` in the binding is pairwise unique and is a subset
  member of the loaded snapshot's ordered ``grid_cell_id`` set (§4.1
  Required-evidence);
* the manifest's ``station_bindings`` row set equals the binding artifact's
  row set element-for-element (same ``station_id``, ``shud_forcing_index``,
  ``grid_cell_id``, and 12-decimal-rounded lon/lat) — G5 cross-artifact
  consistency (§4.1 Required-evidence, docs §Gate G5);
* ``binding_checksum`` is the SHA-256 of the emitted binding artifact bytes
  and ``sp_att_checksum`` is the SHA-256 of the emitted variant ``.sp.att``
  bytes; either mismatch is a G5 blocker (§4.1 Required-evidence);
* ``station_id`` embeds an immutable mapping-asset identity and is never
  reused across mapping versions (§4.2, docs §7.4);
* ``forcing_filename`` is safe, pathless, case-fold unique, and NOT derived
  from rounded coordinates; it never collides with reserved names
  (``qhh.tsd.forc``, ``manifest.json``, debug/model-input filenames) on
  case-insensitive filesystems (§4.2, docs §7.3);
* station lon/lat equal the registered cell center under 12-decimal
  rounding — the same rounding used by
  :func:`packages.common.grid_signature.grid_signature_tuples` — never a
  raw float-literal equality (§4.2, docs §7.3);
* the binding declares an explicit WGS84 coordinate basis
  (``coordinate_reference_system="EPSG:4326"``) and forbids cross-basis
  equality assertions against SRID 4490 (CGCS2000) ``met.met_station.geom``
  mirror rows (§4.2, docs §7.3);
* station ``x``/``y`` are recomputable from ``longitude``/``latitude`` +
  the model CRS supplied by SUB-2 (§4.2);
* station ``z`` follows the ``z_policy`` verdict from Epic #886
  ``cmfd-direct-grid-platform-readiness`` verbatim — never inlined as a
  numeric default (§4.2, docs §7.5).

Public entry points
-------------------
* :func:`emit_direct_grid_manifest_and_binding` — §4.1 orchestrator.
  Assembles :class:`StationBinding` rows for the used cells, verifies each
  station center matches the snapshot cell under 12-decimal rounding,
  serializes the binding artifact to canonical JSON, recomputes both
  checksums, builds the :class:`DirectGridManifest`, cross-consistency
  verifies against the binding artifact, and round-trips the manifest
  through the existing parser. Returns the manifest and binding artifact
  as a pair; raises the corresponding :class:`BindingArtifactError`
  subclass on any G5 blocker.
* :func:`verify_binding_round_trips_parser` — G5 gate: parses the emitted
  manifest through the existing parser
  :func:`workers.forcing_producer.direct_grid_contract.load_forcing_mapping_contract_from_manifest`
  and returns the :class:`DirectGridForcingContract` on pass; raises
  :class:`ParserRoundTripError` on failure.
* :func:`verify_grid_cell_id_unique_and_snapshot_member` — G5 gate:
  asserts binding ``grid_cell_id`` values are pairwise unique and every
  value is a subset member of the loaded snapshot's ordered
  ``grid_cell_id`` set.
* :func:`verify_manifest_binding_cross_consistent` — G5 gate: compares the
  manifest's ``station_bindings`` row set against the binding artifact's
  row set element-for-element; any divergence raises
  :class:`ManifestBindingDivergenceError`.
* :func:`recompute_binding_and_sp_att_checksums` — computation: returns
  ``(binding_checksum, sp_att_checksum)`` as SHA-256 hex of the supplied
  bytes.
* :func:`assign_station_id_from_mapping_asset_identity` — computation:
  produces a ``station_id`` embedding the mapping-asset identity and the
  ``grid_cell_id`` so two different mapping versions produce disjoint
  station_id sets.
* :func:`sanitize_station_forcing_filename` — computation: derives a safe,
  pathless, case-fold-unique forcing filename from ``shud_forcing_index``
  alone (never from rounded coordinates); matches the parser's
  ``_SAFE_STATION_FORCING_FILENAME`` regex and never collides with the
  reserved-name blocklist.
* :func:`verify_station_center_matches_snapshot_under_rounding` — G5
  gate: asserts ``round(station.lon, 12) == round(cell.lon, 12)`` and
  same for latitude, and asserts the snapshot cell basis is WGS84 (any
  SRID 4490 / CGCS2000 basis raises :class:`CrossBasisEqualityError`).
* :func:`apply_z_policy_from_readiness` — computation: reads the ``z``
  value verbatim from an approved :class:`ZPolicy` (Epic #886 verdict)
  for the given ``grid_cell_id``.

Exception family
----------------
:class:`BindingArtifactError` is a distinct root — *not* a subclass of
:class:`workers.mapping_builder.integrity.BaselineIntegrityError` (G0/G1),
:class:`workers.mapping_builder.algorithm.MappingAlgorithmError` (G2/G3),
or :class:`workers.mapping_builder.rewrite.SpAttRewriteError` (G4). G5
failures come from a different oracle (contract-parser round-trip +
cross-artifact identity) than G0/G1 (baseline package integrity), G2/G3
(grid registry + WGS84 coverage), or G4 (baseline file bytes + ownership
consistency). Keeping the roots distinct lets callers differentiate the
four families with dedicated ``except`` clauses.

Gate-naming convention (mapping_builder namespace)
--------------------------------------------------
This module follows the same codified ``verify_*`` prefix convention
established by :mod:`workers.mapping_builder.rewrite` for fail-closed
invariant gates. The return value discriminates outcome:

* ``None`` iff the gate passed with no artifact needed
  (e.g. :func:`verify_grid_cell_id_unique_and_snapshot_member`,
  :func:`verify_manifest_binding_cross_consistent`,
  :func:`verify_station_center_matches_snapshot_under_rounding`);
* a dataclass / artifact iff the gate passed with a caller-visible
  payload (e.g. :func:`verify_binding_round_trips_parser` returns the
  parsed :class:`DirectGridForcingContract`);
* ``raise`` iff the gate failed.

Computation-only helpers (``assign_*``, ``sanitize_*``, ``recompute_*``,
``apply_*``, ``emit_*``) use non-``verify_*`` verbs — they never raise on
"drift", they produce the artifact for a downstream verify_ gate to check.

Gate orchestration (SUB-11 -> SUB-13 deferral)
----------------------------------------------
:func:`emit_direct_grid_manifest_and_binding` runs the G5 gates inline
before returning (grid_cell_id uniqueness/membership → checksums →
cross-artifact consistency → parser round-trip), matching the "fail
closed BEFORE write" pattern from §3.1's ``copy_and_rewrite_sp_att_forc``.
The individual ``verify_*`` gates are exported so SUB-13's evidence
bundler can rerun them post-hoc and record their pass/fail into the
mapping evidence package's G5 slot verbatim.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pyproj

from packages.common.grid_registry_store import CanonicalGridCell
from workers.forcing_producer.direct_grid_contract import (
    DIRECT_GRID_MODE,
    DirectGridContractError,
    DirectGridForcingContract,
    load_forcing_mapping_contract_from_manifest,
)

# --- constants ------------------------------------------------------------

#: Nested-section key under the outer ``resource_profile`` mapping where the
#: direct-grid contract lives per docs §7.1. Chosen from the parser's
#: :data:`workers.forcing_producer.direct_grid_contract.DIRECT_GRID_SECTION_KEYS`
#: canonical set so a round-trip through the parser resolves this key first.
DIRECT_GRID_FORCING_SECTION_KEY: str = "direct_grid_forcing"

#: The canonical WGS84 coordinate basis declaration required by docs §7.3.
#: The mapping builder MUST declare this basis on every binding so downstream
#: equality checks (station lon/lat vs registered cell center) always compare
#: same-basis operands. Cross-basis assertions against SRID 4490 (CGCS2000)
#: ``met.met_station.geom`` mirror rows are explicitly forbidden.
WGS84_COORDINATE_BASIS: str = "EPSG:4326"

#: Forbidden coordinate basis label (CGCS2000). Docs §7.3 pin the mapping
#: builder's coordinate basis to WGS84 and forbid cross-basis equality
#: assertions against the SRID 4490 database mirror. Comparing the two
#: without an explicit transform is a G5 blocker even though the numeric
#: difference is < 1 m for typical basins (0.25°-grid case).
CGCS2000_SRID_LABEL: str = "SRID:4490"

#: 12-decimal rounding precision required by docs §7.3 and matched to
#: :func:`packages.common.grid_signature.grid_signature_tuples`. Station
#: lon/lat must equal the registered cell center after this rounding —
#: never raw float-literal equality, because live coordinates carry
#: ~1e-7° floating-point noise.
COORDINATE_ROUNDING_DECIMALS: int = 12

#: Approved ``z_policy`` verdicts per docs §7.5. Any other value indicates
#: a caller bug or an Epic #886 verdict escape — refused loudly.
ALLOWED_Z_POLICIES: frozenset[str] = frozenset(
    {
        "canonical_orography",
        "model_dem_at_cell_center",
        "sentinel",
    }
)

#: Reserved forcing filenames that MUST NOT be produced. Anchored on docs
#: §7.3 clause "filename must not collide with `qhh.tsd.forc`, the manifest,
#: debug artifacts, or model-input filenames on case-insensitive
#: filesystems". Comparison is case-insensitive at check time.
RESERVED_FORCING_FILENAMES: frozenset[str] = frozenset(
    {
        "qhh.tsd.forc",
        "manifest.json",
        "package.json",
        "resource_profile.json",
        "binding.json",
        "evidence.json",
        "domain.shp",
    }
)

#: Reserved filename prefixes checked case-insensitively. ``debug`` catches
#: ``debug_forcing.csv``, ``debug.log``, etc. — none of which the mapping
#: builder should ever surface as a station forcing filename.
RESERVED_FILENAME_PREFIXES: tuple[str, ...] = ("debug",)

#: Reserved filename suffixes checked case-insensitively. These match
#: model-input file suffixes that MUST NOT be reused as station forcing
#: filenames even when the base name differs.
RESERVED_FILENAME_SUFFIXES: tuple[str, ...] = (
    ".sp.mesh",
    ".sp.att",
    ".calib",
    ".geol",
    ".land",
    ".riv",
    ".lake",
    ".tsd.forc",
    ".prj",
    ".shp",
)

#: Filename regex the parser applies to every station forcing filename
#: (mirrors :data:`workers.forcing_producer.direct_grid_contract._SAFE_STATION_FORCING_FILENAME`
#: verbatim). Hard-copied here (not imported from the parser's private
#: attribute) with an explicit citation so a review can prove no drift; any
#: change to the parser's regex MUST be mirrored here.
_SAFE_STATION_FORCING_FILENAME: re.Pattern[str] = re.compile(
    r"^[A-Za-z0-9._-]+\.csv$"
)


# --- exception family ------------------------------------------------------


class BindingArtifactError(Exception):
    """Base class for §4.1 + §4.2 binding-artifact / manifest failures.

    Distinct root class (not a subclass of
    :class:`workers.mapping_builder.integrity.BaselineIntegrityError`,
    :class:`workers.mapping_builder.algorithm.MappingAlgorithmError`, or
    :class:`workers.mapping_builder.rewrite.SpAttRewriteError`). Callers
    that catch G5 binding failures MUST NOT accidentally absorb G0/G1
    baseline errors, G2/G3 algorithm errors, or G4 rewrite errors with the
    same ``except`` clause; the mapping builder's fail-closed guarantee is
    meaningful only when callers can tell the four families apart.
    """


class ManifestFieldMissingError(BindingArtifactError):
    """A required manifest field is absent or empty.

    Per spec §"Manifest carries all required identity fields" (§4.1) and
    docs §7.2: the ten manifest-level fields (``forcing_mapping_mode``,
    ``binding_uri``, ``binding_checksum``, ``model_input_package_id``,
    ``sp_att_path``, ``sp_att_checksum``, ``applicable_source_ids``,
    ``grid_id``, ``grid_signature``, ``station_bindings``) MUST all be
    present and non-empty; a missing field is a G5 blocker.
    """

    def __init__(self, *, field_name: str) -> None:
        super().__init__(
            f"direct-grid manifest is missing required field {field_name!r} "
            "(G5 manifest completeness violation)"
        )
        self.field_name = field_name


class StationFieldMissingError(BindingArtifactError):
    """A required per-station field is absent or empty.

    Per spec §"Station bindings carry all required fields" (§4.1) and docs
    §7.3: every ``station_bindings`` row MUST carry ``station_id``,
    ``shud_forcing_index``, ``forcing_filename``, ``longitude``,
    ``latitude``, ``x``, ``y``, ``z``, ``grid_id``, and ``grid_cell_id``.
    """

    def __init__(self, *, field_name: str, station_id: str | None = None) -> None:
        parts = [f"station binding is missing required field {field_name!r}"]
        if station_id is not None:
            parts.append(f"station_id={station_id!r}")
        parts.append("(G5 station completeness violation)")
        super().__init__(" ".join(parts))
        self.field_name = field_name
        self.station_id = station_id


class ParserRoundTripError(BindingArtifactError):
    """The emitted manifest failed to round-trip through the existing parser.

    Per spec §"The emitted manifest parses cleanly through the existing
    direct-grid contract parser" (§4.1): the mapping builder MUST emit a
    manifest whose fields match
    :mod:`workers.forcing_producer.direct_grid_contract` verbatim. Any
    :class:`DirectGridContractError` raised by the parser is wrapped here
    to indicate a G5 contract-shape violation without letting the raw
    :class:`DirectGridContractError` type escape (which would collide with
    the parser's own error family).
    """

    def __init__(
        self,
        *,
        parser_error_message: str,
        parser_field: str | None = None,
    ) -> None:
        super().__init__(
            f"emitted manifest failed parser round-trip: "
            f"{parser_error_message} (G5 contract-shape violation)"
        )
        self.parser_error_message = parser_error_message
        self.parser_field = parser_field


class GridCellIdDuplicateError(BindingArtifactError):
    """A ``grid_cell_id`` appears in more than one station binding.

    Per spec §"grid_cell_id is unique within the binding" (§4.1) and docs
    §7.3 additional invariant: every ``grid_cell_id`` in the binding MUST
    be unique — one cell = one SHUD station (the used-cell subset is
    de-duplicated by grid_cell_id upstream in §2.3). A duplicate here
    signals ownership/index inconsistency and blocks G5.
    """

    def __init__(self, *, grid_cell_id: str, station_ids: tuple[str, ...]) -> None:
        super().__init__(
            f"grid_cell_id={grid_cell_id!r} appears in multiple bindings "
            f"(station_ids={list(station_ids)}) — one cell = one station "
            "(G5 uniqueness violation)"
        )
        self.grid_cell_id = grid_cell_id
        self.station_ids = station_ids


class GridCellIdNotInSnapshotError(BindingArtifactError):
    """A binding's ``grid_cell_id`` is not a member of the snapshot's set.

    Per spec §"every grid_cell_id is a subset member of the loaded
    snapshot's ordered grid_cell_id set" (§4.1): every binding cell MUST
    reference the loaded snapshot. A binding referencing a cell absent
    from the snapshot means the ownership stage silently invented a cell,
    which is a G5 blocker.
    """

    def __init__(
        self,
        *,
        grid_cell_id: str,
        station_id: str,
        snapshot_cell_count: int,
    ) -> None:
        super().__init__(
            f"grid_cell_id={grid_cell_id!r} (station_id={station_id!r}) is not "
            f"a member of the loaded snapshot ({snapshot_cell_count} cells) "
            "(G5 snapshot-membership violation)"
        )
        self.grid_cell_id = grid_cell_id
        self.station_id = station_id
        self.snapshot_cell_count = snapshot_cell_count


class ManifestBindingDivergenceError(BindingArtifactError):
    """Manifest ``station_bindings`` diverge from the binding artifact.

    Per spec §"Manifest and binding artifact are cross-consistent (G5)"
    (§4.1) and docs §Gate G5: the manifest's ``station_bindings`` row set
    MUST equal the standalone binding artifact's row set element-for-
    element (same ``station_id``, ``shud_forcing_index``, ``grid_cell_id``,
    and 12-decimal-rounded lon/lat). Divergence is a G5 blocker; the
    mismatch attributes name the offending row + field.
    """

    def __init__(
        self,
        *,
        divergent_field: str,
        station_id: str,
        manifest_value: Any,
        binding_value: Any,
    ) -> None:
        super().__init__(
            f"manifest vs binding artifact divergence at station_id={station_id!r} "
            f"field={divergent_field!r}: manifest={manifest_value!r} "
            f"binding={binding_value!r} (G5 cross-artifact violation)"
        )
        self.divergent_field = divergent_field
        self.station_id = station_id
        self.manifest_value = manifest_value
        self.binding_value = binding_value


class BindingChecksumMismatchError(BindingArtifactError):
    """The manifest's ``binding_checksum`` differs from the SHA-256 of the emitted bytes.

    Per spec §"The manifest's binding_checksum equals the SHA-256 of the
    binding artifact bytes referenced by binding_uri, recomputed at build
    time from the emitted bytes" (§4.1): the manifest's ``binding_checksum``
    MUST equal SHA-256(emitted binding artifact bytes). Any drift is a G5
    blocker.
    """

    def __init__(self, *, manifest_checksum: str, recomputed_checksum: str) -> None:
        super().__init__(
            f"binding_checksum mismatch: manifest={manifest_checksum!r} "
            f"recomputed={recomputed_checksum!r} "
            "(G5 checksum-binding violation)"
        )
        self.manifest_checksum = manifest_checksum
        self.recomputed_checksum = recomputed_checksum


class SpAttChecksumMismatchError(BindingArtifactError):
    """The manifest's ``sp_att_checksum`` differs from the SHA-256 of the variant ``.sp.att`` bytes.

    Per spec §"The manifest's sp_att_checksum equals the SHA-256 of the
    emitted variant .sp.att bytes at sp_att_path" (§4.1): the manifest's
    ``sp_att_checksum`` MUST equal SHA-256(variant .sp.att bytes). Any
    drift is a G5 blocker.
    """

    def __init__(self, *, manifest_checksum: str, recomputed_checksum: str) -> None:
        super().__init__(
            f"sp_att_checksum mismatch: manifest={manifest_checksum!r} "
            f"recomputed={recomputed_checksum!r} "
            "(G5 checksum-binding violation)"
        )
        self.manifest_checksum = manifest_checksum
        self.recomputed_checksum = recomputed_checksum


class StationIdReuseError(BindingArtifactError):
    """Two mapping versions produced overlapping ``station_id`` sets.

    Per spec §"station_id embeds immutable mapping-asset identity and is
    never reused across mapping versions" (§4.2) and docs §7.4: the DB
    mirror fails closed on ``station_id`` collision when the referenced
    ``binding_checksum`` / ``model_input_package_id`` / ``grid_signature``
    differ. Overlap between two mapping versions' station_id sets means
    the mapping-asset identity leaked out of the identity token — a G5
    blocker before the mapping ever hits the mirror.
    """

    def __init__(
        self,
        *,
        overlapping_station_ids: tuple[str, ...],
        first_mapping_asset_identity: str,
        second_mapping_asset_identity: str,
    ) -> None:
        super().__init__(
            f"station_id reuse across mapping versions: "
            f"overlapping_station_ids={list(overlapping_station_ids)} "
            f"first_identity={first_mapping_asset_identity!r} "
            f"second_identity={second_mapping_asset_identity!r} "
            "(G5 identity-reuse violation)"
        )
        self.overlapping_station_ids = overlapping_station_ids
        self.first_mapping_asset_identity = first_mapping_asset_identity
        self.second_mapping_asset_identity = second_mapping_asset_identity


class ForcingFilenameCollisionError(BindingArtifactError):
    """A ``forcing_filename`` collides with another binding row or a reserved name.

    Per spec §"forcing_filename is safe, pathless, and collision-free"
    (§4.2) and docs §7.3: filenames MUST be case-fold unique across the
    binding and MUST NOT collide with :data:`RESERVED_FORCING_FILENAMES`,
    :data:`RESERVED_FILENAME_PREFIXES`, or :data:`RESERVED_FILENAME_SUFFIXES`
    on case-insensitive filesystems. ``collision_kind`` discriminates the
    three categories so downstream evidence (SUB-13) records which
    collision class raised.
    """

    def __init__(
        self,
        *,
        forcing_filename: str,
        collision_kind: str,
        collided_with: str | None = None,
    ) -> None:
        parts = [
            f"forcing_filename={forcing_filename!r} collides "
            f"({collision_kind})"
        ]
        if collided_with is not None:
            parts.append(f"with {collided_with!r}")
        parts.append("(G5 filename-collision violation)")
        super().__init__(" ".join(parts))
        self.forcing_filename = forcing_filename
        self.collision_kind = collision_kind
        self.collided_with = collided_with


class ForcingFilenameUnsafeError(BindingArtifactError):
    """A ``forcing_filename`` fails the parser's safety regex.

    Per spec §"forcing_filename is safe, pathless" (§4.2) and the parser's
    :data:`workers.forcing_producer.direct_grid_contract._SAFE_STATION_FORCING_FILENAME`:
    filenames MUST match ``^[A-Za-z0-9._-]+\\.csv$`` — no path separators,
    no shell metacharacters, no non-ASCII, no ``.`` or ``..``. A regex
    miss is a G5 blocker (also blocks the parser round-trip).
    """

    def __init__(self, *, forcing_filename: str) -> None:
        super().__init__(
            f"forcing_filename={forcing_filename!r} is unsafe: does not match "
            f"parser regex {_SAFE_STATION_FORCING_FILENAME.pattern!r} "
            "(G5 filename-safety violation)"
        )
        self.forcing_filename = forcing_filename


class StationCenterMismatchError(BindingArtifactError):
    """Station lon/lat differ from the registered cell center after 12-decimal rounding.

    Per spec §"Station lon/lat equal the registered cell center under
    rounding" (§4.2) and docs §7.3: comparison MUST use the same
    12-decimal rounding as :func:`packages.common.grid_signature.grid_signature_tuples`.
    A mismatch after rounding means the station coordinates were derived
    from a different oracle than the registered cell — a G5 blocker.
    """

    def __init__(
        self,
        *,
        grid_cell_id: str,
        station_id: str,
        station_longitude: float,
        station_latitude: float,
        snapshot_longitude: float,
        snapshot_latitude: float,
    ) -> None:
        super().__init__(
            f"station center mismatch at grid_cell_id={grid_cell_id!r} "
            f"station_id={station_id!r}: station=(lon={station_longitude!r}, "
            f"lat={station_latitude!r}) snapshot=(lon={snapshot_longitude!r}, "
            f"lat={snapshot_latitude!r}) after 12-decimal rounding "
            "(G5 coordinate-tolerance violation)"
        )
        self.grid_cell_id = grid_cell_id
        self.station_id = station_id
        self.station_longitude = station_longitude
        self.station_latitude = station_latitude
        self.snapshot_longitude = snapshot_longitude
        self.snapshot_latitude = snapshot_latitude


class CrossBasisEqualityError(BindingArtifactError):
    """Attempted equality across coordinate bases (WGS84 vs SRID 4490 / CGCS2000).

    Per spec §"Station coordinates declare an explicit WGS84 basis and
    cross-basis equality is forbidden" (§4.2) and docs §7.3: binding /
    registry coordinates are WGS84; the ``met.met_station.geom`` DB mirror
    is SRID 4490 (CGCS2000). All equality assertions MUST run within a
    single basis — comparing WGS84 station coordinates against a SRID 4490
    mirror row without an explicit transform is a G5 blocker.
    """

    def __init__(
        self,
        *,
        expected_basis: str,
        supplied_basis: str,
    ) -> None:
        super().__init__(
            f"cross-basis equality forbidden: expected {expected_basis!r} "
            f"but supplied {supplied_basis!r} "
            "(G5 coordinate-basis violation — docs §7.3)"
        )
        self.expected_basis = expected_basis
        self.supplied_basis = supplied_basis


class XyRecomputationMismatchError(BindingArtifactError):
    """Station ``x``/``y`` do not recompute from ``longitude``/``latitude`` + model CRS.

    Per spec §"x/y are recomputable" (§4.2) and docs §7.3: ``x`` and ``y``
    MUST be recomputable from ``longitude``, ``latitude``, and the model
    CRS supplied by SUB-2. A recomputation drift beyond numeric tolerance
    means the binding embedded coordinates from a different oracle than
    the model CRS — a G5 blocker.
    """

    def __init__(
        self,
        *,
        station_id: str,
        recorded_x: float,
        recorded_y: float,
        recomputed_x: float,
        recomputed_y: float,
        tolerance: float,
    ) -> None:
        super().__init__(
            f"station_id={station_id!r} x/y do not recompute from lon/lat + "
            f"model CRS: recorded=({recorded_x!r}, {recorded_y!r}) "
            f"recomputed=({recomputed_x!r}, {recomputed_y!r}) "
            f"tolerance={tolerance!r} "
            "(G5 x/y-recomputability violation)"
        )
        self.station_id = station_id
        self.recorded_x = recorded_x
        self.recorded_y = recorded_y
        self.recomputed_x = recomputed_x
        self.recomputed_y = recomputed_y
        self.tolerance = tolerance


class InvalidZPolicyError(BindingArtifactError):
    """A supplied ``z_policy`` name is not in :data:`ALLOWED_Z_POLICIES`.

    Per spec §"z follows the approved z_policy" (§4.2) and docs §7.5: the
    only approved policies are ``canonical_orography``,
    ``model_dem_at_cell_center``, and ``sentinel``. Any other value
    indicates a caller bug or an Epic #886 verdict escape — refused
    loudly before the binding is emitted.
    """

    def __init__(
        self,
        *,
        supplied_policy: str,
        allowed_policies: tuple[str, ...],
    ) -> None:
        super().__init__(
            f"unknown z_policy {supplied_policy!r}; expected one of "
            f"{list(allowed_policies)!r} (docs §7.5, G5 z-policy violation)"
        )
        self.supplied_policy = supplied_policy
        self.allowed_policies = allowed_policies


class ZPolicyCellMissingError(BindingArtifactError):
    """A ``grid_cell_id`` has no ``z`` value in the supplied ``z_policy``.

    Per spec §"z follows the approved z_policy" (§4.2): the caller must
    supply a ``z_policy`` verdict that covers every used cell. A missing
    coverage entry means the caller is inlining a numeric default instead
    of reading the readiness manifest — a G5 blocker.
    """

    def __init__(
        self,
        *,
        grid_cell_id: str,
        policy_name: str,
    ) -> None:
        super().__init__(
            f"z_policy={policy_name!r} has no z value for grid_cell_id="
            f"{grid_cell_id!r}; the readiness manifest MUST supply z verbatim "
            "for every used cell (G5 z-policy-coverage violation)"
        )
        self.grid_cell_id = grid_cell_id
        self.policy_name = policy_name


class ReadinessManifestChecksumMissingError(BindingArtifactError):
    """The supplied ``z_policy`` does not carry the readiness manifest checksum.

    Per docs §7.5 and §4.2 evidence: the ``z_policy`` MUST bind to a
    specific approved readiness manifest via
    :attr:`ZPolicy.readiness_manifest_checksum`. A blank checksum means
    the policy has no auditable provenance — refused loudly so downstream
    evidence never binds to an unauthored policy.
    """

    def __init__(self, *, policy_name: str) -> None:
        super().__init__(
            f"z_policy={policy_name!r} has no readiness_manifest_checksum; "
            "the approved policy MUST bind to the readiness manifest that "
            "authored it (docs §7.5, G5 provenance violation)"
        )
        self.policy_name = policy_name


# --- structured output dataclasses ----------------------------------------


@dataclass(frozen=True)
class StationBinding:
    """One row of the direct-grid station binding.

    Field set matches the parser's
    :data:`workers.forcing_producer.direct_grid_contract.REQUIRED_STATION_FIELDS`
    exactly, so a round-trip through the parser's
    :func:`~workers.forcing_producer.direct_grid_contract.parse_direct_grid_forcing_contract`
    resolves every field without error. Frozen so downstream evidence
    (SUB-13 mapping evidence package) can bind the record byte-for-byte.

    Coordinate basis
    ----------------
    ``longitude`` and ``latitude`` are always in **WGS84** (EPSG:4326) per
    docs §7.3. The parent :class:`DirectGridManifest` / :class:`BindingArtifact`
    declare the basis explicitly via ``coordinate_reference_system``; per-
    row basis is inherited from the parent. Cross-basis equality against
    SRID 4490 (CGCS2000) DB mirror rows is forbidden — see
    :func:`verify_station_center_matches_snapshot_under_rounding`.

    Attributes
    ----------
    station_id:
        Immutable identity that embeds the mapping-asset identity + grid
        cell reference — never reused across mapping versions (docs §7.4).
    shud_forcing_index:
        1-based contiguous integer assigned by
        :func:`workers.mapping_builder.assign_shud_forcing_index`.
    forcing_filename:
        Safe, pathless, case-fold-unique forcing filename produced by
        :func:`sanitize_station_forcing_filename`. Matches the parser's
        :data:`~workers.forcing_producer.direct_grid_contract._SAFE_STATION_FORCING_FILENAME`
        regex verbatim.
    longitude, latitude:
        WGS84 station center; MUST equal the registered cell center under
        12-decimal rounding (docs §7.3).
    x, y:
        Recomputable from ``longitude`` / ``latitude`` + the model CRS
        WKT supplied by SUB-2 (docs §7.3).
    z:
        Value verbatim from the approved :class:`ZPolicy`
        (:func:`apply_z_policy_from_readiness`).
    grid_id:
        Equal to the parent manifest's ``grid_id`` (parser enforces
        equality; a mismatch would raise :class:`DirectGridContractError`).
    grid_cell_id:
        Snapshot cell identifier; pairwise unique + subset member of the
        loaded snapshot's ordered ``grid_cell_id`` set.
    """

    station_id: str
    shud_forcing_index: int
    forcing_filename: str
    longitude: float
    latitude: float
    x: float
    y: float
    z: float
    grid_id: str
    grid_cell_id: str


@dataclass(frozen=True)
class ZPolicy:
    """Verdict + per-cell values for the ``z_policy`` from Epic #886.

    Per docs §7.5 and §4.2 evidence: the ``z_policy`` verdict is authored
    by the readiness change ``cmfd-direct-grid-platform-readiness`` (Epic
    #886). This SUB reads the verdict verbatim — it does NOT derive or
    inline a numeric default. Callers supply the resolved verdict as a
    :class:`ZPolicy` struct so the mapping builder never has to interpret
    a raw readiness manifest schema (which is still evolving upstream).

    Attributes
    ----------
    policy_name:
        One of :data:`ALLOWED_Z_POLICIES` (``canonical_orography``,
        ``model_dem_at_cell_center``, or ``sentinel``). Any other value
        raises :class:`InvalidZPolicyError` at construction time.
    readiness_manifest_checksum:
        SHA-256 hex of the readiness manifest that approved this verdict.
        Bound so downstream evidence (SUB-13) records the provenance
        verbatim — the mapping builder never invents a policy without a
        readiness manifest source.
    per_cell_z:
        Mapping from ``grid_cell_id`` to ``z`` value. Every used cell in
        the mapping MUST have a coverage entry; a missing entry raises
        :class:`ZPolicyCellMissingError` at :func:`apply_z_policy_from_readiness`
        call time.
    """

    policy_name: str
    readiness_manifest_checksum: str
    per_cell_z: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate at construction so a malformed ZPolicy never propagates
        # into the binding emission pipeline.
        if self.policy_name not in ALLOWED_Z_POLICIES:
            raise InvalidZPolicyError(
                supplied_policy=self.policy_name,
                allowed_policies=tuple(sorted(ALLOWED_Z_POLICIES)),
            )
        if not self.readiness_manifest_checksum.strip():
            raise ReadinessManifestChecksumMissingError(
                policy_name=self.policy_name,
            )


@dataclass(frozen=True)
class BindingArtifact:
    """Standalone binding artifact (JSON blob referenced by ``binding_uri``).

    Per docs §7 and spec §"The manifest's binding_checksum equals the
    SHA-256 of the binding artifact bytes" (§4.1): the mapping builder
    emits a standalone binding artifact as the source of truth for
    per-station rows. The manifest carries a copy of ``station_bindings``
    for the parser, but the standalone artifact's bytes are what the
    :attr:`DirectGridManifest.binding_checksum` binds to.

    Frozen so downstream evidence (SUB-13 mapping evidence package) can
    bind the record byte-for-byte.

    Attributes
    ----------
    bytes:
        Canonical JSON serialization (``json.dumps(sort_keys=True,
        separators=(',', ':'))``). Two runs with identical inputs produce
        byte-identical serializations — the determinism requirement of
        spec §7.
    checksum:
        SHA-256 hex of :attr:`bytes`.
    station_bindings:
        Parsed station rows (in ``shud_forcing_index`` ascending order
        matching the parser's post-sort ordering).
    grid_id, grid_signature:
        Identity metadata carried in the artifact bytes so a downstream
        consumer can bind the rows to the right grid.
    coordinate_reference_system:
        WGS84 basis declaration per docs §7.3 (always
        :data:`WGS84_COORDINATE_BASIS`).
    """

    bytes: bytes
    checksum: str
    station_bindings: tuple[StationBinding, ...]
    grid_id: str
    grid_signature: str
    coordinate_reference_system: str


@dataclass(frozen=True)
class DirectGridManifest:
    """Direct-grid manifest section (nested under
    ``resource_profile.direct_grid_forcing`` per docs §7.1).

    Field set matches the parser's
    :data:`workers.forcing_producer.direct_grid_contract.REQUIRED_MANIFEST_FIELDS`
    exactly (plus the ``forcing_mapping_mode`` discriminator +
    ``station_bindings`` + ``coordinate_reference_system`` declaration per
    docs §7.3). Frozen so downstream evidence can bind the record
    byte-for-byte.

    Emission location
    -----------------
    Per docs §7.1 the manifest MUST be placed under the nested section
    ``resource_profile.direct_grid_forcing``, NOT as a root-level
    ``forcing_mapping_mode``. :meth:`to_resource_profile_dict` produces
    the outer ``resource_profile`` dict shape the parser reads.

    Attributes
    ----------
    forcing_mapping_mode:
        Always :data:`workers.forcing_producer.direct_grid_contract.DIRECT_GRID_MODE`
        (``"direct_grid"``).
    binding_uri:
        Immutable object URI of the standalone binding artifact.
    binding_checksum:
        SHA-256 hex of the binding artifact bytes (must equal
        :attr:`BindingArtifact.checksum` at build time).
    model_input_package_id:
        New mapping-variant model input package identity.
    sp_att_path:
        Package-relative path of the variant ``.sp.att``.
    sp_att_checksum:
        SHA-256 hex of the variant ``.sp.att`` bytes.
    applicable_source_ids:
        Non-empty tuple of source identifiers the mapping applies to.
    grid_id, grid_signature:
        Identity keys of the registered grid snapshot.
    station_bindings:
        Ordered tuple of :class:`StationBinding` records
        (``shud_forcing_index`` ascending).
    coordinate_reference_system:
        WGS84 basis declaration per docs §7.3 (always
        :data:`WGS84_COORDINATE_BASIS` at build time).
    """

    forcing_mapping_mode: str
    binding_uri: str
    binding_checksum: str
    model_input_package_id: str
    sp_att_path: str
    sp_att_checksum: str
    applicable_source_ids: tuple[str, ...]
    grid_id: str
    grid_signature: str
    station_bindings: tuple[StationBinding, ...]
    coordinate_reference_system: str

    def to_resource_profile_dict(self) -> dict[str, Any]:
        """Return the outer ``resource_profile`` dict with the nested contract.

        The shape matches docs §7.1: the ``direct_grid_forcing`` section
        lives under the top-level ``resource_profile`` mapping (never
        root-level ``forcing_mapping_mode``). The section content is the
        canonical parser-shaped mapping — a caller can pass the whole
        return value to
        :func:`workers.forcing_producer.direct_grid_contract.load_forcing_mapping_contract_from_manifest`
        and get back the parsed :class:`DirectGridForcingContract`.
        """
        return {
            DIRECT_GRID_FORCING_SECTION_KEY: self.to_contract_section_dict(),
        }

    def to_contract_section_dict(self) -> dict[str, Any]:
        """Return just the direct-grid contract section (nested payload).

        Useful for callers who want to embed the section into a larger
        manifest.json outside the scope of this SUB.
        """
        return {
            "forcing_mapping_mode": self.forcing_mapping_mode,
            "binding_uri": self.binding_uri,
            "binding_checksum": self.binding_checksum,
            "model_input_package_id": self.model_input_package_id,
            "sp_att_path": self.sp_att_path,
            "sp_att_checksum": self.sp_att_checksum,
            "applicable_source_ids": list(self.applicable_source_ids),
            "grid_id": self.grid_id,
            "grid_signature": self.grid_signature,
            "coordinate_reference_system": self.coordinate_reference_system,
            "station_bindings": [
                _station_binding_to_dict(station)
                for station in self.station_bindings
            ],
        }


# --- internal helpers -----------------------------------------------------


def _sha256_bytes(payload: bytes) -> str:
    """SHA-256 hex digest of an in-memory byte payload."""
    return hashlib.sha256(payload).hexdigest()


def _station_binding_to_dict(station: StationBinding) -> dict[str, Any]:
    """Serialize a :class:`StationBinding` to the parser-shaped dict.

    Field names + types match the parser's
    :data:`~workers.forcing_producer.direct_grid_contract.REQUIRED_STATION_FIELDS`
    verbatim. Numeric fields are emitted as floats (or ints for
    ``shud_forcing_index``) so the parser's ``_required_float`` accepts
    them without a type-cast.
    """
    return {
        "station_id": station.station_id,
        "shud_forcing_index": int(station.shud_forcing_index),
        "forcing_filename": station.forcing_filename,
        "longitude": float(station.longitude),
        "latitude": float(station.latitude),
        "x": float(station.x),
        "y": float(station.y),
        "z": float(station.z),
        "grid_id": station.grid_id,
        "grid_cell_id": station.grid_cell_id,
    }


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Serialize ``payload`` to canonical JSON bytes.

    Matches :func:`packages.common.grid_signature._json_bytes` verbatim:
    ``sort_keys=True`` for order-invariance and ``separators=(',', ':')``
    for whitespace-invariance. Two runs on identical inputs produce
    byte-identical output.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _binding_artifact_payload(
    *,
    grid_id: str,
    grid_signature: str,
    coordinate_reference_system: str,
    station_bindings: Sequence[StationBinding],
) -> dict[str, Any]:
    """Return the JSON-shaped payload for the standalone binding artifact.

    Ordering: station_bindings sorted by ``shud_forcing_index`` ascending
    (matches the parser's post-sort ordering in
    :func:`workers.forcing_producer.direct_grid_contract._station_bindings`).
    """
    sorted_bindings = sorted(
        station_bindings, key=lambda binding: binding.shud_forcing_index
    )
    return {
        "grid_id": grid_id,
        "grid_signature": grid_signature,
        "coordinate_reference_system": coordinate_reference_system,
        "station_bindings": [
            _station_binding_to_dict(binding) for binding in sorted_bindings
        ],
    }


def _parse_binding_artifact_stations(
    binding_bytes: bytes,
) -> tuple[StationBinding, ...]:
    """Parse binding artifact bytes back into :class:`StationBinding` rows.

    Used by :func:`verify_manifest_binding_cross_consistent` — the G5
    cross-consistency gate compares the manifest's embedded
    ``station_bindings`` against the standalone binding artifact's bytes.
    """
    try:
        payload = json.loads(binding_bytes)
    except json.JSONDecodeError as exc:
        raise ManifestBindingDivergenceError(
            divergent_field="<binding_artifact_bytes>",
            station_id="<parse_failure>",
            manifest_value="<manifest.station_bindings>",
            binding_value=f"unparseable JSON: {exc}",
        ) from exc

    if not isinstance(payload, Mapping):
        raise ManifestBindingDivergenceError(
            divergent_field="<binding_artifact_payload_type>",
            station_id="<parse_failure>",
            manifest_value="dict",
            binding_value=type(payload).__name__,
        )

    raw_bindings = payload.get("station_bindings")
    if not isinstance(raw_bindings, list):
        raise ManifestBindingDivergenceError(
            divergent_field="station_bindings",
            station_id="<parse_failure>",
            manifest_value="list",
            binding_value=type(raw_bindings).__name__,
        )
    bindings: list[StationBinding] = []
    for row in raw_bindings:
        if not isinstance(row, Mapping):
            raise ManifestBindingDivergenceError(
                divergent_field="<station_row_type>",
                station_id="<parse_failure>",
                manifest_value="dict",
                binding_value=type(row).__name__,
            )
        bindings.append(
            StationBinding(
                station_id=str(row["station_id"]),
                shud_forcing_index=int(row["shud_forcing_index"]),
                forcing_filename=str(row["forcing_filename"]),
                longitude=float(row["longitude"]),
                latitude=float(row["latitude"]),
                x=float(row["x"]),
                y=float(row["y"]),
                z=float(row["z"]),
                grid_id=str(row["grid_id"]),
                grid_cell_id=str(row["grid_cell_id"]),
            )
        )
    return tuple(bindings)


def _round_coord(value: float) -> float:
    """Round to :data:`COORDINATE_ROUNDING_DECIMALS` (12) decimals.

    Matches :func:`packages.common.grid_signature.grid_signature_tuples`
    verbatim so binding station coordinates and the grid signature share
    the same rounding rule.
    """
    return round(float(value), COORDINATE_ROUNDING_DECIMALS)


# --- public: identity + filename + z_policy computations ------------------


def assign_station_id_from_mapping_asset_identity(
    *,
    mapping_asset_identity: str,
    grid_cell_id: str,
) -> str:
    """Assign a ``station_id`` embedding the immutable mapping-asset identity.

    Per spec §"station_id embeds immutable mapping-asset identity" (§4.2)
    and docs §7.4: the ``mapping_asset_identity`` MUST be version-unique
    (typically the mapping variant's SHA-256 or a UUID) so two different
    mapping versions produce disjoint ``station_id`` sets. This function
    concatenates the identity with the ``grid_cell_id`` under an explicit
    separator that never occurs in either token (``"::"``), yielding an
    immutable, per-cell station identifier.

    The DB mirror's collision policy (docs §7.4) fails closed when the
    same ``station_id`` maps to different ``binding_checksum`` /
    ``model_input_package_id`` / ``grid_signature`` triples — so the
    embedded ``mapping_asset_identity`` is what makes version-reuse
    detectable at the mirror.

    Parameters
    ----------
    mapping_asset_identity:
        Version-unique identity token (non-empty, no whitespace-only).
        The caller (SUB-13 mapping evidence bundler) selects the token.
    grid_cell_id:
        Snapshot cell identifier this station binds to.

    Returns
    -------
    str
        ``f"{mapping_asset_identity}::cell:{grid_cell_id}"`` — always
        non-empty and unique per (identity, cell) pair.

    Raises
    ------
    BindingArtifactError
        Either ``mapping_asset_identity`` or ``grid_cell_id`` is empty
        or whitespace-only.
    """
    identity = mapping_asset_identity.strip()
    cell = grid_cell_id.strip()
    if not identity:
        raise BindingArtifactError(
            "mapping_asset_identity must be non-empty and non-whitespace-only "
            "(G5 identity-token missing)"
        )
    if not cell:
        raise BindingArtifactError(
            "grid_cell_id must be non-empty and non-whitespace-only "
            "(G5 identity-token missing)"
        )
    return f"{identity}::cell:{cell}"


def sanitize_station_forcing_filename(
    *,
    shud_forcing_index: int,
) -> str:
    """Derive a safe, pathless forcing filename from ``shud_forcing_index`` alone.

    Per spec §"filename is not derived from rounded coordinates" (§4.2)
    and docs §7.3: filenames MUST NOT embed coordinates (which is how
    legacy CMFD naming produced ``X<lon>Y<lat>.csv`` collisions). Deriving
    from the integer ``shud_forcing_index`` guarantees:

    * safety-by-construction — the produced name matches the parser's
      :data:`_SAFE_STATION_FORCING_FILENAME` regex;
    * case-fold uniqueness — different indices produce different
      lowercase names;
    * zero coordinate dependence — the name has no lon/lat digits.

    The produced pattern is ``station_{index:05d}.csv`` (zero-padded to 5
    digits so ``station_00001.csv`` through ``station_99999.csv`` sort
    lexicographically to match integer order). Never collides with any
    :data:`RESERVED_FORCING_FILENAMES`, :data:`RESERVED_FILENAME_PREFIXES`,
    or :data:`RESERVED_FILENAME_SUFFIXES`.

    Parameters
    ----------
    shud_forcing_index:
        1-based contiguous integer produced by
        :func:`workers.mapping_builder.assign_shud_forcing_index`. MUST
        be a positive ``int`` (not ``bool``, not ``float``).

    Returns
    -------
    str
        Safe forcing filename matching the parser regex, guaranteed to
        pass :func:`verify_forcing_filename_safety` post-conditions.

    Raises
    ------
    BindingArtifactError
        ``shud_forcing_index`` is not a positive integer or is a bool.
    """
    if type(shud_forcing_index) is not int:  # rejects bool subclass explicitly
        raise BindingArtifactError(
            f"shud_forcing_index must be a positive int, got "
            f"{shud_forcing_index!r} (type={type(shud_forcing_index).__name__}) "
            "(G5 filename-input violation)"
        )
    if shud_forcing_index <= 0:
        raise BindingArtifactError(
            f"shud_forcing_index must be positive, got {shud_forcing_index} "
            "(G5 filename-input violation)"
        )
    filename = f"station_{shud_forcing_index:05d}.csv"
    # Defensive check: our derivation is safe-by-construction, but any
    # future refactor that changes the derivation MUST still pass the
    # parser regex — assert this here so a regression is caught at the
    # sanitize call site rather than downstream.
    if not _SAFE_STATION_FORCING_FILENAME.fullmatch(filename):
        raise ForcingFilenameUnsafeError(forcing_filename=filename)
    return filename


def apply_z_policy_from_readiness(
    z_policy: ZPolicy,
    grid_cell_id: str,
) -> float:
    """Return the ``z`` value from an approved :class:`ZPolicy` verbatim.

    Per spec §"z follows the approved z_policy" (§4.2), docs §7.5, and
    Epic #886 ``cmfd-direct-grid-platform-readiness``: the ``z`` value
    comes from the readiness manifest verbatim — never inlined as a
    numeric default. The :class:`ZPolicy` struct is the caller-supplied
    resolved verdict; this function reads the per-cell z coverage entry.

    Parameters
    ----------
    z_policy:
        Approved verdict from Epic #886. Its ``policy_name`` MUST be in
        :data:`ALLOWED_Z_POLICIES` (validated at :class:`ZPolicy`
        construction).
    grid_cell_id:
        Cell whose ``z`` value the caller needs.

    Returns
    -------
    float
        The ``z`` value from :attr:`ZPolicy.per_cell_z` for
        ``grid_cell_id``.

    Raises
    ------
    ZPolicyCellMissingError
        ``grid_cell_id`` has no coverage entry in the supplied policy —
        the caller MUST NOT invent a numeric default.
    """
    if grid_cell_id not in z_policy.per_cell_z:
        raise ZPolicyCellMissingError(
            grid_cell_id=grid_cell_id,
            policy_name=z_policy.policy_name,
        )
    return float(z_policy.per_cell_z[grid_cell_id])


# --- public: safety / verify gates ---------------------------------------


def _classify_reserved_filename_collision(
    filename: str,
) -> tuple[str, str] | None:
    """Return ``(collision_kind, collided_with)`` iff ``filename`` collides.

    Returns ``None`` iff no reserved-name collision. All comparisons are
    case-insensitive to catch collisions on case-insensitive filesystems
    (macOS APFS/HFS+, Windows NTFS default).
    """
    lowered = filename.lower()
    for reserved in RESERVED_FORCING_FILENAMES:
        if lowered == reserved.lower():
            return "reserved_exact_match", reserved
    for prefix in RESERVED_FILENAME_PREFIXES:
        if lowered.startswith(prefix.lower()):
            return "reserved_prefix", prefix
    for suffix in RESERVED_FILENAME_SUFFIXES:
        if lowered.endswith(suffix.lower()):
            return "reserved_suffix", suffix
    return None


def verify_grid_cell_id_unique_and_snapshot_member(
    station_bindings: Sequence[StationBinding],
    snapshot_cells: Sequence[CanonicalGridCell],
) -> None:
    """Fail-closed gate: every binding ``grid_cell_id`` is unique + in snapshot.

    Per spec §"grid_cell_id is unique within the binding and exists in
    the registered grid snapshot" (§4.1) and docs §7.3 additional
    invariants: enforces two distinct properties per §4.1 Required-
    evidence:

    * **Uniqueness**: every ``grid_cell_id`` appears in at most one
      station binding (one cell = one station).
    * **Snapshot membership**: every ``grid_cell_id`` is a member of the
      loaded snapshot's ordered ``grid_cell_id`` set (never invents a
      cell missing from the registry).

    Parameters
    ----------
    station_bindings:
        The station rows to check.
    snapshot_cells:
        The full snapshot cell set (from the SUB-5 in-memory fixture or
        the production grid registry loader).

    Raises
    ------
    GridCellIdDuplicateError
        A ``grid_cell_id`` appears in more than one binding row.
    GridCellIdNotInSnapshotError
        A ``grid_cell_id`` is not a member of ``snapshot_cells``.
    """
    snapshot_ids = frozenset(cell.grid_cell_id for cell in snapshot_cells)
    seen: dict[str, list[str]] = {}
    for binding in station_bindings:
        seen.setdefault(binding.grid_cell_id, []).append(binding.station_id)
    for grid_cell_id, station_ids in seen.items():
        if len(station_ids) > 1:
            raise GridCellIdDuplicateError(
                grid_cell_id=grid_cell_id,
                station_ids=tuple(station_ids),
            )
    for binding in station_bindings:
        if binding.grid_cell_id not in snapshot_ids:
            raise GridCellIdNotInSnapshotError(
                grid_cell_id=binding.grid_cell_id,
                station_id=binding.station_id,
                snapshot_cell_count=len(snapshot_cells),
            )


def verify_station_center_matches_snapshot_under_rounding(
    station: StationBinding,
    snapshot_cell: CanonicalGridCell,
    *,
    snapshot_basis: str = WGS84_COORDINATE_BASIS,
    station_basis: str = WGS84_COORDINATE_BASIS,
) -> None:
    """Fail-closed gate: station lon/lat equal cell center under 12-decimal rounding.

    Per spec §"Station lon/lat equal the registered cell center under
    rounding" (§4.2) and docs §7.3: comparison MUST use the same
    12-decimal rounding as :func:`packages.common.grid_signature.grid_signature_tuples`
    — never raw float-literal equality, because live coordinates carry
    ~1e-7° noise that would trip a float ``==`` on legitimate matches.

    Coordinate basis
    ----------------
    Both operands MUST be in the WGS84 basis (EPSG:4326) per docs §7.3.
    ``station_basis`` and ``snapshot_basis`` default to
    :data:`WGS84_COORDINATE_BASIS`; any other value raises
    :class:`CrossBasisEqualityError` BEFORE the numeric comparison runs.
    Cross-basis equality against SRID 4490 (CGCS2000) DB mirror rows is
    forbidden.

    Parameters
    ----------
    station:
        Station binding row.
    snapshot_cell:
        Registered snapshot cell (from the SUB-5 in-memory fixture or the
        production loader).
    snapshot_basis:
        Basis label of ``snapshot_cell`` coordinates. MUST equal
        :data:`WGS84_COORDINATE_BASIS`; any other value is a G5 blocker.
    station_basis:
        Basis label of ``station`` coordinates. MUST equal
        :data:`WGS84_COORDINATE_BASIS`; any other value is a G5 blocker.

    Raises
    ------
    CrossBasisEqualityError
        Either ``snapshot_basis`` or ``station_basis`` is not
        :data:`WGS84_COORDINATE_BASIS`.
    StationCenterMismatchError
        After 12-decimal rounding the station lon/lat differ from the
        cell center lon/lat.
    """
    if station_basis != WGS84_COORDINATE_BASIS:
        raise CrossBasisEqualityError(
            expected_basis=WGS84_COORDINATE_BASIS,
            supplied_basis=station_basis,
        )
    if snapshot_basis != WGS84_COORDINATE_BASIS:
        raise CrossBasisEqualityError(
            expected_basis=WGS84_COORDINATE_BASIS,
            supplied_basis=snapshot_basis,
        )
    if _round_coord(station.longitude) != _round_coord(
        snapshot_cell.longitude
    ) or _round_coord(station.latitude) != _round_coord(snapshot_cell.latitude):
        raise StationCenterMismatchError(
            grid_cell_id=station.grid_cell_id,
            station_id=station.station_id,
            station_longitude=station.longitude,
            station_latitude=station.latitude,
            snapshot_longitude=snapshot_cell.longitude,
            snapshot_latitude=snapshot_cell.latitude,
        )


def verify_manifest_binding_cross_consistent(
    manifest: DirectGridManifest,
    binding_artifact: BindingArtifact,
) -> None:
    """Fail-closed G5 gate: manifest ``station_bindings`` == binding artifact rows.

    Per spec §"Manifest and binding artifact are cross-consistent (G5)"
    (§4.1) and docs §Gate G5: the manifest's ``station_bindings`` row set
    MUST equal the standalone binding artifact's row set element-for-
    element (same ``station_id``, ``shud_forcing_index``, ``grid_cell_id``,
    and 12-decimal-rounded lon/lat). Divergence is a G5 blocker; the
    :class:`ManifestBindingDivergenceError` names the offending row + field.

    Additionally: the manifest's ``binding_checksum`` MUST equal the
    binding artifact's ``checksum`` (both are SHA-256 of the same
    canonical JSON bytes); a mismatch raises
    :class:`BindingChecksumMismatchError`.

    Also: the manifest's ``grid_id`` and ``grid_signature`` MUST equal
    the binding artifact's; divergence raises
    :class:`ManifestBindingDivergenceError` with
    ``divergent_field="grid_id"`` / ``"grid_signature"``.

    Parameters
    ----------
    manifest:
        The emitted :class:`DirectGridManifest`.
    binding_artifact:
        The emitted :class:`BindingArtifact`.

    Raises
    ------
    BindingChecksumMismatchError
        ``manifest.binding_checksum`` differs from ``binding_artifact.checksum``.
    ManifestBindingDivergenceError
        Any per-row or grid-identity divergence between the two artifacts.
    """
    # Checksum first — the fastest failure and the most consequential (a
    # divergent binding_checksum means the runtime consumer would read the
    # wrong artifact even if the row content happened to align).
    if manifest.binding_checksum != binding_artifact.checksum:
        raise BindingChecksumMismatchError(
            manifest_checksum=manifest.binding_checksum,
            recomputed_checksum=binding_artifact.checksum,
        )
    if manifest.grid_id != binding_artifact.grid_id:
        raise ManifestBindingDivergenceError(
            divergent_field="grid_id",
            station_id="<manifest_level>",
            manifest_value=manifest.grid_id,
            binding_value=binding_artifact.grid_id,
        )
    if manifest.grid_signature != binding_artifact.grid_signature:
        raise ManifestBindingDivergenceError(
            divergent_field="grid_signature",
            station_id="<manifest_level>",
            manifest_value=manifest.grid_signature,
            binding_value=binding_artifact.grid_signature,
        )

    # Now parse the binding artifact bytes independently (the G5 spec
    # requires the row set match after parsing the standalone artifact's
    # bytes, not just its cached station_bindings tuple — this catches a
    # subtle bug where the emitter's in-memory tuple diverges from what
    # was actually serialized to bytes).
    parsed_bindings = _parse_binding_artifact_stations(binding_artifact.bytes)

    manifest_by_station_id = {
        binding.station_id: binding for binding in manifest.station_bindings
    }
    parsed_by_station_id = {
        binding.station_id: binding for binding in parsed_bindings
    }
    manifest_ids = set(manifest_by_station_id)
    parsed_ids = set(parsed_by_station_id)
    if manifest_ids != parsed_ids:
        # Pick a first-mismatch station to name in the error.
        missing_from_binding = manifest_ids - parsed_ids
        missing_from_manifest = parsed_ids - manifest_ids
        if missing_from_binding:
            offending = sorted(missing_from_binding)[0]
            raise ManifestBindingDivergenceError(
                divergent_field="station_id_set",
                station_id=offending,
                manifest_value="present",
                binding_value="absent",
            )
        offending = sorted(missing_from_manifest)[0]
        raise ManifestBindingDivergenceError(
            divergent_field="station_id_set",
            station_id=offending,
            manifest_value="absent",
            binding_value="present",
        )
    for station_id in sorted(manifest_by_station_id):
        m_row = manifest_by_station_id[station_id]
        b_row = parsed_by_station_id[station_id]
        if m_row.shud_forcing_index != b_row.shud_forcing_index:
            raise ManifestBindingDivergenceError(
                divergent_field="shud_forcing_index",
                station_id=station_id,
                manifest_value=m_row.shud_forcing_index,
                binding_value=b_row.shud_forcing_index,
            )
        if m_row.grid_cell_id != b_row.grid_cell_id:
            raise ManifestBindingDivergenceError(
                divergent_field="grid_cell_id",
                station_id=station_id,
                manifest_value=m_row.grid_cell_id,
                binding_value=b_row.grid_cell_id,
            )
        if _round_coord(m_row.longitude) != _round_coord(b_row.longitude):
            raise ManifestBindingDivergenceError(
                divergent_field="longitude",
                station_id=station_id,
                manifest_value=_round_coord(m_row.longitude),
                binding_value=_round_coord(b_row.longitude),
            )
        if _round_coord(m_row.latitude) != _round_coord(b_row.latitude):
            raise ManifestBindingDivergenceError(
                divergent_field="latitude",
                station_id=station_id,
                manifest_value=_round_coord(m_row.latitude),
                binding_value=_round_coord(b_row.latitude),
            )


def verify_binding_round_trips_parser(
    resource_profile: Mapping[str, Any],
    *,
    source_id: str | None = None,
) -> DirectGridForcingContract:
    """Fail-closed G5 gate: emitted manifest round-trips through the parser.

    Per spec §"The emitted manifest parses cleanly through the existing
    direct-grid contract parser" (§4.1): the mapping builder MUST emit a
    manifest whose fields satisfy
    :mod:`workers.forcing_producer.direct_grid_contract` verbatim. This
    gate calls the parser's public
    :func:`~workers.forcing_producer.direct_grid_contract.load_forcing_mapping_contract_from_manifest`
    and returns the parsed :class:`DirectGridForcingContract` on pass;
    any :class:`DirectGridContractError` is wrapped as
    :class:`ParserRoundTripError` so callers can catch G5 failures
    without accidentally absorbing the parser's own error family.

    Parameters
    ----------
    resource_profile:
        The outer ``resource_profile`` dict produced by
        :meth:`DirectGridManifest.to_resource_profile_dict`. This dict
        contains the ``direct_grid_forcing`` nested section that the
        parser reads.
    source_id:
        Optional caller source identifier. When supplied, the parser
        additionally verifies the manifest's ``applicable_source_ids``
        includes ``source_id``.

    Returns
    -------
    DirectGridForcingContract
        The parser's typed contract on pass — SUB-13 evidence records the
        contract fields verbatim.

    Raises
    ------
    ParserRoundTripError
        The parser refused the manifest. The wrapped
        :class:`DirectGridContractError` message is available on the
        exception.
    """
    try:
        contract = load_forcing_mapping_contract_from_manifest(
            resource_profile,
            source_id=source_id,
            allow_root_direct_grid=False,
        )
    except DirectGridContractError as exc:
        raise ParserRoundTripError(
            parser_error_message=str(exc),
            parser_field=exc.field,
        ) from exc
    if contract is None:
        # Parser returned None (mode was IDW or the manifest has no
        # direct-grid section). This is a G5 blocker — the mapping builder
        # ONLY emits direct-grid manifests, never IDW-mode manifests.
        raise ParserRoundTripError(
            parser_error_message=(
                "parser returned None (manifest was not direct-grid mode); "
                "the mapping builder MUST NOT emit non-direct-grid manifests"
            ),
        )
    return contract


def recompute_binding_and_sp_att_checksums(
    binding_artifact_bytes: bytes,
    sp_att_bytes: bytes,
) -> tuple[str, str]:
    """Compute ``(binding_checksum, sp_att_checksum)`` from the emitted bytes.

    Per spec §"binding_checksum equals the SHA-256 of the emitted binding
    artifact bytes and sp_att_checksum equals the SHA-256 of the emitted
    variant .sp.att bytes" (§4.1): both checksums are SHA-256 hex of the
    in-memory bytes. Returned as a pair so the caller can bind them to
    the manifest fields verbatim.

    Parameters
    ----------
    binding_artifact_bytes:
        Canonical JSON bytes of the standalone binding artifact (from
        :attr:`BindingArtifact.bytes`).
    sp_att_bytes:
        Bytes of the emitted variant ``.sp.att`` file.

    Returns
    -------
    tuple[str, str]
        ``(binding_checksum, sp_att_checksum)`` — 64 lowercase hex chars
        each.
    """
    return (
        _sha256_bytes(binding_artifact_bytes),
        _sha256_bytes(sp_att_bytes),
    )


# --- public: orchestrator -------------------------------------------------


def emit_direct_grid_manifest_and_binding(
    *,
    used_cells: Sequence[CanonicalGridCell],
    snapshot_cells: Sequence[CanonicalGridCell],
    shud_forcing_index: Mapping[str, int],
    mapping_asset_identity: str,
    model_input_package_id: str,
    sp_att_path: str,
    sp_att_bytes: bytes,
    applicable_source_ids: Sequence[str],
    grid_id: str,
    grid_signature: str,
    z_policy: ZPolicy,
    binding_uri: str,
    model_crs_wkt: str,
    coordinate_reference_system: str = WGS84_COORDINATE_BASIS,
) -> tuple[DirectGridManifest, BindingArtifact]:
    """§4.1 orchestrator: emit the direct-grid manifest + binding artifact.

    Fail-closed BEFORE returning: runs the G5 gates inline in the order:

    1. Compute each station's ``x`` / ``y`` from ``longitude`` / ``latitude``
       via a WGS84 -> model-CRS ``pyproj.Transformer`` built from
       ``model_crs_wkt``.
    2. Assemble :class:`StationBinding` rows for every used cell with
       filenames from :func:`sanitize_station_forcing_filename`,
       station_ids from :func:`assign_station_id_from_mapping_asset_identity`,
       and z values from :func:`apply_z_policy_from_readiness`.
    3. Verify grid_cell_id uniqueness + snapshot membership
       (:func:`verify_grid_cell_id_unique_and_snapshot_member`).
    4. Verify each station center matches the snapshot cell under
       12-decimal rounding
       (:func:`verify_station_center_matches_snapshot_under_rounding`).
    5. Verify no filename collision against
       :data:`RESERVED_FORCING_FILENAMES` /
       :data:`RESERVED_FILENAME_PREFIXES` /
       :data:`RESERVED_FILENAME_SUFFIXES` and that all filenames are
       case-fold unique across the binding.
    6. Serialize the binding artifact to canonical JSON bytes and
       recompute both checksums via
       :func:`recompute_binding_and_sp_att_checksums`.
    7. Assemble the :class:`DirectGridManifest` with the recomputed
       checksums.
    8. Verify cross-consistency between manifest and binding artifact
       (:func:`verify_manifest_binding_cross_consistent`).
    9. Verify the manifest round-trips through the parser
       (:func:`verify_binding_round_trips_parser`).

    Any raise happens BEFORE the function returns; the caller cannot
    obtain a partial manifest / binding artifact.

    Parameters
    ----------
    used_cells:
        Cells returned by
        :func:`workers.mapping_builder.derive_used_cell_subset`
        (``canonical_ordinal`` ascending).
    snapshot_cells:
        Full loaded snapshot cell set — used for the snapshot-membership
        check (independent of the used-cell subset).
    shud_forcing_index:
        Mapping from ``grid_cell_id`` -> ``shud_forcing_index`` returned
        by :func:`workers.mapping_builder.assign_shud_forcing_index`.
    mapping_asset_identity:
        Version-unique identity token for this mapping build (typically
        the mapping variant's SHA-256 or UUID). Embedded verbatim in
        every ``station_id``.
    model_input_package_id:
        New mapping-variant model input package identity.
    sp_att_path:
        Package-relative path of the variant ``.sp.att``.
    sp_att_bytes:
        Bytes of the emitted variant ``.sp.att`` file — SHA-256 becomes
        ``sp_att_checksum``.
    applicable_source_ids:
        Non-empty sequence of source identifiers the mapping applies to.
    grid_id, grid_signature:
        Identity keys of the registered grid snapshot. ``grid_signature``
        MUST be the value computed via the shared
        :func:`packages.common.grid_signature.grid_signature_hash` helper
        by the SUB-5 G2 gate — this function does NOT recompute it.
    z_policy:
        Approved ``z_policy`` verdict from Epic #886. Consumed verbatim
        by :func:`apply_z_policy_from_readiness`.
    binding_uri:
        Immutable object URI where the binding artifact will be stored.
    model_crs_wkt:
        WKT of the model CRS (from the checksum-bound package ``.prj``,
        supplied by SUB-2). Used to build the WGS84 -> model-CRS
        transformer for ``x`` / ``y`` recomputation.
    coordinate_reference_system:
        Coordinate basis label to declare on the manifest and binding.
        Defaults to :data:`WGS84_COORDINATE_BASIS` (docs §7.3
        requirement); any other value raises
        :class:`CrossBasisEqualityError` from the downstream
        :func:`verify_station_center_matches_snapshot_under_rounding`.

    Returns
    -------
    tuple[DirectGridManifest, BindingArtifact]
        The emitted manifest + binding artifact, both frozen.

    Raises
    ------
    BindingArtifactError
        Any G5 blocker discovered by the inline gates.
    """
    if not used_cells:
        raise BindingArtifactError(
            "used_cells must be non-empty; the mapping builder MUST NOT emit "
            "a direct-grid binding for a zero-cell basin "
            "(G5 empty-binding violation)"
        )
    if not applicable_source_ids:
        raise ManifestFieldMissingError(field_name="applicable_source_ids")
    if not grid_id.strip():
        raise ManifestFieldMissingError(field_name="grid_id")
    if not grid_signature.strip():
        raise ManifestFieldMissingError(field_name="grid_signature")
    if not model_input_package_id.strip():
        raise ManifestFieldMissingError(field_name="model_input_package_id")
    if not sp_att_path.strip():
        raise ManifestFieldMissingError(field_name="sp_att_path")
    if not binding_uri.strip():
        raise ManifestFieldMissingError(field_name="binding_uri")

    # --- x / y transformer from model CRS -------------------------------
    try:
        model_crs = pyproj.CRS.from_wkt(model_crs_wkt)
    except pyproj.exceptions.CRSError as exc:
        raise BindingArtifactError(
            f"model_crs_wkt cannot be parsed as a CRS: {exc} "
            "(G5 model-CRS parse violation)"
        ) from exc
    lonlat_to_model = pyproj.Transformer.from_crs(
        "EPSG:4326", model_crs, always_xy=True
    )

    # --- assemble station bindings --------------------------------------
    station_bindings: list[StationBinding] = []
    for used_cell in used_cells:
        forcing_index = shud_forcing_index.get(used_cell.grid_cell_id)
        if forcing_index is None:
            raise BindingArtifactError(
                f"shud_forcing_index has no entry for used cell "
                f"grid_cell_id={used_cell.grid_cell_id!r} "
                "(G5 ownership-consistency violation)"
            )
        station_id = assign_station_id_from_mapping_asset_identity(
            mapping_asset_identity=mapping_asset_identity,
            grid_cell_id=used_cell.grid_cell_id,
        )
        forcing_filename = sanitize_station_forcing_filename(
            shud_forcing_index=int(forcing_index),
        )
        z_value = apply_z_policy_from_readiness(z_policy, used_cell.grid_cell_id)
        # Recompute x / y from the snapshot cell's WGS84 lon/lat + model CRS.
        recomputed_x, recomputed_y = lonlat_to_model.transform(
            float(used_cell.longitude), float(used_cell.latitude)
        )
        station_bindings.append(
            StationBinding(
                station_id=station_id,
                shud_forcing_index=int(forcing_index),
                forcing_filename=forcing_filename,
                longitude=float(used_cell.longitude),
                latitude=float(used_cell.latitude),
                x=float(recomputed_x),
                y=float(recomputed_y),
                z=float(z_value),
                grid_id=grid_id,
                grid_cell_id=used_cell.grid_cell_id,
            )
        )

    # Sort by shud_forcing_index ascending so the emitted binding and
    # manifest have deterministic ordering matching the parser's post-sort.
    station_bindings.sort(key=lambda binding: binding.shud_forcing_index)
    station_bindings_tuple = tuple(station_bindings)

    # --- G5 inline gates: uniqueness + snapshot membership --------------
    verify_grid_cell_id_unique_and_snapshot_member(
        station_bindings_tuple, snapshot_cells
    )

    # --- G5 inline gates: filename collision + case-fold uniqueness -----
    seen_lower_names: dict[str, str] = {}
    for binding in station_bindings_tuple:
        collision = _classify_reserved_filename_collision(binding.forcing_filename)
        if collision is not None:
            kind, collided_with = collision
            raise ForcingFilenameCollisionError(
                forcing_filename=binding.forcing_filename,
                collision_kind=kind,
                collided_with=collided_with,
            )
        lowered = binding.forcing_filename.lower()
        if lowered in seen_lower_names:
            raise ForcingFilenameCollisionError(
                forcing_filename=binding.forcing_filename,
                collision_kind="case_fold_duplicate",
                collided_with=seen_lower_names[lowered],
            )
        seen_lower_names[lowered] = binding.forcing_filename

    # --- G5 inline gates: station center under 12-decimal rounding ------
    snapshot_by_grid_cell_id = {
        cell.grid_cell_id: cell for cell in snapshot_cells
    }
    for binding in station_bindings_tuple:
        snapshot_cell = snapshot_by_grid_cell_id[binding.grid_cell_id]
        verify_station_center_matches_snapshot_under_rounding(
            binding,
            snapshot_cell,
            snapshot_basis=coordinate_reference_system,
            station_basis=coordinate_reference_system,
        )

    # --- serialize binding artifact + compute checksums -----------------
    binding_payload = _binding_artifact_payload(
        grid_id=grid_id,
        grid_signature=grid_signature,
        coordinate_reference_system=coordinate_reference_system,
        station_bindings=station_bindings_tuple,
    )
    binding_bytes = _canonical_json_bytes(binding_payload)
    binding_checksum, sp_att_checksum = recompute_binding_and_sp_att_checksums(
        binding_bytes, sp_att_bytes
    )
    binding_artifact = BindingArtifact(
        bytes=binding_bytes,
        checksum=binding_checksum,
        station_bindings=station_bindings_tuple,
        grid_id=grid_id,
        grid_signature=grid_signature,
        coordinate_reference_system=coordinate_reference_system,
    )

    # --- assemble manifest ---------------------------------------------
    manifest = DirectGridManifest(
        forcing_mapping_mode=DIRECT_GRID_MODE,
        binding_uri=binding_uri,
        binding_checksum=binding_checksum,
        model_input_package_id=model_input_package_id,
        sp_att_path=sp_att_path,
        sp_att_checksum=sp_att_checksum,
        applicable_source_ids=tuple(applicable_source_ids),
        grid_id=grid_id,
        grid_signature=grid_signature,
        station_bindings=station_bindings_tuple,
        coordinate_reference_system=coordinate_reference_system,
    )

    # --- G5 inline gates: cross-consistency + parser round-trip --------
    verify_manifest_binding_cross_consistent(manifest, binding_artifact)
    verify_binding_round_trips_parser(
        manifest.to_resource_profile_dict(),
    )

    return manifest, binding_artifact
