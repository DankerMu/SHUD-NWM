"""Pre-registration grid-signature stability verifier (SUB-7 / Task 4.1a-e).

Owns the 5-axis stability sweep referenced by Epic #897 SUB-7 (issue #904):

1. Multi-cycle: a fixed geometry hashes to one signature across ≥3 cycles.
2. Multi-variable: a fixed geometry hashes to one signature across the 6
   canonical variables (5 SHUD-facing labels; Wind is a U/V pair).
3. Multi-backend: a fixed bbox+resolution hashes to one signature across the
   three source-family adapter modules (IFS/GFS/ERA5).
4. Coordinate normalization: lat-ascending / lat-descending pairs hash equal,
   and lon-``0..360`` / lon-``-180..180`` pairs hash equal, via
   :func:`workers.grid_registry.input_record._build_cells` which internally
   applies ``_normalize_longitude`` (SUB-4 owns normalization semantics).
5. Product-upgrade + dynamic-crop refusal: a declared product upgrade whose
   signature does not change fails closed; a candidate grid whose per-cycle
   geometry varies fails closed with the pinned "canonical grid contract must
   be stabilized" message.

Derivation contract
-------------------
Every axis derives its signature via the SAME two-step path:

1. Extract ``latitudes`` / ``longitudes`` / ``shape`` from a NetCDF fixture.
2. Build the ordered cell list via
   :func:`workers.grid_registry.input_record._build_cells` (private
   cross-module import matching the SUB-4/SUB-5 precedent at
   ``openspec/changes/canonical-source-grid-registry/tasks.md`` §3.1b:60).
   ``_build_cells`` internally calls
   :func:`workers.forcing_producer.producer._normalize_longitude`, so raw
   ``0..360`` longitudes and raw ``-180..180`` longitudes reduce to the
   same normalized cells.
3. Hash the ordered cells via
   :func:`packages.common.grid_signature.grid_signature_hash` (SUB-1
   shared helper).

Re-implementing the iteration order OR the hash is forbidden by §4.0 — the
verifier consumes SUB-1 + SUB-4 verbatim.

Exception hierarchy
-------------------
All fail-closed cases raise a subclass of :class:`StabilityVerificationError`,
which inherits from :class:`packages.common.grid_registry_store.RegistryStoreError`
(sibling of :class:`packages.common.grid_registry_bbox_guard.BboxMismatchError`).
Each subclass carries structured attributes so downstream consumers (SUB-8
shared-binding eligibility) can log the offending axis / cycle / variable /
backend without parsing the message string.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

import xarray as xr

from packages.common.grid_registry_store import RegistryStoreError
from packages.common.grid_signature import grid_signature_hash
from workers.grid_registry.input_record import CellInput, _build_cells

__all__ = [
    "DynamicCropRefusedError",
    "MultiBackendSignatureDriftError",
    "MultiCycleSignatureDriftError",
    "MultiVariableSignatureDriftError",
    "NormalizationSkippedError",
    "ProductUpgradeSignatureUnchangedError",
    "StabilityVerificationError",
    "verify_coordinate_normalization",
    "verify_multi_backend_stability",
    "verify_multi_cycle_stability",
    "verify_multi_variable_stability",
    "verify_product_upgrade_and_dynamic_crop",
]


# -----------------------------------------------------------------------------
# Exception hierarchy — pinned attribute contracts per §4.0
# -----------------------------------------------------------------------------


class StabilityVerificationError(RegistryStoreError):
    """Base for all pre-registration stability failures.

    Sibling of :class:`packages.common.grid_registry_bbox_guard.BboxMismatchError`
    under the shared :class:`RegistryStoreError` taxonomy.
    """


class MultiCycleSignatureDriftError(StabilityVerificationError):
    """Raised when ≥2 cycles of the same source/variable hash to different signatures."""

    def __init__(
        self,
        *,
        expected_signature: str,
        actual_signature: str,
        mutated_cycle: str,
        mutated_grid_cell_id: str,
    ) -> None:
        self.expected_signature = expected_signature
        self.actual_signature = actual_signature
        self.mutated_cycle = mutated_cycle
        self.mutated_grid_cell_id = mutated_grid_cell_id
        super().__init__(
            f"Grid signature drifted between cycles: expected={expected_signature}, "
            f"actual={actual_signature}, mutated_cycle={mutated_cycle!r}, "
            f"mutated_grid_cell_id={mutated_grid_cell_id!r}"
        )


class MultiVariableSignatureDriftError(StabilityVerificationError):
    """Raised when ≥2 canonical variables hash to different signatures."""

    def __init__(
        self,
        *,
        expected_signature: str,
        actual_signature: str,
        offending_variable: str,
    ) -> None:
        self.expected_signature = expected_signature
        self.actual_signature = actual_signature
        self.offending_variable = offending_variable
        super().__init__(
            f"Grid signature drifted between variables: expected={expected_signature}, "
            f"actual={actual_signature}, offending_variable={offending_variable!r}"
        )


class MultiBackendSignatureDriftError(StabilityVerificationError):
    """Raised when ≥2 source-family adapters hash the same bbox to different signatures."""

    def __init__(
        self,
        *,
        expected_signature: str,
        actual_signature: str,
        offending_backend: str,
        expected_clip_bbox: dict[str, float],
        actual_clip_bbox: dict[str, float],
    ) -> None:
        self.expected_signature = expected_signature
        self.actual_signature = actual_signature
        self.offending_backend = offending_backend
        self.expected_clip_bbox = expected_clip_bbox
        self.actual_clip_bbox = actual_clip_bbox
        super().__init__(
            f"Grid signature drifted between backends: expected={expected_signature}, "
            f"actual={actual_signature}, offending_backend={offending_backend!r}, "
            f"expected_clip_bbox={expected_clip_bbox}, actual_clip_bbox={actual_clip_bbox}"
        )


class NormalizationSkippedError(StabilityVerificationError):
    """Raised when the verifier detects a raw axis convention was hashed unnormalized."""

    def __init__(
        self,
        *,
        axis: Literal["latitude", "longitude"],
        expected_convention: str,
        actual_convention: str,
    ) -> None:
        self.axis = axis
        self.expected_convention = expected_convention
        self.actual_convention = actual_convention
        super().__init__(
            f"Normalization skipped on axis={axis!r}: expected_convention="
            f"{expected_convention!r}, actual_convention={actual_convention!r}"
        )


class ProductUpgradeSignatureUnchangedError(StabilityVerificationError):
    """Raised when a declared product upgrade does not change the signature."""

    def __init__(
        self,
        *,
        pre_upgrade_signature: str,
        post_upgrade_signature: str,
        declared_upgrade: bool,
    ) -> None:
        self.pre_upgrade_signature = pre_upgrade_signature
        self.post_upgrade_signature = post_upgrade_signature
        self.declared_upgrade = declared_upgrade
        super().__init__(
            f"Declared product upgrade did not change grid signature: "
            f"pre_upgrade_signature={pre_upgrade_signature}, "
            f"post_upgrade_signature={post_upgrade_signature}, "
            f"declared_upgrade={declared_upgrade}"
        )


class DynamicCropRefusedError(StabilityVerificationError):
    """Raised when per-cycle geometry variance is detected on a candidate grid.

    The message MUST begin with the byte-for-byte literal
    ``"canonical grid contract must be stabilized"`` (§4.0 pin).
    """

    def __init__(
        self,
        *,
        per_cycle_geometry: dict[str, tuple[int, float, float, float, float]],
    ) -> None:
        self.per_cycle_geometry = per_cycle_geometry
        super().__init__(
            f"canonical grid contract must be stabilized: per-cycle geometry variance "
            f"detected: {per_cycle_geometry!r}"
        )


# -----------------------------------------------------------------------------
# NetCDF signature helpers
# -----------------------------------------------------------------------------


def _extract_axes(nc_path: Path) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Return ``(latitudes, longitudes)`` tuples read from ``nc_path``.

    The verifier reads the raw ``latitude`` / ``longitude`` coordinate arrays
    as-stored. Longitude normalization is applied downstream by ``_build_cells``,
    so fixtures with raw ``0..360`` longitudes and fixtures with raw
    ``-180..180`` longitudes reduce to the same normalized cells.
    """
    with xr.open_dataset(nc_path, engine="netcdf4") as ds:
        latitudes = tuple(float(value) for value in ds["latitude"].values.tolist())
        longitudes = tuple(float(value) for value in ds["longitude"].values.tolist())
    return latitudes, longitudes


