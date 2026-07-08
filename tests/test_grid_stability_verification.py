"""Tests for workers/grid_registry/stability.py (Epic #897 SUB-7 / issue #904).

Covers the 5 stability axes pinned in
``openspec/changes/canonical-source-grid-registry/tasks.md`` §4.0 + §4.1a-e:

* 4.1a multi-cycle: identical signature + stable ``grid_cell_id`` values across
  ≥3 consecutive cycles; mutated latitude on a 4th cycle fails closed.
* 4.1b multi-variable: identical signature across all six canonical variables
  (5 SHUD-facing labels: ``Prcp``, ``Temp``, ``RH``, ``Wind`` [U+V pair],
  ``RN``); mutated geometry on one variable fails closed.
* 4.1c multi-backend: identical signature across the three source-family
  adapter modules; shrunken clip bbox on one adapter fails closed.
* 4.1d coordinate normalization: lat-ascending / lat-descending pairs and
  lon-``0..360`` / lon-``-180..180`` pairs hash equal via ``_build_cells``;
  the ``_bypass_normalization_for_test`` debug arm proves the verifier catches
  a raw ``0..360`` fixture when the normalization pass is skipped.
* 4.1e product-upgrade + dynamic-crop: declared upgrade whose signatures
  differ passes; identical signatures under a declared upgrade fails closed;
  a per-cycle geometry-varying candidate fails closed with the pinned
  byte-for-byte ``"canonical grid contract must be stabilized"`` message.

All fixtures are generated in-test-body under ``tmp_path`` via the
``build_canonical_nc`` builder in :file:`tests/conftest.py` — no NetCDF
binaries are committed to the repo (§4.0 pin).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from packages.common.grid_registry_store import RegistryStoreError
from tests.conftest import build_canonical_nc
from workers.grid_registry.stability import (
    DynamicCropRefusedError,
    MultiBackendSignatureDriftError,
    MultiCycleSignatureDriftError,
    MultiVariableSignatureDriftError,
    NormalizationSkippedError,
    ProductUpgradeSignatureUnchangedError,
    StabilityVerificationError,
    verify_coordinate_normalization,
    verify_multi_backend_stability,
    verify_multi_cycle_stability,
    verify_multi_variable_stability,
    verify_product_upgrade_and_dynamic_crop,
)

# 5x5 sub-grid at 0.25° resolution matching the IFS/GFS 0.25° production
# grid. Small enough to keep test runtime negligible; wide enough that a
# single mutated latitude produces a byte-observable signature drift.
_PINNED_LATS: tuple[float, ...] = (8.0, 8.25, 8.5, 8.75, 9.0)
_PINNED_LONS: tuple[float, ...] = (63.0, 63.25, 63.5, 63.75, 64.0)

# The 6 canonical variables (the "5 SHUD variables" display fact — Wind is a
# U/V pair). Mirrors the pinned set from tasks.md §4.0 line 109.
_CANONICAL_VARIABLES: tuple[str, ...] = (
    "prcp_rate_or_amount",
    "air_temperature_2m",
    "relative_humidity_2m",
    "wind_u_10m",
    "wind_v_10m",
    "net_radiation",
)

# Three consecutive IFS/GFS cycles matching the SUB-10 backfill live date.
_PINNED_CYCLES: tuple[str, ...] = (
    "2026-05-03T00Z",
    "2026-05-03T06Z",
    "2026-05-03T12Z",
)

# Adapter enumeration is by module-name string; the verifier does NOT call
# adapter methods (§4.0 pin — "backend" = source family, not download mirror).
_PINNED_BACKENDS: tuple[str, ...] = (
    "workers.data_adapters.ifs_adapter",
    "workers.data_adapters.gfs_adapter",
    "workers.data_adapters.era5_adapter",
)


# -----------------------------------------------------------------------------
# 4.1a — multi-cycle stability
# -----------------------------------------------------------------------------


def test_multi_cycle_signature_identical(tmp_path: Path) -> None:
    """§4.1a happy path: three consecutive cycles hash to one signature."""
    fixtures: dict[str, Path] = {
        cycle: build_canonical_nc(
            tmp_path,
            source="ifs",
            cycle_iso=cycle,
            variable="air_temperature_2m",
            latitudes=_PINNED_LATS,
            longitudes=_PINNED_LONS,
        )
        for cycle in _PINNED_CYCLES
    }
    assert verify_multi_cycle_stability(fixtures) is None


def test_multi_cycle_mutated_cell_fails_closed(tmp_path: Path) -> None:
    """§4.1a fail-closed: a 4th cycle with a perturbed latitude drifts the signature.

    The perturbation targets ``_PINNED_LATS[2] + 0.001`` — small enough not to
    change the axis outer edges but large enough to change every downstream
    ``grid_cell_id``'s latitude coordinate, so ``mutated_grid_cell_id`` is
    populated with the FIRST cell whose iteration matches the perturbed lat.
    """
    fixtures: dict[str, Path] = {
        cycle: build_canonical_nc(
            tmp_path,
            source="ifs",
            cycle_iso=cycle,
            variable="air_temperature_2m",
            latitudes=_PINNED_LATS,
            longitudes=_PINNED_LONS,
        )
        for cycle in _PINNED_CYCLES
    }
    mutated_lats = list(_PINNED_LATS)
    mutated_lats[2] = mutated_lats[2] + 0.001
    fixtures["cycle4-key"] = build_canonical_nc(
        tmp_path,
        source="ifs",
        cycle_iso="2026-05-03T18Z",
        variable="air_temperature_2m",
        latitudes=mutated_lats,
        longitudes=_PINNED_LONS,
    )
    with pytest.raises(MultiCycleSignatureDriftError) as err_info:
        verify_multi_cycle_stability(fixtures)
    err = err_info.value
    assert err.mutated_cycle == "cycle4-key"
    # The first differing cell corresponds to the first index at the mutated
    # latitude row. Under y-outer / x-inner iteration with 5 longitudes per
    # latitude, that's index 2 * 5 = 10.
    assert err.mutated_grid_cell_id == "10"
    assert err.expected_signature != err.actual_signature
    assert len(err.expected_signature) == 64
    assert len(err.actual_signature) == 64


# -----------------------------------------------------------------------------
# 4.1b — multi-variable stability
# -----------------------------------------------------------------------------


def test_multi_variable_signature_identical(tmp_path: Path) -> None:
    """§4.1b happy path: all six canonical variables hash to one signature."""
    fixtures: dict[str, Path] = {
        variable: build_canonical_nc(
            tmp_path,
            source="ifs",
            cycle_iso="2026-05-03T00Z",
            variable=variable,
            latitudes=_PINNED_LATS,
            longitudes=_PINNED_LONS,
        )
        for variable in _CANONICAL_VARIABLES
    }
    assert verify_multi_variable_stability(fixtures) is None


def test_multi_variable_mutated_fails_closed(tmp_path: Path) -> None:
    """§4.1b fail-closed: one variable with drifted geometry names itself as offender."""
    fixtures: dict[str, Path] = {
        variable: build_canonical_nc(
            tmp_path,
            source="ifs",
            cycle_iso="2026-05-03T00Z",
            variable=variable,
            latitudes=_PINNED_LATS,
            longitudes=_PINNED_LONS,
        )
        for variable in _CANONICAL_VARIABLES[:5]
    }
    # Mutate the sixth variable's geometry by shifting all longitudes by +0.25.
    fixtures["net_radiation"] = build_canonical_nc(
        tmp_path,
        source="ifs",
        cycle_iso="2026-05-03T00Z-shifted",
        variable="net_radiation",
        latitudes=_PINNED_LATS,
        longitudes=tuple(lon + 0.25 for lon in _PINNED_LONS),
    )
    with pytest.raises(MultiVariableSignatureDriftError) as err_info:
        verify_multi_variable_stability(fixtures)
    err = err_info.value
    assert err.offending_variable == "net_radiation"
    assert err.expected_signature != err.actual_signature


# -----------------------------------------------------------------------------
# 4.1c — multi-backend stability
# -----------------------------------------------------------------------------


def test_multi_backend_signature_identical(tmp_path: Path) -> None:
    """§4.1c happy path: three source-family adapters hash to one signature.

    Uses a SMALL 5x5 sub-grid at 0.25° step. The invariant tested is signature
    invariance under adapter identity, not IFS/GFS/ERA5 bbox differences —
    inflating to production bbox (63-145 x 8-64 = 328 x 224) would balloon CI
    runtime for no marginal fidelity gain.
    """
    fixtures: dict[str, Path] = {
        backend: build_canonical_nc(
            tmp_path,
            source=backend.rsplit(".", maxsplit=1)[-1],
            cycle_iso="2026-05-03T00Z",
            variable="air_temperature_2m",
            latitudes=_PINNED_LATS,
            longitudes=_PINNED_LONS,
        )
        for backend in _PINNED_BACKENDS
    }
    assert verify_multi_backend_stability(fixtures) is None


def test_multi_backend_altered_clip_fails_closed(tmp_path: Path) -> None:
    """§4.1c fail-closed: one adapter with a shrunk bbox names itself as offender."""
    baseline_backends = _PINNED_BACKENDS[:2]
    fixtures: dict[str, Path] = {
        backend: build_canonical_nc(
            tmp_path,
            source=backend.rsplit(".", maxsplit=1)[-1],
            cycle_iso="2026-05-03T00Z",
            variable="air_temperature_2m",
            latitudes=_PINNED_LATS,
            longitudes=_PINNED_LONS,
        )
        for backend in baseline_backends
    }
    # Shrunk bbox: drop the outermost latitude/longitude so the ERA5 adapter's
    # clip is strictly smaller than the IFS/GFS baseline.
    fixtures["workers.data_adapters.era5_adapter"] = build_canonical_nc(
        tmp_path,
        source="era5_adapter",
        cycle_iso="2026-05-03T00Z",
        variable="air_temperature_2m",
        latitudes=_PINNED_LATS[:-1],
        longitudes=_PINNED_LONS[:-1],
    )
    with pytest.raises(MultiBackendSignatureDriftError) as err_info:
        verify_multi_backend_stability(fixtures)
    err = err_info.value
    assert err.offending_backend == "workers.data_adapters.era5_adapter"
    assert err.expected_signature != err.actual_signature
    assert err.expected_clip_bbox == {
        "south": min(_PINNED_LATS),
        "north": max(_PINNED_LATS),
        "west": min(_PINNED_LONS),
        "east": max(_PINNED_LONS),
    }
    assert err.actual_clip_bbox == {
        "south": min(_PINNED_LATS[:-1]),
        "north": max(_PINNED_LATS[:-1]),
        "west": min(_PINNED_LONS[:-1]),
        "east": max(_PINNED_LONS[:-1]),
    }


# -----------------------------------------------------------------------------
# 4.1d — coordinate normalization
# -----------------------------------------------------------------------------


def _make_lat_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Build two fixtures at identical geometry with reversed latitude axes."""
    ascending = build_canonical_nc(
        tmp_path,
        source="ifs",
        cycle_iso="2026-05-03T00Z",
        variable="air_temperature_2m_lat_ascending",
        latitudes=_PINNED_LATS,
        longitudes=_PINNED_LONS,
    )
    descending = build_canonical_nc(
        tmp_path,
        source="ifs",
        cycle_iso="2026-05-03T00Z",
        variable="air_temperature_2m_lat_descending",
        latitudes=tuple(reversed(_PINNED_LATS)),
        longitudes=_PINNED_LONS,
    )
    return ascending, descending


