"""Tests for the SUB-4 registry input-record and canonical_grid_key helper.

Covers issue #901 (Epic #897 SUB-4) Task 3.1a + 3.1c Evidence Floor:

* ``test_input_record_sources`` enumerates every field's derivation source.
* ``test_input_record_sidecar_schema_validated`` proves each documented
  malformed sidecar shape raises a ``GridSnapshotInputError`` subclass.
* ``test_input_record_fails_closed`` walks the 9-case rejection matrix
  (missing sidecar, wrong layout, wrong axis order, non-monotonic latitudes,
  NaN in longitudes, axis spacing mismatch, invalid bbox rectangle, invalid
  validity window, bbox vs axis outer-edge disagreement).
* ``test_input_record_matches_producer_grid_points`` proves per-cell parity
  with ``workers.forcing_producer.producer._grid_points_for_dataset`` on a
  shared 5x5 fixture, so the SUB-1 shared-signature invariant remains
  co-owned at the SUB-4 boundary.
* ``test_canonical_grid_key_derivation`` walks 7 sub-cases pinning the
  three-input contract (identity, bbox sensitivity, resolution sensitivity,
  signature sensitivity, no source_id in signature, exact expected key, and
  invalid-input rejection).
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import math
import os
import pathlib
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import psycopg2
import psycopg2.extras
import pytest
from psycopg2.extras import Json as _PgJson

from packages.common.canonical_grid_key import derive_canonical_grid_key
from packages.common.grid_registry_store import (
    CanonicalGridCell,
    CanonicalGridSnapshot,
    PsycopgGridRegistryStore,
    RegistryStoreError,
    RegistryUniqueViolationError,
)
from packages.common.grid_signature import grid_signature_hash
from packages.common.object_store import sha256_bytes
from workers.forcing_producer.producer import (
    CanonicalProduct,
    ForcingProducer,
    ForcingProducerConfig,
    _normalize_longitude,
)
from workers.grid_registry import (
    GridDriftDetectedError,
    LiveProducerSignatureMismatchError,
    RegistrationError,
    RegistrationInvariantError,
    register_snapshot,
)
from workers.grid_registry.input_record import (
    AxisOrderError,
    AxisSpacingMismatchError,
    BboxAxisDisagreementError,
    CellInput,
    GridSnapshotInputError,
    GridSnapshotInputRecord,
    InsufficientAxisPointsError,
    InvalidBboxRectangleError,
    InvalidValidityWindowError,
    MalformedJsonError,
    MissingGridDefinitionFieldError,
    MissingSidecarError,
    NaNAxisValueError,
    NonMonotonicLatitudeError,
    NonRectilinearLayoutError,
    SidecarSchemaError,
    read_input_record,
)
from workers.grid_registry.registry import _live_producer_signature
from workers.grid_registry.shared_binding_eligibility import (
    SharedBindingVerificationEvidence,
    evaluate_shared_binding_eligibility,
)

# -----------------------------------------------------------------------------
# Fixture fabricator
# -----------------------------------------------------------------------------

_DEFAULT_LONGITUDES = [63.0, 63.25, 63.5, 63.75, 64.0]
_DEFAULT_LATITUDES = [8.0, 8.25, 8.5, 8.75, 9.0]
_DEFAULT_CONVERTER_VERSION = "v1.2.3"
_DEFAULT_GRID_DEFINITION_URI = "canonical/IFS/grid/ifs_0p25/grid.json"


def _default_grid_payload(
    *,
    longitudes: list[float] | None = None,
    latitudes: list[float] | None = None,
    layout: Any = "rectilinear",
    axis_order: Any = None,
    include_layout: bool = True,
    include_axis_order: bool = True,
) -> dict[str, Any]:
    longitudes = list(_DEFAULT_LONGITUDES) if longitudes is None else list(longitudes)
    latitudes = list(_DEFAULT_LATITUDES) if latitudes is None else list(latitudes)
    payload: dict[str, Any] = {
        "schema_version": "nhms.grid_definition.v1",
        "grid_id": "ifs_0p25",
        "shape": [len(latitudes), len(longitudes)],
        "longitudes": longitudes,
        "latitudes": latitudes,
    }
    if include_layout:
        payload["layout"] = layout
    if include_axis_order:
        payload["axis_order"] = axis_order if axis_order is not None else ["latitude", "longitude"]
    return payload


def _default_sidecar_payload(
    *,
    longitudes: list[float] | None = None,
    latitudes: list[float] | None = None,
    valid_from: Any = "2026-07-06T00:00:00+00:00",
    valid_to: Any = None,
    override_bbox: dict[str, Any] | None = None,
    drop_valid_from: bool = False,
    drop_valid_to: bool = False,
    drop_download_bbox: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    longitudes = list(_DEFAULT_LONGITUDES) if longitudes is None else list(longitudes)
    latitudes = list(_DEFAULT_LATITUDES) if latitudes is None else list(latitudes)
    bbox: dict[str, Any] = {
        "south": min(latitudes),
        "north": max(latitudes),
        "west": min(longitudes),
        "east": max(longitudes),
    }
    if override_bbox is not None:
        bbox = dict(override_bbox)
    payload: dict[str, Any] = {}
    if not drop_valid_from:
        payload["valid_from"] = valid_from
    if not drop_valid_to:
        payload["valid_to"] = valid_to
    if not drop_download_bbox:
        payload["download_bbox"] = bbox
    if extra:
        payload.update(extra)
    return payload


def _write_fixture(
    tmp_path: pathlib.Path,
    *,
    grid_payload: dict[str, Any] | None = None,
    sidecar_payload: dict[str, Any] | None = None,
    write_sidecar: bool = True,
) -> tuple[pathlib.Path, pathlib.Path]:
    grid_json = tmp_path / "grid.json"
    sidecar = tmp_path / "grid_snapshot_metadata.json"
    grid_json.write_text(
        json.dumps(grid_payload if grid_payload is not None else _default_grid_payload()),
        encoding="utf-8",
    )
    if write_sidecar:
        sidecar.write_text(
            json.dumps(
                sidecar_payload if sidecar_payload is not None else _default_sidecar_payload()
            ),
            encoding="utf-8",
        )
    return grid_json, sidecar


def _stub_converter_resolver(source_id: str) -> str:  # noqa: ARG001 - matches signature
    return _DEFAULT_CONVERTER_VERSION


def _read_default_record(tmp_path: pathlib.Path) -> GridSnapshotInputRecord:
    grid_json, sidecar = _write_fixture(tmp_path)
    return read_input_record(
        "IFS",
        grid_json,
        sidecar,
        grid_definition_uri=_DEFAULT_GRID_DEFINITION_URI,
        expected_converter_version=_stub_converter_resolver,
    )


# -----------------------------------------------------------------------------
# Test 1: field-source enumeration
# -----------------------------------------------------------------------------


_FIELD_SOURCE_TABLE: tuple[tuple[str, str], ...] = (
    ("schema_version", "grid.json"),
    ("grid_id", "grid.json"),
    ("layout", "grid.json"),
    ("axis_order", "grid.json"),
    ("shape", "grid.json"),
    ("longitudes", "grid.json"),
    ("latitudes", "grid.json"),
    ("converter_version", "expected_converter_version"),
    ("native_resolution", "median-abs-diff(longitudes) == median-abs-diff(latitudes)"),
    ("latitude_order", "derived from latitudes"),
    ("flatten_order", "pinned to y_major_lat_then_lon"),
    ("valid_from", "sidecar"),
    ("valid_to", "sidecar"),
    ("download_bbox", "sidecar"),
    ("grid_cell_id", "str(index)"),
    ("canonical_ordinal", "index + 1"),
)


@pytest.mark.parametrize(("field", "expected_source"), _FIELD_SOURCE_TABLE)
def test_input_record_sources(
    tmp_path: pathlib.Path,
    field: str,
    expected_source: str,
) -> None:
    """Each of the 16 fields in the input record must derive from the pinned source."""
    record = _read_default_record(tmp_path)

    if field == "schema_version":
        assert record.schema_version == "nhms.grid_definition.v1"
    elif field == "grid_id":
        assert record.grid_id == "ifs_0p25"
    elif field == "layout":
        assert record.layout == "rectilinear"
    elif field == "axis_order":
        assert record.axis_order == ("latitude", "longitude")
    elif field == "shape":
        assert record.shape == (len(_DEFAULT_LATITUDES), len(_DEFAULT_LONGITUDES))
    elif field == "longitudes":
        assert record.longitudes == tuple(_DEFAULT_LONGITUDES)
    elif field == "latitudes":
        assert record.latitudes == tuple(_DEFAULT_LATITUDES)
    elif field == "converter_version":
        # Sourced from the injected resolver, which mirrors the production
        # workers.canonical_converter.converter.expected_converter_version(source_id).
        assert record.converter_version == _DEFAULT_CONVERTER_VERSION
    elif field == "native_resolution":
        assert record.native_resolution == pytest.approx(0.25)
    elif field == "latitude_order":
        assert record.latitude_order == "ascending"
    elif field == "flatten_order":
        assert record.flatten_order == "y_major_lat_then_lon"
    elif field == "valid_from":
        assert record.valid_from.tzinfo is not None
        assert record.valid_from.isoformat().startswith("2026-07-06T00:00:00")
    elif field == "valid_to":
        assert record.valid_to is None
    elif field == "download_bbox":
        assert set(record.download_bbox.keys()) == {"south", "north", "west", "east"}
        assert record.download_bbox["south"] == pytest.approx(min(_DEFAULT_LATITUDES))
    elif field == "grid_cell_id":
        assert record.cells[0].grid_cell_id == "0"
        assert record.cells[-1].grid_cell_id == str(len(record.cells) - 1)
    elif field == "canonical_ordinal":
        assert record.cells[0].canonical_ordinal == 1
        assert record.cells[-1].canonical_ordinal == len(record.cells)
    else:  # pragma: no cover - guards test-table drift
        raise AssertionError(f"unhandled field {field!r} (expected source {expected_source!r})")


# -----------------------------------------------------------------------------
# Test 2: sidecar schema validation
# -----------------------------------------------------------------------------


def test_input_record_sidecar_schema_validated_missing_valid_from(tmp_path: pathlib.Path) -> None:
    grid_json, sidecar = _write_fixture(
        tmp_path,
        sidecar_payload=_default_sidecar_payload(drop_valid_from=True),
    )
    with pytest.raises(GridSnapshotInputError) as excinfo:
        read_input_record(
            "IFS",
            grid_json,
            sidecar,
            grid_definition_uri=_DEFAULT_GRID_DEFINITION_URI,
            expected_converter_version=_stub_converter_resolver,
        )
    assert isinstance(excinfo.value, SidecarSchemaError)
    assert excinfo.value.field == "valid_from"


def test_input_record_sidecar_schema_validated_valid_from_epoch_int(
    tmp_path: pathlib.Path,
) -> None:
    grid_json, sidecar = _write_fixture(
        tmp_path,
        sidecar_payload=_default_sidecar_payload(valid_from=1720310400),
    )
    with pytest.raises(GridSnapshotInputError) as excinfo:
        read_input_record(
            "IFS",
            grid_json,
            sidecar,
            grid_definition_uri=_DEFAULT_GRID_DEFINITION_URI,
            expected_converter_version=_stub_converter_resolver,
        )
    assert isinstance(excinfo.value, SidecarSchemaError)
    assert excinfo.value.field == "valid_from"


def test_input_record_sidecar_schema_validated_download_bbox_missing_north(
    tmp_path: pathlib.Path,
) -> None:
    bbox = {"south": 8.0, "west": 63.0, "east": 64.0}  # no north
    grid_json, sidecar = _write_fixture(
        tmp_path,
        sidecar_payload=_default_sidecar_payload(override_bbox=bbox),
    )
    with pytest.raises(GridSnapshotInputError) as excinfo:
        read_input_record(
            "IFS",
            grid_json,
            sidecar,
            grid_definition_uri=_DEFAULT_GRID_DEFINITION_URI,
            expected_converter_version=_stub_converter_resolver,
        )
    assert isinstance(excinfo.value, SidecarSchemaError)
    assert excinfo.value.field == "north"


def test_input_record_sidecar_schema_validated_extra_top_level_key(
    tmp_path: pathlib.Path,
) -> None:
    grid_json, sidecar = _write_fixture(
        tmp_path,
        sidecar_payload=_default_sidecar_payload(extra={"unknown_key": "boom"}),
    )
    with pytest.raises(GridSnapshotInputError) as excinfo:
        read_input_record(
            "IFS",
            grid_json,
            sidecar,
            grid_definition_uri=_DEFAULT_GRID_DEFINITION_URI,
            expected_converter_version=_stub_converter_resolver,
        )
    assert isinstance(excinfo.value, SidecarSchemaError)
    assert excinfo.value.field == "unknown_key"


def test_input_record_sidecar_schema_validated_iso8601_without_timezone(
    tmp_path: pathlib.Path,
) -> None:
    grid_json, sidecar = _write_fixture(
        tmp_path,
        sidecar_payload=_default_sidecar_payload(valid_from="2026-07-06T12:00:00"),
    )
    with pytest.raises(GridSnapshotInputError) as excinfo:
        read_input_record(
            "IFS",
            grid_json,
            sidecar,
            grid_definition_uri=_DEFAULT_GRID_DEFINITION_URI,
            expected_converter_version=_stub_converter_resolver,
        )
    assert isinstance(excinfo.value, SidecarSchemaError)
    assert excinfo.value.field == "valid_from"


# -----------------------------------------------------------------------------
# Test 3: fail-closed matrix
# -----------------------------------------------------------------------------


def _build_missing_sidecar(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    grid_json, sidecar = _write_fixture(tmp_path, write_sidecar=False)
    return grid_json, sidecar


def _build_layout_missing(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    return _write_fixture(
        tmp_path,
        grid_payload=_default_grid_payload(include_layout=False),
    )


def _build_layout_curvilinear(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    return _write_fixture(
        tmp_path,
        grid_payload=_default_grid_payload(layout="curvilinear"),
    )


def _build_axis_order_wrong(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    return _write_fixture(
        tmp_path,
        grid_payload=_default_grid_payload(axis_order=["longitude", "latitude"]),
    )


def _build_latitudes_non_monotonic(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    latitudes = [10.0, 20.0, 15.0, 25.0, 30.0]
    return _write_fixture(
        tmp_path,
        grid_payload=_default_grid_payload(latitudes=latitudes),
        sidecar_payload=_default_sidecar_payload(latitudes=latitudes),
    )


def _build_nan_in_longitudes(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    longitudes = [63.0, float("nan"), 63.5, 63.75, 64.0]
    return _write_fixture(
        tmp_path,
        grid_payload=_default_grid_payload(longitudes=longitudes),
    )


def _build_axis_spacing_mismatch(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    # longitude spacing 0.25, latitude spacing 0.5
    longitudes = [63.0, 63.25, 63.5, 63.75, 64.0]
    latitudes = [8.0, 8.5, 9.0, 9.5, 10.0]
    return _write_fixture(
        tmp_path,
        grid_payload=_default_grid_payload(longitudes=longitudes, latitudes=latitudes),
        sidecar_payload=_default_sidecar_payload(longitudes=longitudes, latitudes=latitudes),
    )


def _build_bbox_south_greater_than_north(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    bbox = {"south": 10.0, "north": 5.0, "west": 63.0, "east": 64.0}
    return _write_fixture(
        tmp_path,
        sidecar_payload=_default_sidecar_payload(override_bbox=bbox),
    )


def _build_valid_to_before_valid_from(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    return _write_fixture(
        tmp_path,
        sidecar_payload=_default_sidecar_payload(
            valid_from="2026-07-06T12:00:00+00:00",
            valid_to="2026-07-06T06:00:00+00:00",
        ),
    )


def _build_bbox_disagrees_with_axis(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    # grid.json latitudes start at 8; sidecar bbox says south=0.
    bbox = {"south": 0.0, "north": 9.0, "west": 63.0, "east": 64.0}
    return _write_fixture(
        tmp_path,
        sidecar_payload=_default_sidecar_payload(override_bbox=bbox),
    )


def _build_latitudes_too_short(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(C3) len(latitudes) < 2 must surface as InsufficientAxisPointsError."""
    grid_payload = _default_grid_payload(latitudes=[42.0])
    return _write_fixture(
        tmp_path,
        grid_payload=grid_payload,
        sidecar_payload=_default_sidecar_payload(latitudes=[42.0]),
    )