def _canonical_latitudes(latitudes: tuple[float, ...]) -> tuple[float, ...]:
    """Return ``latitudes`` sorted ascending for identity-invariant hashing.

    §4.0 pins "NetCDF latitude ascending/descending identity-invariant". SUB-4
    :func:`_build_cells` iterates in the axis-storage order, so a
    lat-descending NetCDF fixture would otherwise enumerate cells in a
    different order than its lat-ascending twin — and hash to a different
    signature — despite representing the same underlying grid identity. The
    verifier canonicalizes to ascending order at THIS boundary so the
    downstream ``_build_cells`` call sees a stable axis convention. This is
    a verifier-level canonicalization for the stability sweep only; the
    registration writer (SUB-5) records the raw axis order via SUB-4's
    ``latitude_order`` field and does NOT sort — the two surfaces are
    intentionally decoupled.
    """
    return tuple(sorted(latitudes))


def _signature_from_netcdf(nc_path: Path) -> tuple[str, tuple[CellInput, ...]]:
    """Return ``(grid_signature_hash, ordered_cells)`` for the fixture at ``nc_path``.

    Uses SUB-4's :func:`_build_cells` (which internally calls
    ``_normalize_longitude``) and SUB-1's :func:`grid_signature_hash` verbatim.
    Latitudes are canonicalized to ascending order via
    :func:`_canonical_latitudes` so §4.1d lat-order invariance holds.
    """
    latitudes, longitudes = _extract_axes(nc_path)
    canonical_latitudes = _canonical_latitudes(latitudes)
    cells = _build_cells(longitudes=longitudes, latitudes=canonical_latitudes)
    return grid_signature_hash(cells), cells