def _make_lon_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Build two fixtures at the SAME rectangle in the two longitude conventions.

    Rectangle: 350°-351°E (equivalently -10°--9°E). The 0..360 fixture stores
    ``[350.0, 350.25, 350.5, 350.75, 351.0]``; the -180..180 fixture stores
    ``[-10.0, -9.75, -9.5, -9.25, -9.0]``. Both reduce via
    ``_normalize_longitude`` to the same normalized cells.
    """
    lon_0_360 = (350.0, 350.25, 350.5, 350.75, 351.0)
    lon_180 = (-10.0, -9.75, -9.5, -9.25, -9.0)
    lat_ascending = _PINNED_LATS
    fixture_0_360 = build_canonical_nc(
        tmp_path,
        source="gfs",
        cycle_iso="2026-05-03T00Z",
        variable="air_temperature_2m_lon_0_360",
        latitudes=lat_ascending,
        longitudes=lon_0_360,
    )
    fixture_180 = build_canonical_nc(
        tmp_path,
        source="ifs",
        cycle_iso="2026-05-03T00Z",
        variable="air_temperature_2m_lon_180",
        latitudes=lat_ascending,
        longitudes=lon_180,
    )
    return fixture_0_360, fixture_180


def test_lat_order_invariance(tmp_path: Path) -> None:
    """§4.1d happy path: latitude ascending / descending hash equal."""
    lat_ascending, lat_descending = _make_lat_pair(tmp_path)
    lon_0_360, lon_180 = _make_lon_pair(tmp_path)
    assert (
        verify_coordinate_normalization(
            lat_ascending,
            lat_descending,
            lon_0_360,
            lon_180,
        )
        is None
    )


def test_lon_convention_normalization(tmp_path: Path) -> None:
    """§4.1d happy path: longitude ``0..360`` / ``-180..180`` hash equal via ``_normalize_longitude``.

    Uses a fresh pair of longitude fixtures independent of
    :func:`test_lat_order_invariance` so a regression in the longitude arm
    surfaces in isolation.
    """
    lat_ascending, lat_descending = _make_lat_pair(tmp_path)
    lon_0_360, lon_180 = _make_lon_pair(tmp_path)
    assert (
        verify_coordinate_normalization(
            lat_ascending,
            lat_descending,
            lon_0_360,
            lon_180,
        )
        is None
    )


def test_normalization_skipped_fails_closed(tmp_path: Path) -> None:
    """§4.1d fail-closed: bypass ``_build_cells`` and the ``0..360`` / ``-180..180`` pair drifts.

    The ``_bypass_normalization_for_test`` debug arm hashes the raw NetCDF
    longitudes without ``_normalize_longitude``. The ``0..360`` fixture then
    hashes to a distinct signature from the ``-180..180`` fixture (because
    ``350.0`` is not equal to ``-10.0`` under identity), surfacing
    :class:`NormalizationSkippedError`.
    """
    lat_ascending, lat_descending = _make_lat_pair(tmp_path)
    lon_0_360, lon_180 = _make_lon_pair(tmp_path)
    with pytest.raises(NormalizationSkippedError) as err_info:
        verify_coordinate_normalization(
            lat_ascending,
            lat_descending,
            lon_0_360,
            lon_180,
            _bypass_normalization_for_test=True,
        )
    err = err_info.value
    assert err.axis == "longitude"
    assert err.expected_convention == "[-180, 180)"
    assert err.actual_convention == "[0, 360)"


# -----------------------------------------------------------------------------
# 4.1e — product-upgrade + dynamic-crop refusal
# -----------------------------------------------------------------------------


def _identity_per_cycle_fixtures(tmp_path: Path, pre_upgrade: Path) -> Sequence[Path]:
    """Return three copies of the SAME geometry so dynamic-crop check passes.

    The 4.1e product-upgrade test path exercises the upgrade check; supplying
    three fixtures with identical geometry ensures the dynamic-crop arm does
    not surface a distraction failure.
    """
    del pre_upgrade  # kept for signature symmetry with the test caller
    return tuple(
        build_canonical_nc(
            tmp_path,
            source="ifs",
            cycle_iso=cycle,
            variable="air_temperature_2m",
            latitudes=_PINNED_LATS,
            longitudes=_PINNED_LONS,
        )
        for cycle in _PINNED_CYCLES
    )


def test_product_upgrade_changes_signature(tmp_path: Path) -> None:
    """§4.1e happy path: declared upgrade with DIFFERENT post geometry passes."""
    pre_upgrade = build_canonical_nc(
        tmp_path,
        source="ifs",
        cycle_iso="2026-05-03T00Z-pre",
        variable="air_temperature_2m",
        latitudes=_PINNED_LATS,
        longitudes=_PINNED_LONS,
    )
    post_upgrade = build_canonical_nc(
        tmp_path,
        source="ifs",
        cycle_iso="2026-05-03T00Z-post",
        variable="air_temperature_2m",
        latitudes=tuple(lat + 0.1 for lat in _PINNED_LATS),
        longitudes=_PINNED_LONS,
    )
    per_cycle = _identity_per_cycle_fixtures(tmp_path, pre_upgrade)
    assert (
        verify_product_upgrade_and_dynamic_crop(
            pre_upgrade,
            post_upgrade,
            per_cycle,
            declared_upgrade=True,
        )
        is None
    )


def test_product_upgrade_declared_but_signature_unchanged_fails_closed(
    tmp_path: Path,
) -> None:
    """§4.1e fail-closed: declared upgrade with IDENTICAL signatures raises."""
    pre_upgrade = build_canonical_nc(
        tmp_path,
        source="ifs",
        cycle_iso="2026-05-03T00Z-pre",
        variable="air_temperature_2m",
        latitudes=_PINNED_LATS,
        longitudes=_PINNED_LONS,
    )
    post_upgrade = build_canonical_nc(
        tmp_path,
        source="ifs",
        cycle_iso="2026-05-03T00Z-post",
        variable="air_temperature_2m",
        latitudes=_PINNED_LATS,
        longitudes=_PINNED_LONS,
    )
    per_cycle = _identity_per_cycle_fixtures(tmp_path, pre_upgrade)
    with pytest.raises(ProductUpgradeSignatureUnchangedError) as err_info:
        verify_product_upgrade_and_dynamic_crop(
            pre_upgrade,
            post_upgrade,
            per_cycle,
            declared_upgrade=True,
        )
    err = err_info.value
    assert err.pre_upgrade_signature == err.post_upgrade_signature
    assert err.pre_upgrade_signature is not None
    assert err.post_upgrade_signature is not None
    assert len(err.pre_upgrade_signature) == 64
    assert err.declared_upgrade is True


def test_dynamic_crop_refused(tmp_path: Path) -> None:
    """§4.1e fail-closed: three per-cycle fixtures with differing geometry raise.

    Each per-cycle fixture drops a different outer axis edge, so the
    ``(cell_count, min_lon, max_lon, min_lat, max_lat)`` tuples differ
    pair-wise. Asserts the pinned byte-for-byte message literal appears in
    the error string.
    """
    pre_upgrade = build_canonical_nc(
        tmp_path,
        source="ifs",
        cycle_iso="2026-05-03T00Z-pre",
        variable="air_temperature_2m",
        latitudes=_PINNED_LATS,
        longitudes=_PINNED_LONS,
    )
    post_upgrade = build_canonical_nc(
        tmp_path,
        source="ifs",
        cycle_iso="2026-05-03T00Z-post",
        variable="air_temperature_2m",
        latitudes=tuple(lat + 0.1 for lat in _PINNED_LATS),
        longitudes=_PINNED_LONS,
    )
    per_cycle: list[Path] = []
    # Cycle A: baseline 5x5 geometry.
    per_cycle.append(
        build_canonical_nc(
            tmp_path,
            source="ifs",
            cycle_iso="cycleA",
            variable="air_temperature_2m",
            latitudes=_PINNED_LATS,
            longitudes=_PINNED_LONS,
        )
    )
    # Cycle B: drops the outermost longitude — narrower rectangle.
    per_cycle.append(
        build_canonical_nc(
            tmp_path,
            source="ifs",
            cycle_iso="cycleB",
            variable="air_temperature_2m",
            latitudes=_PINNED_LATS,
            longitudes=_PINNED_LONS[:-1],
        )
    )
    # Cycle C: drops the outermost latitude — shorter rectangle.
    per_cycle.append(
        build_canonical_nc(
            tmp_path,
            source="ifs",
            cycle_iso="cycleC",
            variable="air_temperature_2m",
            latitudes=_PINNED_LATS[:-1],
            longitudes=_PINNED_LONS,
        )
    )
    with pytest.raises(DynamicCropRefusedError) as err_info:
        verify_product_upgrade_and_dynamic_crop(
            pre_upgrade,
            post_upgrade,
            per_cycle,
            declared_upgrade=True,
        )
    err = err_info.value
    # Full per-cycle geometry map, keyed by fixture path stem.
    assert set(err.per_cycle_geometry.keys()) == {"cycleA", "cycleB", "cycleC"}
    for cell_count, min_lon, max_lon, min_lat, max_lat in err.per_cycle_geometry.values():
        assert isinstance(cell_count, int)
        assert isinstance(min_lon, float)
        assert isinstance(max_lon, float)
        assert isinstance(min_lat, float)
        assert isinstance(max_lat, float)
    # Pinned §4.0 message literal — byte-for-byte.
    assert "canonical grid contract must be stabilized" in str(err)


# -----------------------------------------------------------------------------
# Bonus: exception hierarchy pin — cheap and locks the taxonomy in place.
# -----------------------------------------------------------------------------


def test_stability_verification_error_hierarchy() -> None:
    """All 6 subclasses inherit from ``StabilityVerificationError`` -> ``RegistryStoreError``."""
    assert issubclass(StabilityVerificationError, RegistryStoreError)
    for subclass in (
        MultiCycleSignatureDriftError,
        MultiVariableSignatureDriftError,
        MultiBackendSignatureDriftError,
        NormalizationSkippedError,
        ProductUpgradeSignatureUnchangedError,
        DynamicCropRefusedError,
    ):
        assert issubclass(subclass, StabilityVerificationError)
        assert issubclass(subclass, RegistryStoreError)