def _build_longitudes_too_short(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(C3) len(longitudes) < 2 must surface as InsufficientAxisPointsError.

    Uses a latitudes fixture of 2 values so `_derive_latitude_order` succeeds
    and the failure surfaces at `_derive_native_resolution`.
    """
    grid_payload = _default_grid_payload(
        longitudes=[42.0],
        latitudes=[8.0, 8.25],
    )
    return _write_fixture(
        tmp_path,
        grid_payload=grid_payload,
        sidecar_payload=_default_sidecar_payload(
            longitudes=[42.0],
            latitudes=[8.0, 8.25],
        ),
    )


def _build_valid_to_equals_valid_from(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(C5) valid_to == valid_from must fail closed (zero-length window)."""
    same = "2026-07-06T12:00:00+00:00"
    return _write_fixture(
        tmp_path,
        sidecar_payload=_default_sidecar_payload(valid_from=same, valid_to=same),
    )


def _build_shape_mismatch(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(C6) declared shape != axis lengths must fail closed at layout invariant."""
    grid_payload = _default_grid_payload()
    grid_payload["shape"] = [3, 3]  # actual axis lengths are (5, 5)
    return _write_fixture(tmp_path, grid_payload=grid_payload)


def _build_nan_in_latitudes(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(C6) symmetric with nan_in_longitudes."""
    latitudes = [8.0, float("nan"), 8.5, 8.75, 9.0]
    return _write_fixture(
        tmp_path,
        grid_payload=_default_grid_payload(latitudes=latitudes),
    )


def _build_grid_id_missing(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(C8) grid.json without a `grid_id` key must fail closed with a narrow subclass."""
    grid_payload = _default_grid_payload()
    grid_payload.pop("grid_id", None)
    return _write_fixture(tmp_path, grid_payload=grid_payload)


def _build_schema_version_missing(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(C8) grid.json without a `schema_version` key must fail closed with a narrow subclass."""
    grid_payload = _default_grid_payload()
    grid_payload.pop("schema_version", None)
    return _write_fixture(tmp_path, grid_payload=grid_payload)


def _build_grid_id_empty_string(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(C8 residual) grid.json with `grid_id=""` must fail closed with a narrow subclass.

    The earlier fix only guarded `is None`; an explicit empty string slid through
    the `.get(..., "")` fallback and produced a record with empty identity fields.
    """
    grid_payload = _default_grid_payload()
    grid_payload["grid_id"] = ""
    return _write_fixture(tmp_path, grid_payload=grid_payload)


def _build_grid_id_whitespace(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(C8 residual) grid.json with `grid_id="   "` (whitespace only) must fail closed."""
    grid_payload = _default_grid_payload()
    grid_payload["grid_id"] = "   "
    return _write_fixture(tmp_path, grid_payload=grid_payload)


def _build_schema_version_empty_string(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(C8 residual) grid.json with `schema_version=""` must fail closed with a narrow subclass."""
    grid_payload = _default_grid_payload()
    grid_payload["schema_version"] = ""
    return _write_fixture(tmp_path, grid_payload=grid_payload)


_FixtureBuilder = Callable[[pathlib.Path], tuple[pathlib.Path, pathlib.Path]]
_FailClosedCase = tuple[str, _FixtureBuilder, type[GridSnapshotInputError], str]

_FAIL_CLOSED_CASES: tuple[_FailClosedCase, ...] = (
    ("missing_sidecar", _build_missing_sidecar, MissingSidecarError, "sidecar_path"),
    ("layout_missing", _build_layout_missing, NonRectilinearLayoutError, "layout"),
    ("layout_wrong", _build_layout_curvilinear, NonRectilinearLayoutError, "layout"),
    ("axis_order_wrong", _build_axis_order_wrong, AxisOrderError, "axis_order"),
    (
        "latitudes_non_monotonic",
        _build_latitudes_non_monotonic,
        NonMonotonicLatitudeError,
        "latitudes",
    ),
    ("nan_in_longitudes", _build_nan_in_longitudes, NaNAxisValueError, "longitudes"),
    (
        "axis_spacing_mismatch",
        _build_axis_spacing_mismatch,
        AxisSpacingMismatchError,
        "native_resolution",
    ),
    (
        "bbox_south_greater_than_north",
        _build_bbox_south_greater_than_north,
        InvalidBboxRectangleError,
        "download_bbox",
    ),
    (
        "valid_to_before_valid_from",
        _build_valid_to_before_valid_from,
        InvalidValidityWindowError,
        "valid_to",
    ),
    (
        "bbox_disagrees_with_axis_outer_edges",
        _build_bbox_disagrees_with_axis,
        BboxAxisDisagreementError,
        "south",
    ),
    # C3: len < 2 axis mislabel — surface distinct exception subclass.
    (
        "latitudes_too_short",
        _build_latitudes_too_short,
        InsufficientAxisPointsError,
        "latitudes",
    ),
    (
        "longitudes_too_short",
        _build_longitudes_too_short,
        InsufficientAxisPointsError,
        "longitudes",
    ),
    # C5: zero-length validity window (valid_to == valid_from).
    (
        "valid_to_equals_valid_from",
        _build_valid_to_equals_valid_from,
        InvalidValidityWindowError,
        "valid_to",
    ),
    # C6: fail-closed matrix gaps — shape mismatch + NaN-in-latitudes.
    (
        "shape_mismatch",
        _build_shape_mismatch,
        NonRectilinearLayoutError,
        "shape",
    ),
    (
        "nan_in_latitudes",
        _build_nan_in_latitudes,
        NaNAxisValueError,
        "latitudes",
    ),
    # C8: silent-empty coercion of grid_id / schema_version.
    (
        "grid_id_missing",
        _build_grid_id_missing,
        MissingGridDefinitionFieldError,
        "grid_id",
    ),
    (
        "grid_id_empty_string",
        _build_grid_id_empty_string,
        MissingGridDefinitionFieldError,
        "grid_id",
    ),
    (
        "grid_id_whitespace",
        _build_grid_id_whitespace,
        MissingGridDefinitionFieldError,
        "grid_id",
    ),
    (
        "schema_version_missing",
        _build_schema_version_missing,
        MissingGridDefinitionFieldError,
        "schema_version",
    ),
    (
        "schema_version_empty_string",
        _build_schema_version_empty_string,
        MissingGridDefinitionFieldError,
        "schema_version",
    ),
)


@pytest.mark.parametrize(
    ("case", "builder", "expected_exception", "expected_field"),
    _FAIL_CLOSED_CASES,
    ids=[case[0] for case in _FAIL_CLOSED_CASES],
)
def test_input_record_fails_closed(
    tmp_path: pathlib.Path,
    case: str,  # noqa: ARG001 - carried by the parametrize id
    builder: Callable[[pathlib.Path], tuple[pathlib.Path, pathlib.Path]],
    expected_exception: type[GridSnapshotInputError],
    expected_field: str,
) -> None:
    grid_json, sidecar = builder(tmp_path)
    with pytest.raises(GridSnapshotInputError) as excinfo:
        read_input_record(
            "IFS",
            grid_json,
            sidecar,
            grid_definition_uri=_DEFAULT_GRID_DEFINITION_URI,
            expected_converter_version=_stub_converter_resolver,
        )
    assert isinstance(excinfo.value, expected_exception), (
        f"expected {expected_exception.__name__}; got {type(excinfo.value).__name__}"
    )
    assert excinfo.value.field == expected_field, (
        f"expected field {expected_field!r}; got {excinfo.value.field!r}"
    )


# -----------------------------------------------------------------------------
# Test 4: parity with producer._grid_points_for_dataset
# -----------------------------------------------------------------------------


def _make_producer(tmp_path: pathlib.Path) -> ForcingProducer:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ForcingProducer(
        config=ForcingProducerConfig(
            workspace_root=workspace,
            object_store_root=workspace,
        ),
        repository=None,
    )


def _make_canonical_product(grid_definition_uri: str) -> CanonicalProduct:
    from datetime import UTC
    from datetime import datetime as _dt

    return CanonicalProduct(
        canonical_product_id="test-product",
        source_id="IFS",
        cycle_time=_dt(2026, 7, 6, tzinfo=UTC),
        valid_time=_dt(2026, 7, 6, tzinfo=UTC),
        variable="Prcp",
        unit="mm/day",
        grid_id="ifs_0p25",
        object_uri="s3://ignored/product.nc",
        checksum="0" * 64,
        grid_definition_uri=grid_definition_uri,
    )


def test_input_record_matches_producer_grid_points(tmp_path: pathlib.Path) -> None:
    """The input-record cell list must equal producer._grid_points_for_dataset
    output byte-for-byte on a shared 5x5 fixture — this pins the SUB-1 shared-
    signature invariant at the SUB-4 boundary."""
    longitudes = [63.0, 63.25, 63.5, 63.75, 64.0]
    latitudes = [8.0, 8.25, 8.5, 8.75, 9.0]
    y_count = len(latitudes)
    x_count = len(longitudes)

    workspace = tmp_path / "producer"
    workspace.mkdir(exist_ok=True)
    grid_definition_key = "canonical/IFS/grid/ifs_0p25/grid.json"
    grid_definition_path = workspace / grid_definition_key
    grid_definition_path.parent.mkdir(parents=True, exist_ok=True)
    grid_definition_bytes = json.dumps(
        {
            "schema_version": "nhms.grid_definition.v1",
            "grid_id": "ifs_0p25",
            "layout": "rectilinear",
            "axis_order": ["latitude", "longitude"],
            "shape": [y_count, x_count],
            "longitudes": longitudes,
            "latitudes": latitudes,
        }
    ).encode("utf-8")
    grid_definition_path.write_bytes(grid_definition_bytes)

    # Write matching input-record fixture in its own tmpdir.
    input_dir = tmp_path / "input"
    input_dir.mkdir(exist_ok=True)
    grid_json, sidecar = _write_fixture(
        input_dir,
        grid_payload=_default_grid_payload(longitudes=longitudes, latitudes=latitudes),
        sidecar_payload=_default_sidecar_payload(longitudes=longitudes, latitudes=latitudes),
    )
    record = read_input_record(
        "IFS",
        grid_json,
        sidecar,
        grid_definition_uri=_DEFAULT_GRID_DEFINITION_URI,
        expected_converter_version=_stub_converter_resolver,
    )

    # Build the producer and call _grid_points_for_dataset. When
    # grid_definition_uri is set (producer.py:1389), _grid_points_for_dataset
    # delegates to _grid_points_from_definition, which hits producer.py:1470-1479
    # — the exact block the input record mirrors. dataset / data_array / shape
    # remain unused because the definition path returns first.
    producer = ForcingProducer(
        config=ForcingProducerConfig(
            workspace_root=workspace,
            object_store_root=workspace,
        ),
        repository=None,
    )
    product = _make_canonical_product(grid_definition_uri=grid_definition_key)
    grid_points = producer._grid_points_for_dataset(
        product,
        dataset=None,
        data_array=None,
        shape=(y_count, x_count),
        expected_count=y_count * x_count,
    )
    assert grid_points is not None
    assert len(grid_points) == y_count * x_count == len(record.cells)

    for index, (producer_point, input_cell) in enumerate(zip(grid_points, record.cells, strict=True)):
        assert input_cell.grid_cell_id == producer_point.grid_cell_id == str(index)
        assert input_cell.longitude == producer_point.longitude
        assert input_cell.latitude == producer_point.latitude
        # Both must have already applied _normalize_longitude.
        assert input_cell.longitude == _normalize_longitude(input_cell.longitude)
        assert input_cell.canonical_ordinal == index + 1


# -----------------------------------------------------------------------------
# Test 5: canonical_grid_key derivation
# -----------------------------------------------------------------------------


_BACKFILL_SIGNATURE = "6c008901b8b7" + "0" * 52
_BACKFILL_BBOX = {"south": 8.0, "north": 64.0, "west": 63.0, "east": 145.0}
_BACKFILL_RESOLUTION = 0.25

# Byte-for-byte pinned expected key for the (signature, bbox, resolution) triple
# above. Computed once from the pinned implementation; hard-coded here so a
# coordinated future refactor of both `_reference_canonical_grid_key` and
# `derive_canonical_grid_key` cannot silently drift the encoding away from the
# Task 3.1c contract (evidence f: "byte-for-byte" pin).
_EXPECTED_BACKFILL_KEY = "2c6f5186d4a738547cab4760e9fc8ea900259d5394c71182043e2d99ca647244"


def _reference_canonical_grid_key(
    grid_signature: str,
    bbox: dict[str, float],
    native_resolution: float,
) -> str:
    """Inline reference algorithm mirroring the pinned Task 3.1c contract.

    Kept as ALGORITHM DOCUMENTATION for the encoding pinned by Task 3.1c; no
    longer used as the equality anchor (see `_EXPECTED_BACKFILL_KEY`).
    """
    payload = json.dumps(
        {
            "grid_signature": grid_signature,
            "bbox": {
                "south": bbox["south"],
                "north": bbox["north"],
                "west": bbox["west"],
                "east": bbox["east"],
            },
            "native_resolution": f"{native_resolution:.12f}",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return sha256_bytes(payload)


def test_canonical_grid_key_same_inputs_same_key() -> None:
    """(a) identical three-input tuples → identical key."""
    key_1 = derive_canonical_grid_key(
        _BACKFILL_SIGNATURE, dict(_BACKFILL_BBOX), _BACKFILL_RESOLUTION
    )
    key_2 = derive_canonical_grid_key(
        _BACKFILL_SIGNATURE, dict(_BACKFILL_BBOX), _BACKFILL_RESOLUTION
    )
    assert key_1 == key_2
    assert len(key_1) == 64
    assert set(key_1).issubset(set("0123456789abcdef"))


def test_canonical_grid_key_int_bbox_equals_float_bbox() -> None:
    """(C1) int bbox values must produce the same key as float bbox values.

    `json.dumps(8)` and `json.dumps(8.0)` differ (`"8"` vs `"8.0"`), so if
    `_validate_bbox` accepts `int` without coercing to `float`, the same-shape
    bbox produces different `canonical_grid_key`s.
    """
    int_bbox = {"south": 8, "north": 64, "west": 63, "east": 145}
    float_bbox = {"south": 8.0, "north": 64.0, "west": 63.0, "east": 145.0}
    key_int = derive_canonical_grid_key(_BACKFILL_SIGNATURE, int_bbox, 0.25)
    key_float = derive_canonical_grid_key(_BACKFILL_SIGNATURE, float_bbox, 0.25)
    assert key_int == key_float
    # Anchor to the pinned expected value so a future refactor that reverts the
    # coercion silently is caught here.
    assert key_int == _EXPECTED_BACKFILL_KEY


def test_canonical_grid_key_different_bbox_different_key() -> None:
    """(b) same signature + different bbox → different keys."""
    bbox_a = dict(_BACKFILL_BBOX)
    bbox_b = dict(_BACKFILL_BBOX)
    bbox_b["north"] = 63.999
    key_a = derive_canonical_grid_key(_BACKFILL_SIGNATURE, bbox_a, _BACKFILL_RESOLUTION)
    key_b = derive_canonical_grid_key(_BACKFILL_SIGNATURE, bbox_b, _BACKFILL_RESOLUTION)
    assert key_a != key_b


def test_canonical_grid_key_different_native_resolution_different_key() -> None:
    """(c) same signature + different native_resolution → different keys."""
    key_a = derive_canonical_grid_key(
        _BACKFILL_SIGNATURE, dict(_BACKFILL_BBOX), _BACKFILL_RESOLUTION
    )
    key_b = derive_canonical_grid_key(
        _BACKFILL_SIGNATURE, dict(_BACKFILL_BBOX), _BACKFILL_RESOLUTION + 0.01
    )
    assert key_a != key_b


def test_canonical_grid_key_different_signature_different_key() -> None:
    """(d) different signature → different keys, regardless of matching bbox / resolution."""
    other_signature = "a" * 64
    key_a = derive_canonical_grid_key(
        _BACKFILL_SIGNATURE, dict(_BACKFILL_BBOX), _BACKFILL_RESOLUTION
    )
    key_b = derive_canonical_grid_key(
        other_signature, dict(_BACKFILL_BBOX), _BACKFILL_RESOLUTION
    )
    assert key_a != key_b


def test_canonical_grid_key_source_id_not_in_signature() -> None:
    """(e) The pinned function signature has exactly three parameters — no source_id."""
    signature = inspect.signature(derive_canonical_grid_key)
    parameter_names = tuple(signature.parameters.keys())
    assert parameter_names == ("grid_signature", "bbox", "native_resolution")
    assert "source_id" not in parameter_names


def test_canonical_grid_key_exact_expected_for_backfill_inputs() -> None:
    """(f) Byte-for-byte pinned encoding: the exact expected 64-char lowercase hex.

    The signature ``6c008901b8b7…`` is synthetic (extended to 64 chars with a
    deterministic suffix) — this test pins the encoding, not the live IFS/GFS
    backfill key (SUB-4 does not run backfill).

    The expected value is a hard-coded module-level constant (`_EXPECTED_BACKFILL_KEY`)
    — NOT `_reference_canonical_grid_key(...)` — so a coordinated future refactor
    of both `_reference_canonical_grid_key` and `derive_canonical_grid_key` cannot
    silently drift the encoding away from the Task 3.1c contract.
    """
    actual = derive_canonical_grid_key(
        _BACKFILL_SIGNATURE, dict(_BACKFILL_BBOX), _BACKFILL_RESOLUTION
    )
    assert actual == _EXPECTED_BACKFILL_KEY
    # Sanity: the expected key is 64-char lowercase hex.
    assert len(_EXPECTED_BACKFILL_KEY) == 64
    assert set(_EXPECTED_BACKFILL_KEY).issubset(set("0123456789abcdef"))
    # Documentation cross-check: the algorithm reference produces the same key.
    reference = _reference_canonical_grid_key(
        _BACKFILL_SIGNATURE, dict(_BACKFILL_BBOX), _BACKFILL_RESOLUTION
    )
    assert reference == _EXPECTED_BACKFILL_KEY


_INVALID_INPUT_CASES: tuple[tuple[str, dict[str, Any], str], ...] = (
    (
        "native_resolution_nan",
        {
            "grid_signature": _BACKFILL_SIGNATURE,
            "bbox": dict(_BACKFILL_BBOX),
            "native_resolution": float("nan"),
        },
        "native_resolution",
    ),
    (
        "native_resolution_inf",
        {
            "grid_signature": _BACKFILL_SIGNATURE,
            "bbox": dict(_BACKFILL_BBOX),
            "native_resolution": float("inf"),
        },
        "native_resolution",
    ),
    (
        "bbox_missing_north",
        {
            "grid_signature": _BACKFILL_SIGNATURE,
            "bbox": {"south": 8.0, "west": 63.0, "east": 145.0},
            "native_resolution": _BACKFILL_RESOLUTION,
        },
        "north",
    ),
    (
        "bbox_extra_key",
        {
            "grid_signature": _BACKFILL_SIGNATURE,
            "bbox": {**_BACKFILL_BBOX, "center": 30.0},
            "native_resolution": _BACKFILL_RESOLUTION,
        },
        "center",
    ),
    (
        "bbox_non_float_value",
        {
            "grid_signature": _BACKFILL_SIGNATURE,
            "bbox": {"south": "8.0", "north": 64.0, "west": 63.0, "east": 145.0},
            "native_resolution": _BACKFILL_RESOLUTION,
        },
        "south",
    ),
    (
        "grid_signature_wrong_length",
        {
            "grid_signature": "a" * 63,
            "bbox": dict(_BACKFILL_BBOX),
            "native_resolution": _BACKFILL_RESOLUTION,
        },
        "grid_signature",
    ),
    (
        "grid_signature_non_hex",
        {
            "grid_signature": "g" * 64,
            "bbox": dict(_BACKFILL_BBOX),
            "native_resolution": _BACKFILL_RESOLUTION,
        },
        "grid_signature",
    ),
)


@pytest.mark.parametrize(
    ("case", "kwargs", "expected_field_substring"),
    _INVALID_INPUT_CASES,
    ids=[case[0] for case in _INVALID_INPUT_CASES],
)
def test_canonical_grid_key_rejects_invalid_inputs(
    case: str,  # noqa: ARG001
    kwargs: dict[str, Any],
    expected_field_substring: str,
) -> None:
    """(g) Every documented invalid input raises ValueError naming the offending field."""
    with pytest.raises(ValueError) as excinfo:
        derive_canonical_grid_key(
            grid_signature=kwargs["grid_signature"],
            bbox=kwargs["bbox"],
            native_resolution=kwargs["native_resolution"],
        )
    assert expected_field_substring in str(excinfo.value), (
        f"expected substring {expected_field_substring!r} in error message; "
        f"got {excinfo.value!s}"
    )


# -----------------------------------------------------------------------------
# Cell / record dataclass shape assertions
# -----------------------------------------------------------------------------


def test_cell_input_and_record_are_frozen(tmp_path: pathlib.Path) -> None:
    """Guard rail: the input record and its per-cell rows are frozen dataclasses so
    the Task 3.1b writer cannot silently mutate the record between read and write."""
    record = _read_default_record(tmp_path)
    with pytest.raises(Exception):
        record.native_resolution = 999.0  # type: ignore[misc]
    cell = record.cells[0]
    with pytest.raises(Exception):
        cell.longitude = 999.0  # type: ignore[misc]
    assert isinstance(cell, CellInput)


# Simple safety net: the fabricator produces a payload the reader accepts.
def test_default_fixture_reads_without_error(tmp_path: pathlib.Path) -> None:
    record = _read_default_record(tmp_path)
    assert record.layout == "rectilinear"
    assert not math.isnan(record.native_resolution)
    assert isinstance(record.download_bbox, Mapping)


# -----------------------------------------------------------------------------
# C2: JSON decode leak - malformed JSON must surface as GridSnapshotInputError
# -----------------------------------------------------------------------------


_VALID_SIDECAR_BODY = (
    b'"valid_from": "2026-07-06T00:00:00+00:00", '
    b'"valid_to": null, '
    b'"download_bbox": {"south": 8.0, "north": 9.0, "west": 63.0, "east": 64.0}'
)

_MALFORMED_SIDECAR_CASES: tuple[tuple[str, bytes], ...] = (
    (
        "bom_prefixed_sidecar",
        b"\xef\xbb\xbf"
        + json.dumps(
            {
                "valid_from": "2026-07-06T00:00:00+00:00",
                "valid_to": None,
                "download_bbox": {"south": 8.0, "north": 9.0, "west": 63.0, "east": 64.0},
            }
        ).encode("utf-8"),
    ),
    (
        "truncated_sidecar",
        b'{"valid_from": "2026-07-06T00:00:00+00:00", '
        b'"valid_to": null, "download_bbox": {"south": 8.0',
    ),
    (
        "trailing_comma_sidecar",
        b"{" + _VALID_SIDECAR_BODY + b",}",
    ),
)


@pytest.mark.parametrize(
    ("case", "sidecar_bytes"),
    _MALFORMED_SIDECAR_CASES,
    ids=[case[0] for case in _MALFORMED_SIDECAR_CASES],
)
def test_input_record_malformed_json_bytes(
    tmp_path: pathlib.Path,
    case: str,  # noqa: ARG001 - carried by parametrize id
    sidecar_bytes: bytes,
) -> None:
    """(C2) Malformed sidecar JSON must raise MalformedJsonError (a
    GridSnapshotInputError subclass) so downstream `except GridSnapshotInputError`
    handlers do not crash on `json.JSONDecodeError` bubbling up unwrapped."""
    grid_json = tmp_path / "grid.json"
    sidecar = tmp_path / "grid_snapshot_metadata.json"
    grid_json.write_text(json.dumps(_default_grid_payload()), encoding="utf-8")
    sidecar.write_bytes(sidecar_bytes)
    with pytest.raises(GridSnapshotInputError) as excinfo:
        read_input_record(
            "IFS",
            grid_json,
            sidecar,
            grid_definition_uri=_DEFAULT_GRID_DEFINITION_URI,
            expected_converter_version=_stub_converter_resolver,
        )
    assert isinstance(excinfo.value, MalformedJsonError)
    assert excinfo.value.field == "sidecar"


def test_input_record_malformed_grid_json_bytes(tmp_path: pathlib.Path) -> None:
    """(C2) Malformed grid.json bytes must raise MalformedJsonError with
    field='grid.json'."""
    grid_json = tmp_path / "grid.json"
    sidecar = tmp_path / "grid_snapshot_metadata.json"
    grid_json.write_bytes(b'{"schema_version": "nhms.grid_definition.v1",')  # truncated
    sidecar.write_text(json.dumps(_default_sidecar_payload()), encoding="utf-8")
    with pytest.raises(GridSnapshotInputError) as excinfo:
        read_input_record(
            "IFS",
            grid_json,
            sidecar,
            grid_definition_uri=_DEFAULT_GRID_DEFINITION_URI,
            expected_converter_version=_stub_converter_resolver,
        )
    assert isinstance(excinfo.value, MalformedJsonError)
    assert excinfo.value.field == "grid.json"


# -----------------------------------------------------------------------------
# C4: TOCTOU-safe capture of grid_definition_uri / grid_definition_checksum
# -----------------------------------------------------------------------------


def test_input_record_captures_grid_definition_checksum(tmp_path: pathlib.Path) -> None:
    """(C4) The record's `grid_definition_checksum` must equal the SHA-256 of the
    exact grid.json bytes read at input-record time, and `grid_definition_uri`
    must equal the caller-supplied URI."""
    grid_json, sidecar = _write_fixture(tmp_path)
    expected_checksum = sha256_bytes(grid_json.read_bytes())
    record = read_input_record(
        "IFS",
        grid_json,
        sidecar,
        grid_definition_uri=_DEFAULT_GRID_DEFINITION_URI,
        expected_converter_version=_stub_converter_resolver,
    )
    assert record.grid_definition_uri == _DEFAULT_GRID_DEFINITION_URI
    assert record.grid_definition_checksum == expected_checksum
    # Sanity: the checksum is a 64-char lowercase hex string.
    assert len(record.grid_definition_checksum) == 64
    assert set(record.grid_definition_checksum).issubset(set("0123456789abcdef"))


def test_input_record_capture_prevents_toctou(tmp_path: pathlib.Path) -> None:
    """(C4) A rewrite of grid.json AFTER `read_input_record` returns must NOT
    change the record's `grid_definition_checksum`. This proves the reader
    hashed the exact bytes it parsed (single-open), closing the TOCTOU window
    that a second-open checksum in the Task 3.1b writer would open."""
    grid_json, sidecar = _write_fixture(tmp_path)
    original_bytes = grid_json.read_bytes()
    original_checksum = sha256_bytes(original_bytes)
    record = read_input_record(
        "IFS",
        grid_json,
        sidecar,
        grid_definition_uri=_DEFAULT_GRID_DEFINITION_URI,
        expected_converter_version=_stub_converter_resolver,
    )
    assert record.grid_definition_checksum == original_checksum

    # Rewrite grid.json bytes with a different valid payload; simulate the
    # TOCTOU window where the file is replaced between the reader read and any
    # downstream re-open.
    rewritten_payload = _default_grid_payload()
    rewritten_payload["schema_version"] = "nhms.grid_definition.v99"
    rewritten_bytes = json.dumps(rewritten_payload).encode("utf-8")
    grid_json.write_bytes(rewritten_bytes)
    assert sha256_bytes(rewritten_bytes) != original_checksum

    # The record's checksum still reflects the FIRST bytes' digest, not the
    # rewritten ones — SUB-5 writer can trust it without re-opening.
    assert record.grid_definition_checksum == original_checksum


# =============================================================================
# SUB-5 / Task 3.1b — writer & CLI tests
# =============================================================================

# The 21-field snapshot provenance table mirrors Task 3.1b §"Field provenance"
# byte-for-byte. Test 6 walks every row.
_SUB5_FIELD_SOURCE_TABLE: tuple[tuple[str, str], ...] = (
    ("grid_snapshot_id", "DB gen_random_uuid()"),
    ("canonical_grid_key", "derive_canonical_grid_key(signature, bbox, native_resolution)"),
    ("source_id", "normalize_source_id(cli_source_id)"),
    ("grid_id", "record.grid_id"),
    ("grid_signature", "grid_signature_hash(record.cells)"),
    ("grid_definition_uri", "record.grid_definition_uri"),
    ("grid_definition_checksum", "record.grid_definition_checksum"),
    ("longitude_convention", "pinned literal '[-180, 180)'"),
    ("latitude_order", "record.latitude_order"),
    ("flatten_order", "record.flatten_order"),
    ("native_resolution", "record.native_resolution"),
    ("bbox_south", "record.download_bbox['south']"),
    ("bbox_north", "record.download_bbox['north']"),
    ("bbox_west", "record.download_bbox['west']"),
    ("bbox_east", "record.download_bbox['east']"),
    ("converter_version", "record.converter_version"),
    ("valid_from", "record.valid_from"),
    ("valid_to", "record.valid_to"),
    ("applicable_source_ids", "(normalize_source_id(cli_source_id),)"),
    ("superseded_at", "None on insert"),
    ("created_at", "DB default"),
)

_SUB5_RUN_PREFIX = "sub5_902"


# -----------------------------------------------------------------------------
# Pure-Python (no DB, no marker) tests — Task 3.1b evidence
# -----------------------------------------------------------------------------


def test_register_snapshot_signature_pinned() -> None:
    """The public writer signature is exactly `(record, *, source_id, store) -> UUID`."""
    sig = inspect.signature(register_snapshot)
    assert tuple(sig.parameters.keys()) == ("record", "source_id", "store"), (
        f"unexpected parameter names {tuple(sig.parameters.keys())!r}"
    )
    # `register_snapshot` uses `from __future__ import annotations`, so
    # annotations are strings. Resolve via `get_type_hints` for the strict
    # `is uuid.UUID` check.
    hints = inspect.get_annotations(register_snapshot, eval_str=True)
    assert hints["return"] is uuid.UUID
    # `source_id` and `store` must be keyword-only.
    for name in ("source_id", "store"):
        assert sig.parameters[name].kind == inspect.Parameter.KEYWORD_ONLY


def test_writer_provenance_table_covers_all_21_snapshot_fields() -> None:
    """The SUB-5 field-source table must enumerate every CanonicalGridSnapshot
    dataclass field byte-for-byte. A future field rename or addition to SUB-3
    surfaces here as a test failure."""
    dataclass_fields = tuple(f.name for f in dataclasses.fields(CanonicalGridSnapshot))
    table_fields = tuple(row[0] for row in _SUB5_FIELD_SOURCE_TABLE)
    assert set(dataclass_fields) == set(table_fields), (
        f"provenance table {set(table_fields)!r} does not cover CanonicalGridSnapshot "
        f"fields {set(dataclass_fields)!r}"
    )
    assert len(dataclass_fields) == 21


def test_cli_help_lists_required_flags() -> None:
    """`python -m workers.grid_registry --help` MUST list all four required flags."""
    proc = subprocess.run(
        [sys.executable, "-m", "workers.grid_registry", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"CLI --help exited nonzero: stderr={proc.stderr!r}"
    )
    for flag in ("--source-id", "--grid-json", "--sidecar", "--grid-definition-uri"):
        assert flag in proc.stdout, (
            f"CLI --help output missing flag {flag!r}; got:\n{proc.stdout}"
        )


def test_cli_database_url_env_fallback_with_env(tmp_path: pathlib.Path) -> None:
    """(F7) With DATABASE_URL set in env but no --database-url flag, the CLI
    MUST fall through to attempting a store call and fail with a store-level
    diagnostic — NOT the missing-env diagnostic. Uses a mock URL that fails
    at connect time to prove the fallback ran."""
    grid_json, sidecar = _write_fixture(tmp_path)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "DATABASE_URL": "postgres://mock:0/none",
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "workers.grid_registry",
            "--source-id",
            "ifs",
            "--grid-json",
            str(grid_json),
            "--sidecar",
            str(sidecar),
            "--grid-definition-uri",
            "s3://mock/x",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "DATABASE_URL environment variable is required" not in proc.stderr, (
        f"CLI reported missing-env even though DATABASE_URL was set. "
        f"stderr={proc.stderr!r}"
    )


def test_cli_database_url_env_fallback_no_env(tmp_path: pathlib.Path) -> None:
    """(F7) With NEITHER DATABASE_URL in env NOR --database-url flag, the CLI
    MUST fail with a diagnostic naming `DATABASE_URL`."""
    grid_json, sidecar = _write_fixture(tmp_path)
    env = {"PATH": os.environ.get("PATH", "")}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "workers.grid_registry",
            "--source-id",
            "ifs",
            "--grid-json",
            str(grid_json),
            "--sidecar",
            str(sidecar),
            "--grid-definition-uri",
            "s3://mock/x",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "DATABASE_URL environment variable is required" in proc.stderr, (
        f"CLI did not surface the required-env diagnostic. stderr={proc.stderr!r}"
    )


def _build_manual_record(
    *,
    override_cell_longitude: float | None = None,
) -> GridSnapshotInputRecord:
    """Build a `GridSnapshotInputRecord` via direct dataclass instantiation.

    Used for tests that need to bypass SUB-4's `_build_cells` normalization to
    prove the writer defends against a future SUB-4 refactor. `raw_grid_json_bytes`
    holds a valid rectilinear payload so the live-producer shim parses cleanly
    on the happy-path tests; callers overriding cell geometry can rely on the
    cells-vs-raw-bytes divergence to force `LiveProducerSignatureMismatchError`.
    """
    longitudes = tuple(_DEFAULT_LONGITUDES)
    latitudes = tuple(_DEFAULT_LATITUDES)
    cells: list[CellInput] = []
    for index, (lat, lon) in enumerate((lat, lon) for lat in latitudes for lon in longitudes):
        cell_lon = override_cell_longitude if index == 0 and override_cell_longitude is not None else lon
        cells.append(
            CellInput(
                grid_cell_id=str(index),
                canonical_ordinal=index + 1,
                longitude=cell_lon,
                latitude=lat,
            )
        )
    raw_grid_json_bytes = json.dumps(
        {
            "schema_version": "nhms.grid_definition.v1",
            "grid_id": "ifs_0p25",
            "layout": "rectilinear",
            "axis_order": ["latitude", "longitude"],
            "shape": [len(latitudes), len(longitudes)],
            "longitudes": list(longitudes),
            "latitudes": list(latitudes),
        }
    ).encode("utf-8")
    return GridSnapshotInputRecord(
        schema_version="nhms.grid_definition.v1",
        grid_id="ifs_0p25",
        layout="rectilinear",
        axis_order=("latitude", "longitude"),
        shape=(len(latitudes), len(longitudes)),
        longitudes=longitudes,
        latitudes=latitudes,
        converter_version=_DEFAULT_CONVERTER_VERSION,
        native_resolution=0.25,
        latitude_order="ascending",
        flatten_order="y_major_lat_then_lon",
        valid_from=datetime(2026, 7, 6, tzinfo=UTC),
        valid_to=None,
        download_bbox={
            "south": min(latitudes),
            "north": max(latitudes),
            "west": min(longitudes),
            "east": max(longitudes),
        },
        grid_definition_uri=_DEFAULT_GRID_DEFINITION_URI,
        grid_definition_checksum="a" * 64,
        cells=tuple(cells),
        raw_grid_json_bytes=raw_grid_json_bytes,
    )


@pytest.mark.parametrize("override_lon", [200.0, -200.0, 180.0])
def test_writer_asserts_normalized_longitude_defensively(override_lon: float) -> None:
    """Direct-dataclass record with cell longitude outside `[-180.0, 180.0)`
    MUST raise `RegistrationInvariantError` BEFORE any DB touch.

    Covers three boundary cases: `200.0` (above upper bound),
    `-200.0` (below lower bound), and `180.0` (upper bound is exclusive; the
    normalized value would be `-180.0`).
    """
    record = _build_manual_record(override_cell_longitude=override_lon)
    # The store MUST NOT be touched — pass a URL that would otherwise raise.
    store = PsycopgGridRegistryStore(database_url="postgres://never-touched:0/none")
    with pytest.raises(RegistrationInvariantError) as excinfo:
        register_snapshot(record, source_id="IFS", store=store)
    assert "cell 0" in str(excinfo.value)
    assert repr(override_lon) in str(excinfo.value)


def test_writer_accepts_normalized_longitude_lower_bound() -> None:
    """Positive-control: `-180.0` (exact lower inclusive bound) MUST NOT raise
    the defensive `RegistrationInvariantError`; the invariant window is
    `[-180.0, 180.0)`. The register call would fail later on a mock DB URL, so
    we assert only the specific invariant does not fire."""
    record = _build_manual_record(override_cell_longitude=-180.0)
    # The invariant should pass; anything after (live-producer / DB) may fail.
    store = PsycopgGridRegistryStore(database_url="postgres://never-touched:0/none")
    with pytest.raises(Exception) as excinfo:
        register_snapshot(record, source_id="IFS", store=store)
    assert not isinstance(excinfo.value, RegistrationInvariantError), (
        f"-180.0 must be accepted by the invariant guard; got "
        f"RegistrationInvariantError: {excinfo.value}"
    )


def test_error_hierarchy_is_registration_error_subclass() -> None:
    """All three writer errors inherit from `RegistrationError`."""
    assert issubclass(RegistrationInvariantError, RegistrationError)
    assert issubclass(LiveProducerSignatureMismatchError, RegistrationError)
    assert issubclass(GridDriftDetectedError, RegistrationError)


def test_live_producer_signature_matches_registry_on_valid_record(tmp_path: pathlib.Path) -> None:
    """The live-producer path returns the same hash as `grid_signature_hash(record.cells)`
    on a valid record — proves the two paths agree at the source of truth."""
    record = _read_default_record(tmp_path)
    registry_computed = grid_signature_hash(record.cells)
    live_computed = _live_producer_signature(record, registry_computed=registry_computed)
    assert registry_computed == live_computed
    # The live-producer shim consumed the raw grid.json bytes SUB-4 pinned via
    # `grid_definition_checksum`, not a re-serialization derived from the same
    # SUB-4-parsed axis tuples (F5).
    assert isinstance(record.raw_grid_json_bytes, bytes)
    assert len(record.raw_grid_json_bytes) > 0


def test_backfill_signature_constant_is_reused_verbatim() -> None:
    """Pin: SUB-5 tests reuse SUB-4's `_BACKFILL_SIGNATURE` constant byte-for-byte.
    A future edit that redefines the constant surfaces here."""
    assert _BACKFILL_SIGNATURE == "6c008901b8b7" + "0" * 52
    assert len(_BACKFILL_SIGNATURE) == 64
    # Encoding pin: the derived canonical_grid_key for the pinned constant must
    # equal the SUB-4 pinned value byte-for-byte.
    key = derive_canonical_grid_key(
        _BACKFILL_SIGNATURE, dict(_BACKFILL_BBOX), _BACKFILL_RESOLUTION
    )
    assert key == _EXPECTED_BACKFILL_KEY


def test_writer_race_fallback_only_triggers_on_unique_violation(
    tmp_path: pathlib.Path,
) -> None:
    """A generic `RegistryStoreError` from `insert_snapshot` (e.g. a transient
    connection reset) MUST propagate raw and MUST NOT trigger the
    `find_snapshot_by_identity` race-fallback. Only
    `RegistryUniqueViolationError` (SQLSTATE 23505) should activate that path.
    Guards against silent misinterpretation of non-race errors as race-lost."""
    record = _read_default_record(tmp_path)
    store = PsycopgGridRegistryStore(database_url="postgres://never-touched:0/none")

    call_counter = {"find": 0}

    def counting_find_snapshot_by_identity(*_a: Any, **_kw: Any) -> uuid.UUID | None:
        call_counter["find"] += 1
        if call_counter["find"] == 1:
            # Pre-insert idempotency check: no existing snapshot.
            return None
        # A second call would be the race fallback — must not fire.
        pytest.fail(
            "race fallback invoked for non-UniqueViolation store error; "
            "fallback must be scoped to RegistryUniqueViolationError only."
        )

    def no_drift(*_a: Any, **_kw: Any) -> None:
        return None

    def failing_insert(*_a: Any, **_kw: Any) -> uuid.UUID:
        raise RegistryStoreError("simulated transient connection failure")

    # Monkeypatch on the dataclass instance without pytest fixtures (module fn).
    object.__setattr__(store, "find_snapshot_by_identity", counting_find_snapshot_by_identity)
    object.__setattr__(store, "find_conflicting_snapshot_by_source_grid", no_drift)
    object.__setattr__(store, "insert_snapshot", failing_insert)

    with pytest.raises(RegistryStoreError, match="simulated transient") as excinfo:
        register_snapshot(record, source_id="IFS", store=store)
    # Not the narrower subtype — the raw base error propagates unchanged.
    assert not isinstance(excinfo.value, RegistryUniqueViolationError)
    assert call_counter["find"] == 1


# -----------------------------------------------------------------------------
# Real-DB integration tests — Task 3.1b evidence
# -----------------------------------------------------------------------------


def _seed_sub5_data_sources(database_url: str) -> None:
    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            for src in ("IFS", "gfs", "ERA5"):
                cursor.execute(
                    """
                    INSERT INTO met.data_source (
                        source_id, source_name, source_type, status, native_format,
                        adapter_name, config_json
                    )
                    VALUES (%s, %s, 'forecast', 'mock', 'netcdf', %s, %s)
                    ON CONFLICT (source_id) DO NOTHING
                    """,
                    (src, f"{src} SUB-5 test source", src, _PgJson({"test": True})),
                )
    finally:
        connection.close()


@pytest.fixture(scope="module")
def sub5_migrated_database(integration_database_url: str) -> Iterator[str]:
    from tests.integration_helpers import apply_migrations_from_zero

    apply_migrations_from_zero(integration_database_url)
    _seed_sub5_data_sources(integration_database_url)
    yield integration_database_url
    # Teardown: delete any canonical_grid_snapshot rows referencing the SUB-5
    # seeded data_source ids so downstream module-scoped fixtures in
    # `tests/test_real_database_integration.py` can DELETE from met.data_source
    # without hitting the canonical_grid_snapshot.source_id FK. The FK on
    # met.canonical_grid_cell.grid_snapshot_id is ON DELETE CASCADE (see
    # db/migrations/000043_canonical_grid_snapshot.sql), so a single DELETE on
    # the snapshot table cascades to cell rows.
    connection = psycopg2.connect(integration_database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM met.canonical_grid_snapshot
                WHERE source_id IN ('IFS', 'gfs', 'ERA5')
                """
            )
    finally:
        connection.close()


def _fetch_snapshot_row(database_url: str, grid_snapshot_id: uuid.UUID) -> dict[str, Any]:
    # Register psycopg2's UUID adapter so PostgreSQL UUID columns come back
    # as `uuid.UUID` objects (not str). Migration 000043 stores
    # `grid_snapshot_id` as `UUID` and callers compare against `uuid.UUID`
    # values returned by `register_snapshot`.
    psycopg2.extras.register_uuid()
    connection = psycopg2.connect(
        database_url, cursor_factory=psycopg2.extras.RealDictCursor
    )
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM met.canonical_grid_snapshot WHERE grid_snapshot_id = %s",
                (str(grid_snapshot_id),),
            )
            row = cursor.fetchone()
        assert row is not None
        return dict(row)
    finally:
        connection.close()


def _count_rows_for_id(database_url: str, grid_snapshot_id: uuid.UUID) -> tuple[int, int]:
    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM met.canonical_grid_snapshot WHERE grid_snapshot_id = %s",
                (str(grid_snapshot_id),),
            )
            snap_count = cursor.fetchone()[0]
            cursor.execute(
                "SELECT COUNT(*) FROM met.canonical_grid_cell WHERE grid_snapshot_id = %s",
                (str(grid_snapshot_id),),
            )
            cell_count = cursor.fetchone()[0]
        return snap_count, cell_count
    finally:
        connection.close()


def _write_unique_fixture(
    tmp_path: pathlib.Path,
    *,
    suffix: str,
    grid_id: str | None = None,
) -> tuple[pathlib.Path, pathlib.Path, str]:
    """Write a grid.json + sidecar pair whose grid_id embeds `suffix` for uniqueness."""
    payload = _default_grid_payload()
    if grid_id is not None:
        payload["grid_id"] = grid_id
    else:
        payload["grid_id"] = f"grid_{suffix}"
    grid_json = tmp_path / f"grid_{suffix}.json"
    sidecar = tmp_path / f"sidecar_{suffix}.json"
    grid_json.write_text(json.dumps(payload), encoding="utf-8")
    sidecar.write_text(
        json.dumps(_default_sidecar_payload()),
        encoding="utf-8",
    )
    uri = f"canonical/IFS/grid/{payload['grid_id']}/grid.json"
    return grid_json, sidecar, uri


@pytest.mark.integration
@pytest.mark.parametrize(
    ("cli_source_id", "expected_normalized"),
    [("ifs", "IFS"), ("GFS", "gfs")],
    ids=["ifs_lowercase", "gfs_uppercase"],
)
def test_writer_snapshot_field_provenance_round_trip(
    sub5_migrated_database: str,
    tmp_path: pathlib.Path,
    cli_source_id: str,
    expected_normalized: str,
) -> None:
    """Every one of the 21 snapshot fields round-trips per Task 3.1b field
    provenance. Also proves `applicable_source_ids` equals the single-element
    tuple of the normalized source id (matching the SUB-8 case rules).
    `longitude_convention` MUST equal '[-180, 180)' byte-for-byte."""
    grid_json, sidecar, uri = _write_unique_fixture(
        tmp_path, suffix=f"prov_{cli_source_id}"
    )
    record = read_input_record(
        cli_source_id,
        grid_json,
        sidecar,
        grid_definition_uri=uri,
        expected_converter_version=_stub_converter_resolver,
    )

    store = PsycopgGridRegistryStore(database_url=sub5_migrated_database)
    snapshot_id = register_snapshot(record, source_id=cli_source_id, store=store)
    assert isinstance(snapshot_id, uuid.UUID)

    row = _fetch_snapshot_row(sub5_migrated_database, snapshot_id)
    assert row["grid_snapshot_id"] == snapshot_id
    assert row["source_id"] == expected_normalized
    assert row["grid_id"] == record.grid_id
    # Byte-for-byte pin.
    assert row["longitude_convention"] == "[-180, 180)"
    assert row["latitude_order"] == record.latitude_order
    assert row["flatten_order"] == record.flatten_order
    assert row["native_resolution"] == pytest.approx(record.native_resolution)
    assert row["bbox_south"] == pytest.approx(record.download_bbox["south"])
    assert row["bbox_north"] == pytest.approx(record.download_bbox["north"])
    assert row["bbox_west"] == pytest.approx(record.download_bbox["west"])
    assert row["bbox_east"] == pytest.approx(record.download_bbox["east"])
    assert row["converter_version"] == record.converter_version
    assert row["valid_from"] == record.valid_from
    assert row["valid_to"] is None
    assert list(row["applicable_source_ids"]) == [expected_normalized]
    assert row["superseded_at"] is None
    assert row["created_at"] is not None
    assert row["grid_definition_uri"] == uri
    assert row["grid_definition_checksum"] == record.grid_definition_checksum
    # grid_signature equals the SUB-1 shared-helper value for the record's cells.
    expected_signature = grid_signature_hash(record.cells)
    assert row["grid_signature"] == expected_signature
    # canonical_grid_key equals the derived value.
    expected_key = derive_canonical_grid_key(
        expected_signature, dict(record.download_bbox), record.native_resolution
    )
    assert row["canonical_grid_key"] == expected_key


@pytest.mark.integration
def test_writer_live_producer_signature_matches_at_registration(
    sub5_migrated_database: str, tmp_path: pathlib.Path
) -> None:
    """When registration succeeds, the stored `grid_signature` equals BOTH the
    registry-computed hash AND the live producer's `_grid_points_from_definition`
    recompute. This proves the two paths agree at the write boundary."""
    grid_json, sidecar, uri = _write_unique_fixture(tmp_path, suffix="live_match")
    record = read_input_record(
        "IFS",
        grid_json,
        sidecar,
        grid_definition_uri=uri,
        expected_converter_version=_stub_converter_resolver,
    )
    registry_computed = grid_signature_hash(record.cells)
    live_computed = _live_producer_signature(record, registry_computed=registry_computed)
    assert registry_computed == live_computed

    store = PsycopgGridRegistryStore(database_url=sub5_migrated_database)
    snapshot_id = register_snapshot(record, source_id="IFS", store=store)
    row = _fetch_snapshot_row(sub5_migrated_database, snapshot_id)
    assert row["grid_signature"] == registry_computed == live_computed


def test_writer_rejects_live_producer_signature_mismatch() -> None:
    """A record whose `cells` were mutated to disagree with the producer's
    live path MUST raise `LiveProducerSignatureMismatchError` with BOTH
    computed values on the exception attributes. Runs pure-Python (no DB) —
    we mutate the cells tuple to simulate the drift condition."""
    record = _build_manual_record()
    # Mutate cell 0's latitude to break the equality without leaving the
    # normalized-longitude range (defensive assertion stays green).
    mutated_cells = list(record.cells)
    original = mutated_cells[0]
    mutated_cells[0] = CellInput(
        grid_cell_id=original.grid_cell_id,
        canonical_ordinal=original.canonical_ordinal,
        longitude=original.longitude,
        latitude=original.latitude + 0.5,
    )
    mutated_record = dataclasses.replace(record, cells=tuple(mutated_cells))
    store = PsycopgGridRegistryStore(database_url="postgres://never-touched:0/none")
    with pytest.raises(LiveProducerSignatureMismatchError) as excinfo:
        register_snapshot(mutated_record, source_id="IFS", store=store)
    error = excinfo.value
    # Both hashes must be populated on the exception attributes.
    assert error.registry_computed and len(error.registry_computed) == 64
    assert error.live_producer_computed and len(error.live_producer_computed) == 64
    assert error.registry_computed != error.live_producer_computed


def test_writer_rejects_live_producer_signature_mismatch_via_raw_bytes_mutation() -> None:
    """(F5) Mutating `raw_grid_json_bytes` (the live-producer shim input)
    without mutating `cells` MUST also raise `LiveProducerSignatureMismatchError`.

    This proves the live path re-parses the actual bytes rather than the
    SUB-4-parsed axis tuples: a byte-level edit that shifts a longitude value
    diverges the live hash from the record.cells hash.
    """
    record = _build_manual_record()
    payload = json.loads(record.raw_grid_json_bytes.decode("utf-8"))
    payload["longitudes"][0] = payload["longitudes"][0] + 0.01
    mutated_bytes = json.dumps(payload).encode("utf-8")
    mutated_record = dataclasses.replace(record, raw_grid_json_bytes=mutated_bytes)
    store = PsycopgGridRegistryStore(database_url="postgres://never-touched:0/none")
    with pytest.raises(LiveProducerSignatureMismatchError) as excinfo:
        register_snapshot(mutated_record, source_id="IFS", store=store)
    error = excinfo.value
    assert error.registry_computed and len(error.registry_computed) == 64
    assert error.live_producer_computed and len(error.live_producer_computed) == 64
    assert error.registry_computed != error.live_producer_computed


def test_live_producer_signature_mismatch_populates_registry_computed_on_none_return() -> None:
    """(F1) When `_grid_points_from_definition` returns None (rectilinear branch
    cannot rebuild), the raised `LiveProducerSignatureMismatchError` MUST carry
    the caller's `registry_computed` (a valid 64-hex string), not the empty
    string the pre-fix code planted."""
    # Build a manual record whose shape/longitudes/latitudes are internally
    # inconsistent so `_grid_points_from_definition` fails the rectilinear
    # shape check and returns None. Shape (3, 3) but longitudes of length 4:
    # `len(longitudes) != x_count` at producer.py:1468 → None.
    base = _build_manual_record()
    inconsistent_bytes = json.dumps(
        {
            "schema_version": base.schema_version,
            "grid_id": base.grid_id,
            "layout": "rectilinear",
            "axis_order": list(base.axis_order),
            "shape": [3, 3],
            "longitudes": list(range(4)),  # length mismatch
            "latitudes": list(range(3)),
        }
    ).encode("utf-8")
    bad_record = dataclasses.replace(
        base,
        shape=(3, 3),
        longitudes=tuple(range(4)),
        latitudes=tuple(range(3)),
        raw_grid_json_bytes=inconsistent_bytes,
    )
    expected_registry_computed = grid_signature_hash(bad_record.cells)
    with pytest.raises(LiveProducerSignatureMismatchError) as excinfo:
        _live_producer_signature(bad_record, registry_computed=expected_registry_computed)
    error = excinfo.value
    assert error.live_producer_computed is None
    assert error.registry_computed == expected_registry_computed
    assert len(error.registry_computed) == 64
    assert set(error.registry_computed).issubset(set("0123456789abcdef"))


def test_input_record_raw_grid_json_bytes_matches_checksum(tmp_path: pathlib.Path) -> None:
    """(F5) SHA-256 of `record.raw_grid_json_bytes` MUST equal
    `record.grid_definition_checksum` — the checksum-verified bytes flow from
    SUB-4's single-open read through to the SUB-5 live-producer shim."""
    record = _read_default_record(tmp_path)
    assert isinstance(record.raw_grid_json_bytes, bytes)
    assert len(record.raw_grid_json_bytes) > 0
    assert sha256_bytes(record.raw_grid_json_bytes) == record.grid_definition_checksum


@pytest.mark.integration
def test_writer_idempotent_on_identical_input(
    sub5_migrated_database: str, tmp_path: pathlib.Path
) -> None:
    """Two `register_snapshot` calls with an identical record + source_id
    return the SAME UUID and only ONE row exists in the DB."""
    grid_json, sidecar, uri = _write_unique_fixture(tmp_path, suffix="idem")
    record = read_input_record(
        "IFS",
        grid_json,
        sidecar,
        grid_definition_uri=uri,
        expected_converter_version=_stub_converter_resolver,
    )
    store = PsycopgGridRegistryStore(database_url=sub5_migrated_database)
    first_id = register_snapshot(record, source_id="IFS", store=store)
    second_id = register_snapshot(record, source_id="IFS", store=store)
    assert first_id == second_id
    # Only one snapshot row for this grid_id.
    connection = psycopg2.connect(sub5_migrated_database)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM met.canonical_grid_snapshot
                WHERE source_id = %s AND grid_id = %s
                """,
                ("IFS", record.grid_id),
            )
            assert cursor.fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.integration
def test_writer_rejects_grid_drift_within_source(
    sub5_migrated_database: str, tmp_path: pathlib.Path
) -> None:
    """A second `register_snapshot` with same `(source_id, grid_id)` but a
    mutated cell (different signature) MUST raise `GridDriftDetectedError`
    naming both signatures. SUB-5 does NOT own supersession."""
    grid_id = f"grid_drift_{uuid.uuid4().hex[:8]}"
    # First registration: use the standard 5x5 grid.
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    grid_json_a, sidecar_a, uri = _write_unique_fixture(dir_a, suffix="drift_a", grid_id=grid_id)
    record_a = read_input_record(
        "IFS",
        grid_json_a,
        sidecar_a,
        grid_definition_uri=uri,
        expected_converter_version=_stub_converter_resolver,
    )
    store = PsycopgGridRegistryStore(database_url=sub5_migrated_database)
    first_id = register_snapshot(record_a, source_id="IFS", store=store)

    # Second registration: same grid_id, but shift the latitudes (identity drift
    # while staying rectilinear and matching bbox for the drift path — we want
    # SUB-5 to reject at the drift check).
    dir_b = tmp_path / "b"
    dir_b.mkdir()
    drifted_latitudes = [lat + 0.5 for lat in _DEFAULT_LATITUDES]
    drifted_payload = _default_grid_payload(latitudes=drifted_latitudes)
    drifted_payload["grid_id"] = grid_id
    grid_json_b = dir_b / "grid.json"
    sidecar_b = dir_b / "sidecar.json"
    grid_json_b.write_text(json.dumps(drifted_payload), encoding="utf-8")
    sidecar_b.write_text(
        json.dumps(_default_sidecar_payload(latitudes=drifted_latitudes)),
        encoding="utf-8",
    )
    record_b = read_input_record(
        "IFS",
        grid_json_b,
        sidecar_b,
        grid_definition_uri=uri,  # same URI intentionally — drift scenario
        expected_converter_version=_stub_converter_resolver,
    )
    # Sanity: signatures actually differ.
    assert grid_signature_hash(record_a.cells) != grid_signature_hash(record_b.cells)

    with pytest.raises(GridDriftDetectedError) as excinfo:
        register_snapshot(record_b, source_id="IFS", store=store)
    error = excinfo.value
    assert error.source_id == "IFS"
    assert error.grid_id == grid_id
    assert error.existing_snapshot_id == first_id
    assert error.existing_signature == grid_signature_hash(record_a.cells)
    assert error.registry_computed_signature == grid_signature_hash(record_b.cells)

    # (F9) Post-condition: only v1 exists — the drift-rejected v2 was NOT
    # inserted. Guards against a future refactor that would silently write
    # the drift row anyway.
    connection = psycopg2.connect(sub5_migrated_database)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM met.canonical_grid_snapshot
                WHERE source_id = %s AND grid_id = %s
                """,
                ("IFS", grid_id),
            )
            assert cursor.fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.integration
def test_writer_atomicity_no_partial_rows(
    sub5_migrated_database: str, tmp_path: pathlib.Path, monkeypatch: Any
) -> None:
    """A mid-write failure inside `insert_snapshot` MUST leave ZERO rows across
    both the snapshot and cell tables — writer-level atomicity guarantee."""
    grid_json, sidecar, uri = _write_unique_fixture(tmp_path, suffix="atomic")
    record = read_input_record(
        "IFS",
        grid_json,
        sidecar,
        grid_definition_uri=uri,
        expected_converter_version=_stub_converter_resolver,
    )
    store = PsycopgGridRegistryStore(database_url=sub5_migrated_database)

    # Force `insert_snapshot` to raise mid-way. We monkeypatch the store's
    # `insert_snapshot` to raise a RegistryStoreError AFTER any partial write
    # would have happened — the sibling test at
    # `tests/test_grid_registry_store.py::test_mid_write_failure_rolls_back_all_rows`
    # proves the store-level rollback; here we prove the WRITER surfaces the
    # error and does not leave partial rows for the same key.
    # `PsycopgGridRegistryStore` is a frozen dataclass, so we patch the class-level
    # method rather than an instance attribute; monkeypatch teardown restores it.

    def failing_insert(
        self: PsycopgGridRegistryStore,
        snapshot: CanonicalGridSnapshot,
        cells: list[CanonicalGridCell],
    ) -> uuid.UUID:
        del self, snapshot, cells
        raise RegistryStoreError("simulated mid-write DB failure for atomicity test")

    monkeypatch.setattr(PsycopgGridRegistryStore, "insert_snapshot", failing_insert)

    with pytest.raises(RegistryStoreError):
        register_snapshot(record, source_id="IFS", store=store)

    # Verify no rows exist for this grid_id. monkeypatch teardown restores the
    # original class method automatically at test exit.
    connection = psycopg2.connect(sub5_migrated_database)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM met.canonical_grid_snapshot
                WHERE source_id = %s AND grid_id = %s
                """,
                ("IFS", record.grid_id),
            )
            assert cursor.fetchone()[0] == 0
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM met.canonical_grid_cell c
                JOIN met.canonical_grid_snapshot s USING (grid_snapshot_id)
                WHERE s.source_id = %s AND s.grid_id = %s
                """,
                ("IFS", record.grid_id),
            )
            assert cursor.fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.integration