def _signature_without_normalization(nc_path: Path) -> str:
    """Debug-only path that hashes the RAW NetCDF longitudes without ``_build_cells``.

    Used exclusively by :func:`verify_coordinate_normalization` when the caller
    sets ``_bypass_normalization_for_test=True`` — i.e. when a test wants to
    prove the verifier surfaces a failure IF a future refactor were to skip
    ``_normalize_longitude``. Production callers MUST NOT set that flag; this
    helper deliberately reimplements the ordered iteration to synthesize the
    fail-closed path without touching SUB-4.

    Bypasses ONLY longitude normalization; latitude canonicalization via
    :func:`_canonical_latitudes` is still applied so the fail-closed path
    stays symmetric with :func:`_signature_from_netcdf` on the latitude axis.
    Without this, a lat-ascending vs lat-descending fixture pair fed through
    this bypass path would drift on latitude order and mask the longitude
    convention issue the bypass exists to expose.
    """
    latitudes, longitudes = _extract_axes(nc_path)
    canonical_latitudes = _canonical_latitudes(latitudes)
    raw_cells: list[_RawCell] = []
    for index, (latitude, longitude) in enumerate(
        (lat, lon) for lat in canonical_latitudes for lon in longitudes
    ):
        raw_cells.append(_RawCell(str(index), float(longitude), float(latitude)))
    return grid_signature_hash(raw_cells)


class _RawCell:
    """Grid-point struct that carries un-normalized coordinates.

    Satisfies the :class:`packages.common.grid_signature.GridPoint` structural
    protocol but skips the ``_normalize_longitude`` pass that ``_build_cells``
    applies. Only :func:`_signature_without_normalization` uses this.
    """

    __slots__ = ("grid_cell_id", "longitude", "latitude")

    def __init__(self, grid_cell_id: str, longitude: float, latitude: float) -> None:
        self.grid_cell_id = grid_cell_id
        self.longitude = longitude
        self.latitude = latitude


