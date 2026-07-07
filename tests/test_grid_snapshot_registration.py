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

import inspect
import json
import math
import pathlib
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from packages.common.canonical_grid_key import derive_canonical_grid_key
from packages.common.object_store import sha256_bytes
from workers.forcing_producer.producer import (
    CanonicalProduct,
    ForcingProducer,
    ForcingProducerConfig,
    _normalize_longitude,
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