def test_writer_backfill_shared_canonical_grid_key(
    sub5_migrated_database: str, tmp_path: pathlib.Path
) -> None:
    """Two source snapshots with identical `grid_signature`, identical bbox,
    and identical `native_resolution` MUST share the same `canonical_grid_key`
    (Task 3.1b Evidence Floor: 'paired with the Task 3.3 backfill acceptance').

    We simulate the pre-shared-eligibility state: each snapshot's
    `applicable_source_ids` equals its own single normalized source id.
    """
    # Same grid.json bytes but registered under different source_ids means
    # the FK to met.data_source picks up the normalized id. The signature is
    # signature-of-cells, which is the same regardless of source_id.
    (tmp_path / "ifs").mkdir(exist_ok=True)
    grid_json_ifs, sidecar_ifs, uri_ifs = _write_unique_fixture(
        tmp_path / "ifs", suffix="backfill_ifs", grid_id=f"shared_{uuid.uuid4().hex[:6]}"
    )
    # Reuse the same grid_id? No — the DB has a UNIQUE(source_id, grid_id)
    # in effect via the drift check; different source ids means different rows.
    # But `applicable_source_ids` shows same normalized source id.
    dir_gfs = tmp_path / "gfs"
    dir_gfs.mkdir()
    grid_payload = _default_grid_payload()
    grid_payload["grid_id"] = f"shared_gfs_{uuid.uuid4().hex[:6]}"
    grid_json_gfs = dir_gfs / "grid.json"
    sidecar_gfs = dir_gfs / "sidecar.json"
    grid_json_gfs.write_text(json.dumps(grid_payload), encoding="utf-8")
    sidecar_gfs.write_text(
        json.dumps(_default_sidecar_payload()),
        encoding="utf-8",
    )
    uri_gfs = f"canonical/gfs/grid/{grid_payload['grid_id']}/grid.json"

    store = PsycopgGridRegistryStore(database_url=sub5_migrated_database)
    record_ifs = read_input_record(
        "IFS",
        grid_json_ifs,
        sidecar_ifs,
        grid_definition_uri=uri_ifs,
        expected_converter_version=_stub_converter_resolver,
    )
    record_gfs = read_input_record(
        "gfs",
        grid_json_gfs,
        sidecar_gfs,
        grid_definition_uri=uri_gfs,
        expected_converter_version=_stub_converter_resolver,
    )
    # Sanity: identical signatures because cells are identical (same
    # longitudes/latitudes even though grid_ids differ).
    sig_ifs = grid_signature_hash(record_ifs.cells)
    sig_gfs = grid_signature_hash(record_gfs.cells)
    assert sig_ifs == sig_gfs
    assert record_ifs.native_resolution == record_gfs.native_resolution
    assert record_ifs.download_bbox == record_gfs.download_bbox

    id_ifs = register_snapshot(record_ifs, source_id="IFS", store=store)
    id_gfs = register_snapshot(record_gfs, source_id="gfs", store=store)
    row_ifs = _fetch_snapshot_row(sub5_migrated_database, id_ifs)
    row_gfs = _fetch_snapshot_row(sub5_migrated_database, id_gfs)
    # Same canonical_grid_key because (signature, bbox, native_resolution) match.
    assert row_ifs["canonical_grid_key"] == row_gfs["canonical_grid_key"]
    # Pre-shared-eligibility phase: each row's applicable_source_ids is its own.
    assert list(row_ifs["applicable_source_ids"]) == ["IFS"]
    assert list(row_gfs["applicable_source_ids"]) == ["gfs"]