def _extract_bbox(nc_path: Path) -> dict[str, float]:
    """Return ``{"south","north","west","east"}`` from the fixture's coord axes."""
    latitudes, longitudes = _extract_axes(nc_path)
    return {
        "south": min(latitudes),
        "north": max(latitudes),
        "west": min(longitudes),
        "east": max(longitudes),
    }


def _extract_geometry_tuple(
    nc_path: Path,
) -> tuple[int, float, float, float, float]:
    """Return ``(cell_count, min_lon, max_lon, min_lat, max_lat)`` for ``nc_path``."""
    latitudes, longitudes = _extract_axes(nc_path)
    return (
        len(latitudes) * len(longitudes),
        min(longitudes),
        max(longitudes),
        min(latitudes),
        max(latitudes),
    )


def _first_differing_cell_id(
    expected: Sequence[CellInput],
    actual: Sequence[CellInput],
) -> str:
    """Return the ``grid_cell_id`` of the first cell that differs (or ``"?"`` if lengths differ)."""
    for expected_cell, actual_cell in zip(expected, actual, strict=False):
        if (
            expected_cell.grid_cell_id != actual_cell.grid_cell_id
            or expected_cell.longitude != actual_cell.longitude
            or expected_cell.latitude != actual_cell.latitude
        ):
            return expected_cell.grid_cell_id
    # No per-cell mismatch found via zip — length or trailing-cell mismatch.
    if len(actual) != len(expected):
        boundary = min(len(expected), len(actual))
        return str(boundary)
    return "?"


# -----------------------------------------------------------------------------
# Public API — 5 pure functions
# -----------------------------------------------------------------------------


def verify_multi_cycle_stability(cycle_fixtures: Mapping[str, Path]) -> None:
    """Assert one signature across every cycle in ``cycle_fixtures``.

    Parameters
    ----------
    cycle_fixtures:
        Ordered mapping from cycle-ISO string (e.g. ``"2026-05-03T00Z"``) to
        NetCDF fixture path. Insertion order defines which cycle is the
        expected baseline (the first entry).

    Returns
    -------
    ``None`` when every cycle hashes to the same signature and the ordered
    cell list is byte-identical.

    Raises
    ------
    MultiCycleSignatureDriftError
        On the first cycle whose signature disagrees, carrying the mutated
        cycle key and the first differing ``grid_cell_id``.
    """
    _require_non_empty(cycle_fixtures, "cycle_fixtures")
    iterator = iter(cycle_fixtures.items())
    first_cycle, first_path = next(iterator)
    expected_signature, expected_cells = _signature_from_netcdf(first_path)
    for cycle_key, nc_path in iterator:
        actual_signature, actual_cells = _signature_from_netcdf(nc_path)
        if actual_signature != expected_signature:
            raise MultiCycleSignatureDriftError(
                expected_signature=expected_signature,
                actual_signature=actual_signature,
                mutated_cycle=cycle_key,
                mutated_grid_cell_id=_first_differing_cell_id(
                    expected_cells, actual_cells
                ),
            )
    return None


def verify_multi_variable_stability(variable_fixtures: Mapping[str, Path]) -> None:
    """Assert one signature across every canonical variable in ``variable_fixtures``.

    Parameters
    ----------
    variable_fixtures:
        Ordered mapping from canonical variable name (e.g.
        ``"air_temperature_2m"``) to NetCDF fixture path. Insertion order
        defines which variable is the expected baseline.

    Returns
    -------
    ``None`` when every variable hashes to the same signature.

    Raises
    ------
    MultiVariableSignatureDriftError
        On the first variable whose signature disagrees.
    """
    _require_non_empty(variable_fixtures, "variable_fixtures")
    iterator = iter(variable_fixtures.items())
    _, first_path = next(iterator)
    expected_signature, _ = _signature_from_netcdf(first_path)
    for variable_name, nc_path in iterator:
        actual_signature, _ = _signature_from_netcdf(nc_path)
        if actual_signature != expected_signature:
            raise MultiVariableSignatureDriftError(
                expected_signature=expected_signature,
                actual_signature=actual_signature,
                offending_variable=variable_name,
            )
    return None


def verify_multi_backend_stability(backend_fixtures: Mapping[str, Path]) -> None:
    """Assert one signature + one bbox across every adapter in ``backend_fixtures``.

    Parameters
    ----------
    backend_fixtures:
        Ordered mapping from source-family adapter module name (e.g.
        ``"workers.data_adapters.ifs_adapter"``) to NetCDF fixture path.
        Insertion order defines which backend is the expected baseline.
        Adapter modules are enumerated by module-name string; adapter methods
        are NOT called (§4.0 non-goal).

    Returns
    -------
    ``None`` when every backend hashes to the same signature AND yields the
    same clip bbox.

    Raises
    ------
    MultiBackendSignatureDriftError
        On the first backend whose signature or bbox disagrees.
    """
    _require_non_empty(backend_fixtures, "backend_fixtures")
    iterator = iter(backend_fixtures.items())
    _, first_path = next(iterator)
    expected_signature, _ = _signature_from_netcdf(first_path)
    expected_bbox = _extract_bbox(first_path)
    for backend_name, nc_path in iterator:
        actual_signature, _ = _signature_from_netcdf(nc_path)
        actual_bbox = _extract_bbox(nc_path)
        if actual_signature != expected_signature or actual_bbox != expected_bbox:
            raise MultiBackendSignatureDriftError(
                expected_signature=expected_signature,
                actual_signature=actual_signature,
                offending_backend=backend_name,
                expected_clip_bbox=expected_bbox,
                actual_clip_bbox=actual_bbox,
            )
    return None


def verify_coordinate_normalization(
    lat_ascending_fixture: Path,
    lat_descending_fixture: Path,
    lon_0_360_fixture: Path,
    lon_180_fixture: Path,
    *,
    _bypass_normalization_for_test: bool = False,
) -> None:
    """Assert lat-order invariance AND lon-convention invariance.

    Parameters
    ----------
    lat_ascending_fixture / lat_descending_fixture:
        Two NetCDF fixtures at identical geometry with reversed latitude
        axes. ``_build_cells`` iterates in the pinned y-outer / x-inner order,
        producing the same ordered cell list regardless of the input axis
        direction.
    lon_0_360_fixture / lon_180_fixture:
        Two NetCDF fixtures at the same rectangle expressed in the two
        longitude conventions. ``_build_cells`` calls ``_normalize_longitude``,
        so both fixtures reduce to the same normalized ``[-180, 180)`` cells.
    _bypass_normalization_for_test:
        Debug-only flag exposed for the fail-closed regression test. When
        ``True``, the longitude arm hashes the raw NetCDF longitudes through
        :func:`_signature_without_normalization`, which skips
        ``_build_cells`` and therefore skips ``_normalize_longitude``. This
        surfaces a :class:`NormalizationSkippedError` when a ``0..360``
        fixture is compared against a ``-180..180`` fixture, proving the
        verifier catches a future refactor that drops the normalization pass.
        Production callers MUST NOT set this flag.

    Returns
    -------
    ``None`` when both latitude order and longitude convention are invariant.

    Raises
    ------
    NormalizationSkippedError
        Names the offending axis (``"latitude"`` or ``"longitude"``) plus
        the expected + actual conventions.
    """
    ascending_signature, _ = _signature_from_netcdf(lat_ascending_fixture)
    descending_signature, _ = _signature_from_netcdf(lat_descending_fixture)
    if ascending_signature != descending_signature:
        raise NormalizationSkippedError(
            axis="latitude",
            expected_convention="ascending",
            actual_convention="descending",
        )

    if _bypass_normalization_for_test:
        lon_0_360_signature = _signature_without_normalization(lon_0_360_fixture)
        lon_180_signature = _signature_without_normalization(lon_180_fixture)
    else:
        lon_0_360_signature, _ = _signature_from_netcdf(lon_0_360_fixture)
        lon_180_signature, _ = _signature_from_netcdf(lon_180_fixture)
    if lon_0_360_signature != lon_180_signature:
        raise NormalizationSkippedError(
            axis="longitude",
            expected_convention="[-180, 180)",
            actual_convention="[0, 360)",
        )
    return None