# -----------------------------------------------------------------------------
# SUB-10 / Task 3.3 backfill acceptance — live grid.json + sidecar files
# -----------------------------------------------------------------------------


_LIVE_CANONICAL_ROOT = pathlib.Path(__file__).resolve().parent.parent / "canonical"


def _row_to_snapshot(row: dict[str, Any]) -> CanonicalGridSnapshot:
    """Rebuild a ``CanonicalGridSnapshot`` dataclass from a ``_fetch_snapshot_row``
    result.

    Used by SUB-10 §3.3 backfill acceptance so the shared-binding eligibility
    entry point (Task 5.1) can be invoked with real persisted rows without
    routing through the SUB-3 ``load_snapshot`` object-reader path (which
    demands an object-store to re-verify the ``grid_definition_checksum``;
    the SUB-10 test asserts the DB rows only, not object-store bytes).
    """
    return CanonicalGridSnapshot(
        grid_snapshot_id=row["grid_snapshot_id"],
        canonical_grid_key=row["canonical_grid_key"],
        source_id=row["source_id"],
        grid_id=row["grid_id"],
        grid_signature=row["grid_signature"],
        grid_definition_uri=row["grid_definition_uri"],
        grid_definition_checksum=row["grid_definition_checksum"],
        longitude_convention=row["longitude_convention"],
        latitude_order=row["latitude_order"],
        flatten_order=row["flatten_order"],
        native_resolution=float(row["native_resolution"]),
        bbox_south=float(row["bbox_south"]),
        bbox_north=float(row["bbox_north"]),
        bbox_west=float(row["bbox_west"]),
        bbox_east=float(row["bbox_east"]),
        converter_version=row["converter_version"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        applicable_source_ids=tuple(row["applicable_source_ids"]),
        superseded_at=row["superseded_at"],
        created_at=row["created_at"],
    )


@pytest.mark.integration
def test_backfill_ifs_gfs_share_key(
    sub5_migrated_database: str,
) -> None:
    """SUB-10 §3.3 backfill acceptance: the two LIVE snapshot rows registered
    from the committed `canonical/{IFS,gfs}/grid/{grid_id}/grid.json` + sidecar
    files share one `canonical_grid_key`, carry the pinned bbox
    63-145°E / 8-64°N and `native_resolution = 0.25`, and after
    `evaluate_shared_binding_eligibility` acceptance both rows'
    `applicable_source_ids` cover both `IFS` and `gfs`.

    Fixture-bootstrapped from the SAME 4 committed live files node-27 uses:
    reading them AS the inputs (not fabricating identical bytes) proves the
    committed live assets themselves discharge the SUB-1 shared-signature
    invariant at SUB-4's canonical_grid_key boundary, not just fabricated
    fixture blocks.

    Set-equality assertion on `applicable_source_ids` (not positional) per the
    §3.3 pin-relaxation recorded in tasks.md §5.1 — the SUB-3 store's
    position-preserving append leaves the IFS row `["IFS", "gfs"]` and the
    gfs row `["gfs", "IFS"]`, both satisfying
    `sorted(...) == ["IFS", "gfs"]`.
    """
    # Sanity: the 4 live files exist in the committed tree at the URIs
    # SUB-10 pinned for node-27.
    grid_json_ifs = _LIVE_CANONICAL_ROOT / "IFS" / "grid" / "ifs_0p25" / "grid.json"
    sidecar_ifs = (
        _LIVE_CANONICAL_ROOT / "IFS" / "grid" / "ifs_0p25" / "grid_snapshot_metadata.json"
    )
    grid_json_gfs = _LIVE_CANONICAL_ROOT / "gfs" / "grid" / "gfs_0p25" / "grid.json"
    sidecar_gfs = (
        _LIVE_CANONICAL_ROOT / "gfs" / "grid" / "gfs_0p25" / "grid_snapshot_metadata.json"
    )
    for path in (grid_json_ifs, sidecar_ifs, grid_json_gfs, sidecar_gfs):
        assert path.is_file(), f"missing SUB-10 live asset: {path}"

    uri_ifs = "canonical/IFS/grid/ifs_0p25/grid.json"
    uri_gfs = "canonical/gfs/grid/gfs_0p25/grid.json"

    record_ifs = read_input_record(
        "IFS",
        grid_json_ifs,
        sidecar_ifs,
        grid_definition_uri=uri_ifs,
        expected_converter_version=_stub_converter_resolver,
    )
    record_gfs = read_input_record(
        "gfs",
        grid_json_gfs,
        sidecar_gfs,
        grid_definition_uri=uri_gfs,
        expected_converter_version=_stub_converter_resolver,
    )

    # SUB-1 shared-signature invariant: identical bbox × resolution × axis
    # order → byte-identical signatures.
    sig_ifs = grid_signature_hash(record_ifs.cells)
    sig_gfs = grid_signature_hash(record_gfs.cells)
    assert sig_ifs == sig_gfs
    assert record_ifs.native_resolution == pytest.approx(0.25)
    assert record_gfs.native_resolution == pytest.approx(0.25)
    assert record_ifs.download_bbox == record_gfs.download_bbox

    store = PsycopgGridRegistryStore(database_url=sub5_migrated_database)
    id_ifs = register_snapshot(record_ifs, source_id="IFS", store=store)
    id_gfs = register_snapshot(record_gfs, source_id="gfs", store=store)

    row_ifs = _fetch_snapshot_row(sub5_migrated_database, id_ifs)
    row_gfs = _fetch_snapshot_row(sub5_migrated_database, id_gfs)

    # (1) shared canonical_grid_key.
    assert row_ifs["canonical_grid_key"] == row_gfs["canonical_grid_key"]

    # (2) pinned bbox 63-145°E / 8-64°N and native_resolution = 0.25 on BOTH rows.
    for row in (row_ifs, row_gfs):
        assert row["bbox_south"] == pytest.approx(8.0)
        assert row["bbox_north"] == pytest.approx(64.0)
        assert row["bbox_west"] == pytest.approx(63.0)
        assert row["bbox_east"] == pytest.approx(145.0)
        assert row["native_resolution"] == pytest.approx(0.25)

    # (3) Post-3.1b phase: each row's applicable_source_ids equals its own
    # single normalized source id (SUB-5 write semantics before SUB-8 fires).
    assert list(row_ifs["applicable_source_ids"]) == ["IFS"]
    assert list(row_gfs["applicable_source_ids"]) == ["gfs"]

    # (4) Opt-in step: pre-populate applicable_source_ids to satisfy SUB-8's
    # registry-state gate (check #4). SUB-8 is a fail-closed audit that
    # requires both rows to already list both source ids; the eligibility
    # function only re-writes the canonical pair idempotently. In production
    # the operator (or a CLI flag on registration) writes the pair before
    # invoking eligibility.
    store.extend_applicable_source_ids(id_ifs, ("IFS", "gfs"))
    store.extend_applicable_source_ids(id_gfs, ("IFS", "gfs"))

    # Reload rows so the snapshots handed to SUB-8 reflect the opt-in state.
    row_ifs = _fetch_snapshot_row(sub5_migrated_database, id_ifs)
    row_gfs = _fetch_snapshot_row(sub5_migrated_database, id_gfs)
    assert sorted(row_ifs["applicable_source_ids"]) == ["IFS", "gfs"]
    assert sorted(row_gfs["applicable_source_ids"]) == ["IFS", "gfs"]

    # (5) Invoke SUB-8 shared-binding eligibility on the two opted-in rows.
    # Fabricated evidence: URI can be a fake because the decision only checks
    # non-None; verified_source_ids covers both normalized ids.
    snapshot_ifs = _row_to_snapshot(row_ifs)
    snapshot_gfs = _row_to_snapshot(row_gfs)
    evidence = SharedBindingVerificationEvidence(
        verified_source_ids=frozenset({"IFS", "gfs"}),
        comparison_evidence_uri="s3://nhms-evidence/backfill-2026-07-08/comparison.json",
    )
    # Acceptance returns None; any denial raises SharedBindingEligibilityError.
    result = evaluate_shared_binding_eligibility(
        snapshot_ifs,
        snapshot_gfs,
        verification_evidence=evidence,
        store=store,
    )
    assert result is None

    # (6) Reload both rows; SUB-8 acceptance-time canonical-pair write is
    # idempotent (extend semantics) over the pre-opted-in state. Set-equality
    # assertion per the §3.3 pin-relaxation (positional would fail on the gfs
    # row that lands as ["gfs", "IFS"] under position-preserving append).
    reloaded_ifs = _fetch_snapshot_row(sub5_migrated_database, id_ifs)
    reloaded_gfs = _fetch_snapshot_row(sub5_migrated_database, id_gfs)
    assert sorted(reloaded_ifs["applicable_source_ids"]) == ["IFS", "gfs"]
    assert sorted(reloaded_gfs["applicable_source_ids"]) == ["IFS", "gfs"]

    # (7) bbox pin re-asserted on the reloaded rows for §3.3 evidence completeness.
    for row in (reloaded_ifs, reloaded_gfs):
        assert row["bbox_south"] == pytest.approx(8.0)
        assert row["bbox_north"] == pytest.approx(64.0)
        assert row["bbox_west"] == pytest.approx(63.0)
        assert row["bbox_east"] == pytest.approx(145.0)
        assert row["native_resolution"] == pytest.approx(0.25)


# -----------------------------------------------------------------------------
# Phase 4.5 gate additions — F2, F3, F4, F6, F8 integration coverage
# -----------------------------------------------------------------------------


def test_find_conflicting_snapshot_by_source_grid_wraps_db_error_as_registry_store_error() -> None:
    """(F2) A DB connection failure inside
    `find_conflicting_snapshot_by_source_grid` MUST surface as `RegistryStoreError`,
    not a raw `psycopg2.OperationalError` — the store owns the boundary."""
    bad_store = PsycopgGridRegistryStore(database_url="postgres://mock:0/none")
    with pytest.raises(RegistryStoreError):
        bad_store.find_conflicting_snapshot_by_source_grid("IFS", "grid_x", "a" * 64)


@pytest.mark.integration
def test_writer_atomicity_derive_canonical_grid_key_raise(
    sub5_migrated_database: str, tmp_path: pathlib.Path, monkeypatch: Any
) -> None:
    """(F4) A raise from `derive_canonical_grid_key` MUST propagate and leave
    ZERO rows across the snapshot / cell tables — writer atomicity guarantee."""
    # Randomize per-run so a stale row from a prior pytest invocation cannot
    # short-circuit through find_snapshot_by_identity and bypass the
    # derive_canonical_grid_key path this test exercises.
    grid_json, sidecar, uri = _write_unique_fixture(
        tmp_path, suffix=f"atomic_key_{uuid.uuid4().hex[:8]}"
    )
    record = read_input_record(
        "IFS",
        grid_json,
        sidecar,
        grid_definition_uri=uri,
        expected_converter_version=_stub_converter_resolver,
    )
    store = PsycopgGridRegistryStore(database_url=sub5_migrated_database)

    def raising_derive(*_a: Any, **_kw: Any) -> str:
        raise ValueError("simulated derive_canonical_grid_key failure")

    monkeypatch.setattr(
        "workers.grid_registry.registry.derive_canonical_grid_key", raising_derive
    )

    with pytest.raises(ValueError, match="simulated"):
        register_snapshot(record, source_id="IFS", store=store)

    connection = psycopg2.connect(sub5_migrated_database)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM met.canonical_grid_snapshot
                WHERE source_id = %s AND grid_id = %s
                """,
                ("IFS", record.grid_id),
            )
            assert cursor.fetchone()[0] == 0
    finally:
        connection.close()


@pytest.mark.integration
def test_idempotency_skips_superseded_snapshot(
    sub5_migrated_database: str, tmp_path: pathlib.Path
) -> None:
    """(F6) A registration whose identity triple matches a SUPERSEDED historical
    row MUST NOT return the historical UUID; it must write a NEW row.
    Guards `find_snapshot_by_identity`'s `superseded_at IS NULL` filter."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    grid_json, sidecar, uri = _write_unique_fixture(tmp_path, suffix="supersede_skip")
    record = read_input_record(
        "IFS",
        grid_json,
        sidecar,
        grid_definition_uri=uri,
        expected_converter_version=_stub_converter_resolver,
    )
    store = PsycopgGridRegistryStore(database_url=sub5_migrated_database)
    first_id = register_snapshot(record, source_id="IFS", store=store)
    store.supersede(first_id, _dt.now(_UTC))

    # Same identity triple as before but the historical row is superseded — a
    # second register_snapshot must write a NEW row, not return `first_id`.
    second_id = register_snapshot(record, source_id="IFS", store=store)
    assert second_id != first_id

    connection = psycopg2.connect(sub5_migrated_database)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM met.canonical_grid_snapshot
                WHERE source_id = %s AND grid_id = %s
                """,
                ("IFS", record.grid_id),
            )
            assert cursor.fetchone()[0] == 2
    finally:
        connection.close()


@pytest.mark.integration
def test_writer_concurrent_registration_produces_single_row(
    sub5_migrated_database: str, tmp_path: pathlib.Path
) -> None:
    """(F3) Two threads registering the same identity triple concurrently MUST
    produce exactly ONE row — the DB-level partial UNIQUE index (000044) is
    the concurrency backstop; the writer catches the resulting store error
    and re-reads the winning row for read-your-writes idempotency."""
    # Randomize per-run so a stale row from a prior pytest invocation against
    # a persistent test DB cannot short-circuit both threads through
    # find_snapshot_by_identity and skip the race path entirely.
    grid_json, sidecar, uri = _write_unique_fixture(
        tmp_path, suffix=f"concurrent_{uuid.uuid4().hex[:8]}"
    )
    record = read_input_record(
        "IFS",
        grid_json,
        sidecar,
        grid_definition_uri=uri,
        expected_converter_version=_stub_converter_resolver,
    )
    store = PsycopgGridRegistryStore(database_url=sub5_migrated_database)

    results: list[uuid.UUID | Exception] = [None, None]  # type: ignore[list-item]
    barrier = threading.Barrier(2)

    def _worker(slot: int) -> None:
        try:
            barrier.wait(timeout=5.0)
            results[slot] = register_snapshot(record, source_id="IFS", store=store)
        except Exception as error:  # noqa: BLE001 — thread capture
            results[slot] = error

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30.0)

    for slot, result in enumerate(results):
        assert isinstance(result, uuid.UUID), (
            f"thread {slot} did not return a UUID: {result!r}"
        )
    # Both threads should have converged on the same winning UUID.
    assert results[0] == results[1]

    connection = psycopg2.connect(sub5_migrated_database)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM met.canonical_grid_snapshot
                WHERE source_id = %s AND grid_id = %s
                """,
                ("IFS", record.grid_id),
            )
            assert cursor.fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.integration