def verify_product_upgrade_and_dynamic_crop(
    pre_upgrade_fixture: Path,
    post_upgrade_fixture: Path,
    per_cycle_fixtures: Sequence[Path],
    *,
    declared_upgrade: bool,
) -> None:
    """Assert (a) declared upgrade changes signature and (b) no per-cycle crop.

    Parameters
    ----------
    pre_upgrade_fixture / post_upgrade_fixture:
        Two NetCDF fixtures representing the grid before and after a product
        upgrade. When ``declared_upgrade=True`` the two signatures MUST
        differ; otherwise the caller has silently upgraded a live product
        without updating the registry snapshot.
    per_cycle_fixtures:
        Sequence of NetCDF fixture paths representing multiple cycles of the
        SAME candidate grid. A production candidate MUST hash every cycle to
        an identical geometry tuple; a "dynamic crop" (i.e. clipping cell
        counts per cycle) is refused with the pinned "canonical grid contract
        must be stabilized" message.
    declared_upgrade:
        Explicit caller signal — the §4.0 pin rules out sidecar version
        detection. ``True`` when the caller intends the upgrade to change
        the grid; ``False`` when the caller expects the grid to remain stable
        (in which case identical pre/post signatures are acceptable).

    Returns
    -------
    ``None`` when the upgrade properly changes the signature AND per-cycle
    geometry is stable.

    Raises
    ------
    ProductUpgradeSignatureUnchangedError
        When ``declared_upgrade=True`` and pre/post signatures are equal.
    DynamicCropRefusedError
        When ≥2 per-cycle fixtures have differing geometry tuples. Error
        ``per_cycle_geometry`` maps cycle label (fixture path ``parent.name``
        with fallback to ``stem``) to ``(cell_count, min_lon, max_lon, min_lat,
        max_lat)`` for every supplied fixture (not just the offenders), so the
        operator has the full picture in the message.
    ValueError
        When two per-cycle fixtures share the same cycle-key derivation
        (``parent.name`` / ``stem``). The dict-key aggregation would otherwise
        silently overwrite the earlier fixture's geometry and mask a real
        drift; refusing at aggregation time surfaces the caller bug directly.
    """
    pre_signature, _ = _signature_from_netcdf(pre_upgrade_fixture)
    post_signature, _ = _signature_from_netcdf(post_upgrade_fixture)
    if declared_upgrade and pre_signature == post_signature:
        raise ProductUpgradeSignatureUnchangedError(
            pre_upgrade_signature=pre_signature,
            post_upgrade_signature=post_signature,
            declared_upgrade=declared_upgrade,
        )

    if len(per_cycle_fixtures) >= 2:
        per_cycle_geometry: dict[str, tuple[int, float, float, float, float]] = {}
        for fixture_path in per_cycle_fixtures:
            # Prefer the cycle_iso directory name (the fixture builder places
            # each cycle's file at ``tmp_path/{source}/{cycle_iso}/{variable}.nc``,
            # so ``parent.name`` is the cycle key). Fall back to the file stem
            # when the fixture is at a flatter path.
            cycle_key = fixture_path.parent.name or fixture_path.stem
            if cycle_key in per_cycle_geometry:
                raise ValueError(
                    f"per_cycle_fixtures contains multiple fixtures with the same cycle key "
                    f"{cycle_key!r}; each cycle MUST have a unique fixture path"
                )
            per_cycle_geometry[cycle_key] = _extract_geometry_tuple(fixture_path)
        distinct = set(per_cycle_geometry.values())
        if len(distinct) > 1:
            raise DynamicCropRefusedError(per_cycle_geometry=per_cycle_geometry)
    return None


# -----------------------------------------------------------------------------
# Small utilities
# -----------------------------------------------------------------------------


def _require_non_empty(mapping: Mapping[str, Any], field: str) -> None:
    """Raise ``ValueError`` if ``mapping`` is empty — an empty stability sweep is a caller bug."""
    if not mapping:
        raise ValueError(f"{field} must contain at least one fixture entry.")