def test_writer_race_fallback_returns_winner_on_unique_violation(
    sub5_migrated_database: str, tmp_path: pathlib.Path
) -> None:
    """When a real active row exists for the identity triple, a monkeypatched
    `RegistryUniqueViolationError` from `insert_snapshot` MUST trigger the
    fallback re-query and return the seeded UUID (read-your-writes). Pairs
    with the pure-Python narrowing test to prove the fallback path lives on
    exactly SQLSTATE 23505."""
    grid_json, sidecar, uri = _write_unique_fixture(
        tmp_path, suffix=f"race_fallback_{uuid.uuid4().hex[:8]}"
    )
    record = read_input_record(
        "IFS",
        grid_json,
        sidecar,
        grid_definition_uri=uri,
        expected_converter_version=_stub_converter_resolver,
    )
    store = PsycopgGridRegistryStore(database_url=sub5_migrated_database)
    # Seed the active row the fallback re-query is expected to find.
    seeded_id = register_snapshot(record, source_id="IFS", store=store)

    # Bypass the pre-check idempotency once so the writer reaches the insert
    # path; the monkeypatched insert then raises UniqueViolation and the
    # subsequent fallback re-query hits the real DB and returns `seeded_id`.
    original_find = store.find_snapshot_by_identity
    find_calls = {"n": 0}

    def find_bypass_first(
        source_id: str, grid_id: str, grid_signature: str
    ) -> uuid.UUID | None:
        find_calls["n"] += 1
        if find_calls["n"] == 1:
            return None  # force writer past the pre-check
        return original_find(source_id, grid_id, grid_signature)

    def raising_insert(*_a: Any, **_kw: Any) -> uuid.UUID:
        raise RegistryUniqueViolationError(
            "simulated 23505 unique constraint violation"
        )

    # Frozen dataclass — `monkeypatch.setattr` raises FrozenInstanceError, so
    # bypass via object.__setattr__ (the store instance is scoped to this
    # test only; no cleanup needed).
    object.__setattr__(store, "find_snapshot_by_identity", find_bypass_first)
    object.__setattr__(store, "insert_snapshot", raising_insert)

    returned_id = register_snapshot(record, source_id="IFS", store=store)
    assert returned_id == seeded_id
    # Two calls total: pre-check (skipped) + fallback (returned seeded_id).
    assert find_calls["n"] == 2


@pytest.mark.integration
def test_cli_stdout_contains_only_uuid(
    sub5_migrated_database: str, tmp_path: pathlib.Path
) -> None:
    """(F8) On success the CLI MUST print exactly one line of stdout containing
    only the inserted UUID — no diagnostic noise, no trailing whitespace beyond
    the one newline. Consumers relying on stdout to capture the id must not
    have to parse noise."""
    grid_json, sidecar, _uri = _write_unique_fixture(tmp_path, suffix="cli_stdout")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "workers.grid_registry",
            "--source-id",
            "ifs",
            "--grid-json",
            str(grid_json),
            "--sidecar",
            str(sidecar),
            "--grid-definition-uri",
            "canonical/IFS/grid/cli_stdout/grid.json",
            "--database-url",
            sub5_migrated_database,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"CLI exited nonzero: stdout={proc.stdout!r}, stderr={proc.stderr!r}"
    )
    # Exactly one line + trailing newline.
    assert proc.stdout.count("\n") == 1, f"unexpected stdout shape: {proc.stdout!r}"
    # The line parses as a UUID.
    parsed = uuid.UUID(proc.stdout.strip())
    assert isinstance(parsed, uuid.UUID)
