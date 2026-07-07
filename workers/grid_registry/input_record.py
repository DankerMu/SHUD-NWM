"""Registry input-record contract (Task 3.1a).

Reads a canonical ``grid.json`` (at ``canonical/{source}/grid/{grid_id}/
grid.json``) merged with a sidecar ``grid_snapshot_metadata.json`` (validated
against :file:`schemas/grid_snapshot_metadata.schema.json`) and returns a
frozen :class:`GridSnapshotInputRecord` that the Task 3.1b writer consumes.

All fail-closed cases raise :class:`GridSnapshotInputError` (or a subclass)
with a ``field`` attribute naming the offending JSON key, so downstream
callers can write ``except GridSnapshotInputError`` handlers.

This module does not touch the database, the object store, or ``.sp.att``
files. It only reads two JSON files, validates them, and returns the merged
input record.
"""

from __future__ import annotations

import json
import math
import pathlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import numpy as np

from packages.common.object_store import sha256_bytes

# The default schema file that ships with the repo. Callers may inject a
# different path for testing, but production callers should never override it.
DEFAULT_SIDECAR_SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "schemas"
    / "grid_snapshot_metadata.schema.json"
)

_AXIS_SPACING_TOL = 1e-9
_BBOX_TOL = 1e-6
_REQUIRED_BBOX_KEYS = frozenset({"south", "north", "west", "east"})


# -----------------------------------------------------------------------------
# Exception hierarchy
# -----------------------------------------------------------------------------


class GridSnapshotInputError(Exception):
    """Base for all fail-closed input-record errors.

    ``field`` names the offending JSON key (or logical field) so a downstream
    ``except GridSnapshotInputError`` handler can log which shape caused the
    rejection without parsing the message string.
    """

    def __init__(self, field: str, message: str) -> None:
        self.field = field
        super().__init__(message)


class MissingSidecarError(GridSnapshotInputError):
    """Raised when the sidecar JSON file does not exist on disk."""


class SidecarSchemaError(GridSnapshotInputError):
    """Raised when sidecar bytes fail the JSON Schema contract."""


class NonRectilinearLayoutError(GridSnapshotInputError):
    """Raised when ``grid.json`` layout is missing or not ``"rectilinear"``."""


class NonMonotonicLatitudeError(GridSnapshotInputError):
    """Raised when latitudes are neither strictly ascending nor descending."""


class AxisOrderError(GridSnapshotInputError):
    """Raised when ``axis_order`` is missing or not ``["latitude", "longitude"]``."""


class NaNAxisValueError(GridSnapshotInputError):
    """Raised when longitudes or latitudes contain ``NaN`` or ``inf``."""


class AxisSpacingMismatchError(GridSnapshotInputError):
    """Raised when lon spacing does not equal lat spacing within tolerance."""


class InvalidBboxRectangleError(GridSnapshotInputError):
    """Raised when the sidecar bbox has ``south >= north`` or ``west >= east``."""


class InvalidValidityWindowError(GridSnapshotInputError):
    """Raised when ``valid_to`` is not strictly after ``valid_from``."""


class BboxAxisDisagreementError(GridSnapshotInputError):
    """Raised when the sidecar bbox drifts from ``grid.json`` axis outer edges."""


class MalformedJsonError(GridSnapshotInputError):
    """Raised when a JSON file (grid.json, sidecar, or schema) cannot be decoded.

    Wraps :class:`json.JSONDecodeError` so downstream callers writing
    ``except GridSnapshotInputError`` handlers do not crash on BOM-prefixed,
    truncated, or otherwise malformed JSON bytes.
    """


class InsufficientAxisPointsError(GridSnapshotInputError):
    """Raised when an axis has fewer than two values.

    A single-point axis is not a monotonicity failure or a spacing failure — it
    is an insufficient-input failure that must be surfaced distinctly so
    downstream handlers filtering on exception class are not misled.
    """


class MissingGridDefinitionFieldError(GridSnapshotInputError):
    """Raised when ``grid.json`` omits a required identity field.

    Distinct from :class:`NonRectilinearLayoutError` so downstream callers can
    catch missing ``grid_id`` / ``schema_version`` narrowly without swallowing
    layout / axis failures.
    """


# -----------------------------------------------------------------------------
# Records
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class CellInput:
    """One row in the ordered per-cell input list.

    ``grid_cell_id`` matches the producer's ``str(index)`` convention at
    ``workers/forcing_producer/producer.py:1472`` (y-outer / x-inner iteration).
    ``canonical_ordinal`` is ``index + 1``; both are recorded so the Task 3.1b
    writer never re-derives the ordinal from the string.
    """

    grid_cell_id: str
    canonical_ordinal: int
    longitude: float
    latitude: float


@dataclass(frozen=True)
class GridSnapshotInputRecord:
    """Merged read of one snapshot's grid.json + sidecar metadata.

    Fields sourced from ``grid.json``:
        ``schema_version``, ``grid_id``, ``layout``, ``axis_order``, ``shape``,
        ``longitudes``, ``latitudes``.

    Fields derived from ``grid.json`` at read time:
        ``converter_version`` (from ``expected_converter_version(source_id)``),
        ``native_resolution``, ``latitude_order``, ``flatten_order``,
        ``grid_definition_checksum`` (SHA-256 of the exact bytes parsed, so the
        Task 3.1b writer never needs to re-open ``grid.json`` and open a TOCTOU
        window with content drift between the input read and the checksum
        compute).

    Fields sourced from sidecar:
        ``valid_from``, ``valid_to``, ``download_bbox``.

    Fields supplied by the caller (Task 3.1b registry writer / SUB-5 CLI):
        ``grid_definition_uri`` — the ``canonical/{source}/grid/{grid_id}/
        grid.json`` URI the caller derives; the reader does not synthesize it.

    Fields computed in the fixed flatten order (y outer / x inner):
        ``cells``.
    """

    schema_version: str
    grid_id: str
    layout: Literal["rectilinear"]
    axis_order: tuple[str, ...]
    shape: tuple[int, int]
    longitudes: tuple[float, ...]
    latitudes: tuple[float, ...]
    converter_version: str
    native_resolution: float
    latitude_order: Literal["ascending", "descending"]
    flatten_order: Literal["y_major_lat_then_lon"]
    valid_from: datetime
    valid_to: datetime | None
    download_bbox: Mapping[str, float]
    grid_definition_uri: str
    grid_definition_checksum: str
    cells: tuple[CellInput, ...]


# -----------------------------------------------------------------------------
# Reader
# -----------------------------------------------------------------------------


def read_input_record(
    source_id: str,
    grid_json_path: pathlib.Path,
    sidecar_path: pathlib.Path,
    *,
    grid_definition_uri: str,
    schema_path: pathlib.Path | None = None,
    expected_converter_version: Callable[[str], str] | None = None,
) -> GridSnapshotInputRecord:
    """Read grid.json + sidecar and return a validated input record.

    ``grid_definition_uri`` is required so the SUB-5 CLI can pass the URI
    derived from the ``canonical/{source}/grid/{grid_id}/grid.json`` convention;
    the reader computes ``grid_definition_checksum`` from the exact bytes parsed
    (no re-open) to close the TOCTOU window between input read and checksum
    compute at the Task 3.1b writer boundary.

    All fail-closed contract violations raise a subclass of
    :class:`GridSnapshotInputError`.
    """
    grid_payload, grid_raw_bytes = _read_grid_json(grid_json_path)
    grid_definition_checksum = sha256_bytes(grid_raw_bytes)

    if grid_payload.get("schema_version") is None:
        raise MissingGridDefinitionFieldError(
            "schema_version",
            f"grid.json at {grid_json_path} is missing required key 'schema_version'.",
        )
    if grid_payload.get("grid_id") is None:
        raise MissingGridDefinitionFieldError(
            "grid_id",
            f"grid.json at {grid_json_path} is missing required key 'grid_id'.",
        )

    layout = _extract_layout(grid_payload)
    axis_order = _extract_axis_order(grid_payload)
    longitudes = _extract_axis(grid_payload, "longitudes")
    latitudes = _extract_axis(grid_payload, "latitudes")
    shape = _extract_shape(grid_payload, longitudes=longitudes, latitudes=latitudes)
    latitude_order = _derive_latitude_order(latitudes)
    native_resolution = _derive_native_resolution(longitudes, latitudes)

    schema = _load_schema(schema_path)
    sidecar_payload = _read_sidecar(sidecar_path)
    _validate_sidecar_against_schema(sidecar_payload, schema=schema)

    valid_from = _parse_iso_datetime(sidecar_payload["valid_from"], field="valid_from")
    valid_to = None
    if sidecar_payload.get("valid_to") is not None:
        valid_to = _parse_iso_datetime(sidecar_payload["valid_to"], field="valid_to")
    if valid_to is not None and not (valid_to > valid_from):
        raise InvalidValidityWindowError(
            "valid_to",
            f"valid_to ({valid_to.isoformat()}) must be strictly after valid_from "
            f"({valid_from.isoformat()}).",
        )

    download_bbox = _validate_download_bbox(sidecar_payload["download_bbox"])
    _cross_check_bbox_against_axes(download_bbox, longitudes=longitudes, latitudes=latitudes)

    resolver = expected_converter_version
    if resolver is None:
        # Deferred import so tests that stub out the resolver do not force a
        # heavy import of workers.canonical_converter.
        from workers.canonical_converter.converter import (
            expected_converter_version as default_resolver,
        )

        resolver = default_resolver
    converter_version = resolver(source_id)

    cells = _build_cells(longitudes=longitudes, latitudes=latitudes)

    return GridSnapshotInputRecord(
        schema_version=str(grid_payload.get("schema_version", "")),
        grid_id=str(grid_payload.get("grid_id", "")),
        layout=layout,
        axis_order=axis_order,
        shape=shape,
        longitudes=longitudes,
        latitudes=latitudes,
        converter_version=str(converter_version),
        native_resolution=native_resolution,
        latitude_order=latitude_order,
        flatten_order="y_major_lat_then_lon",
        valid_from=valid_from,
        valid_to=valid_to,
        download_bbox=download_bbox,
        grid_definition_uri=grid_definition_uri,
        grid_definition_checksum=grid_definition_checksum,
        cells=cells,
    )


# -----------------------------------------------------------------------------
# grid.json helpers
# -----------------------------------------------------------------------------


def _read_grid_json(grid_json_path: pathlib.Path) -> tuple[Mapping[str, object], bytes]:
    """Return ``(parsed_payload, raw_bytes)`` so callers can hash the exact
    bytes that were parsed and avoid TOCTOU between read and checksum."""
    raw_bytes = grid_json_path.read_bytes()
    try:
        payload = json.loads(raw_bytes)
    except json.JSONDecodeError as err:
        raise MalformedJsonError(
            "grid.json",
            f"grid.json at {grid_json_path} is not valid JSON: {err}.",
        ) from err
    if not isinstance(payload, Mapping):
        raise NonRectilinearLayoutError(
            "layout",
            f"grid.json at {grid_json_path} must decode to a JSON object; got {type(payload).__name__}.",
        )
    return payload, raw_bytes


def _extract_layout(payload: Mapping[str, object]) -> Literal["rectilinear"]:
    layout = payload.get("layout")
    if layout != "rectilinear":
        raise NonRectilinearLayoutError(
            "layout",
            f"grid.json layout must be 'rectilinear'; got {layout!r}.",
        )
    return "rectilinear"


def _extract_axis_order(payload: Mapping[str, object]) -> tuple[str, ...]:
    raw = payload.get("axis_order")
    if raw is None:
        raise AxisOrderError(
            "axis_order",
            "grid.json is missing required key 'axis_order'.",
        )
    if not isinstance(raw, list):
        raise AxisOrderError(
            "axis_order",
            f"grid.json 'axis_order' must be a list; got type {type(raw).__name__}.",
        )
    axis_order = tuple(str(value) for value in raw)
    if axis_order != ("latitude", "longitude"):
        raise AxisOrderError(
            "axis_order",
            f"grid.json 'axis_order' must be ['latitude', 'longitude']; got {list(axis_order)!r}.",
        )
    return axis_order


def _extract_axis(payload: Mapping[str, object], key: str) -> tuple[float, ...]:
    raw = payload.get(key)
    if raw is None:
        raise NonRectilinearLayoutError(
            key,
            f"grid.json is missing required key {key!r}.",
        )
    if not isinstance(raw, list):
        raise NonRectilinearLayoutError(
            key,
            f"grid.json {key!r} must be a list; got type {type(raw).__name__}.",
        )
    values: list[float] = []
    for entry in raw:
        try:
            value = float(entry)
        except (TypeError, ValueError) as error:
            raise NaNAxisValueError(
                key,
                f"grid.json {key!r} contains non-numeric entry {entry!r}: {error}.",
            ) from error
        if math.isnan(value) or math.isinf(value):
            raise NaNAxisValueError(
                key,
                f"grid.json {key!r} contains non-finite entry {entry!r}.",
            )
        values.append(value)
    return tuple(values)


def _extract_shape(
    payload: Mapping[str, object],
    *,
    longitudes: tuple[float, ...],
    latitudes: tuple[float, ...],
) -> tuple[int, int]:
    raw = payload.get("shape")
    if raw is None:
        raise NonRectilinearLayoutError(
            "shape",
            "grid.json is missing required key 'shape'.",
        )
    if not isinstance(raw, list) or len(raw) != 2:
        raise NonRectilinearLayoutError(
            "shape",
            f"grid.json 'shape' must be a two-element list [y_count, x_count]; got {raw!r}.",
        )
    try:
        y_count, x_count = int(raw[0]), int(raw[1])
    except (TypeError, ValueError) as error:
        raise NonRectilinearLayoutError(
            "shape",
            f"grid.json 'shape' entries must be integers; got {raw!r}: {error}.",
        ) from error
    if y_count != len(latitudes) or x_count != len(longitudes):
        raise NonRectilinearLayoutError(
            "shape",
            f"grid.json 'shape' [{y_count}, {x_count}] does not match "
            f"(len(latitudes)={len(latitudes)}, len(longitudes)={len(longitudes)}).",
        )
    return (y_count, x_count)


def _derive_latitude_order(latitudes: tuple[float, ...]) -> Literal["ascending", "descending"]:
    if len(latitudes) < 2:
        raise InsufficientAxisPointsError(
            "latitudes",
            f"latitudes must contain at least two values to derive an order; got {len(latitudes)}.",
        )
    diffs = np.diff(latitudes)
    if bool(np.all(diffs > 0)):
        return "ascending"
    if bool(np.all(diffs < 0)):
        return "descending"
    raise NonMonotonicLatitudeError(
        "latitudes",
        "latitudes must be strictly ascending or strictly descending; found a non-monotonic sequence.",
    )


def _derive_native_resolution(
    longitudes: tuple[float, ...],
    latitudes: tuple[float, ...],
) -> float:
    if len(longitudes) < 2:
        raise InsufficientAxisPointsError(
            "longitudes",
            "longitudes must contain at least two values to derive native_resolution.",
        )
    if len(latitudes) < 2:
        raise InsufficientAxisPointsError(
            "latitudes",
            "latitudes must contain at least two values to derive native_resolution.",
        )
    lon_res = float(np.median(np.abs(np.diff(longitudes))))
    lat_res = float(np.median(np.abs(np.diff(latitudes))))
    if abs(lon_res - lat_res) > _AXIS_SPACING_TOL:
        raise AxisSpacingMismatchError(
            "native_resolution",
            f"Longitude spacing ({lon_res}) disagrees with latitude spacing ({lat_res}) "
            f"beyond rectilinear tolerance {_AXIS_SPACING_TOL}.",
        )
    return lon_res


# -----------------------------------------------------------------------------
# Sidecar helpers
# -----------------------------------------------------------------------------


def _load_schema(schema_path: pathlib.Path | None) -> Mapping[str, object]:
    path = schema_path if schema_path is not None else DEFAULT_SIDECAR_SCHEMA_PATH
    with path.open("r", encoding="utf-8") as handle:
        try:
            schema = json.load(handle)
        except json.JSONDecodeError as err:
            raise MalformedJsonError(
                "schema",
                f"Sidecar schema at {path} is not valid JSON: {err}.",
            ) from err
    if not isinstance(schema, Mapping):
        raise SidecarSchemaError(
            "schema",
            f"Sidecar schema at {path} must decode to a JSON object; got {type(schema).__name__}.",
        )
    return schema


def _read_sidecar(sidecar_path: pathlib.Path) -> Mapping[str, object]:
    if not sidecar_path.exists():
        raise MissingSidecarError(
            "sidecar_path",
            f"Sidecar metadata file not found at {sidecar_path}. Author "
            "grid_snapshot_metadata.json before registration.",
        )
    with sidecar_path.open("r", encoding="utf-8") as handle:
        try:
            payload = json.load(handle)
        except json.JSONDecodeError as err:
            raise MalformedJsonError(
                "sidecar",
                f"Sidecar at {sidecar_path} is not valid JSON: {err}.",
            ) from err
    if not isinstance(payload, Mapping):
        raise SidecarSchemaError(
            "sidecar",
            f"Sidecar at {sidecar_path} must decode to a JSON object; got {type(payload).__name__}.",
        )
    return payload


def _validate_sidecar_against_schema(
    sidecar_payload: Mapping[str, object],
    *,
    schema: Mapping[str, object],
) -> None:
    """Manual draft-2020-12 validation of the pinned schema shape.

    The stdlib ships no JSON Schema validator and ``jsonschema`` is not a
    project dependency; the schema is small and pinned, so we mirror its
    contract by hand. The four rejection modes we enforce (from the schema at
    :file:`schemas/grid_snapshot_metadata.schema.json`) are:

    1. missing required top-level key (``valid_from`` / ``valid_to`` /
       ``download_bbox``);
    2. extra top-level key (``additionalProperties: false``);
    3. wrong type on a top-level key (e.g. ``valid_from`` as an int);
    4. ``download_bbox`` shape violations (missing key, extra key, wrong type).

    Timezone-offset absence on ``valid_from`` / ``valid_to`` is caught in
    :func:`_parse_iso_datetime`, not here — the draft-2020-12
    ``format: date-time`` production is annotation-only by default.
    """
    schema_required = _string_list(schema.get("required"), field="schema.required")
    schema_properties = schema.get("properties", {})
    if not isinstance(schema_properties, Mapping):
        raise SidecarSchemaError(
            "schema.properties",
            "Sidecar schema 'properties' must be a mapping.",
        )
    # (1) required keys present
    for key in schema_required:
        if key not in sidecar_payload:
            raise SidecarSchemaError(
                key,
                f"Sidecar is missing required key {key!r}.",
            )
    # (2) additionalProperties: false at top level
    allowed_keys = set(schema_properties.keys())
    for key in sidecar_payload.keys():
        if key not in allowed_keys:
            raise SidecarSchemaError(
                key,
                f"Sidecar has unexpected top-level key {key!r}; allowed keys are "
                f"{sorted(allowed_keys)}.",
            )
    # (3) type per top-level key
    for key, property_schema in schema_properties.items():
        if key not in sidecar_payload:
            continue
        if not isinstance(property_schema, Mapping):
            continue
        expected_types = _property_types(property_schema)
        if not _matches_type(sidecar_payload[key], expected_types):
            raise SidecarSchemaError(
                key,
                f"Sidecar field {key!r} must have type {expected_types}; got "
                f"{type(sidecar_payload[key]).__name__}.",
            )
    # (4) download_bbox nested shape
    bbox_schema = schema_properties.get("download_bbox")
    if isinstance(bbox_schema, Mapping):
        _validate_bbox_object_against_schema(
            sidecar_payload.get("download_bbox"),
            bbox_schema=bbox_schema,
        )


def _string_list(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SidecarSchemaError(field, f"{field} must be a list; got {type(value).__name__}.")
    return tuple(str(entry) for entry in value)


def _property_types(property_schema: Mapping[str, object]) -> tuple[str, ...]:
    raw = property_schema.get("type")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(str(entry) for entry in raw)
    return ()


def _matches_type(value: object, expected_types: tuple[str, ...]) -> bool:
    if not expected_types:
        return True
    for expected in expected_types:
        if _value_matches_json_type(value, expected):
            return True
    return False


def _value_matches_json_type(value: object, json_type: str) -> bool:
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "null":
        return value is None
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "object":
        return isinstance(value, Mapping)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "boolean":
        return isinstance(value, bool)
    return False


def _validate_bbox_object_against_schema(
    value: object,
    *,
    bbox_schema: Mapping[str, object],
) -> None:
    if not isinstance(value, Mapping):
        raise SidecarSchemaError(
            "download_bbox",
            f"Sidecar 'download_bbox' must be a JSON object; got {type(value).__name__}.",
        )
    required = _string_list(bbox_schema.get("required", []), field="schema.download_bbox.required")
    properties = bbox_schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise SidecarSchemaError(
            "download_bbox.properties",
            "Sidecar schema 'download_bbox.properties' must be a mapping.",
        )
    for key in required:
        if key not in value:
            raise SidecarSchemaError(
                key,
                f"Sidecar 'download_bbox' is missing required key {key!r}.",
            )
    allowed = set(properties.keys())
    for key in value.keys():
        if key not in allowed:
            raise SidecarSchemaError(
                key,
                f"Sidecar 'download_bbox' has unexpected key {key!r}; allowed keys are "
                f"{sorted(allowed)}.",
            )
    for key, property_schema in properties.items():
        if key not in value:
            continue
        if not isinstance(property_schema, Mapping):
            continue
        expected_types = _property_types(property_schema)
        if not _matches_type(value[key], expected_types):
            raise SidecarSchemaError(
                key,
                f"Sidecar 'download_bbox.{key}' must have type {expected_types}; got "
                f"{type(value[key]).__name__}.",
            )


def _parse_iso_datetime(raw: object, *, field: str) -> datetime:
    if not isinstance(raw, str):
        raise SidecarSchemaError(
            field,
            f"Sidecar field {field!r} must be an ISO8601 string; got type {type(raw).__name__}.",
        )
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise SidecarSchemaError(
            field,
            f"Sidecar field {field!r} is not a valid ISO8601 timestamp: {error}.",
        ) from error
    if parsed.tzinfo is None:
        raise SidecarSchemaError(
            field,
            f"Sidecar field {field!r} must include a timezone offset; got naive datetime {raw!r}.",
        )
    return parsed


def _validate_download_bbox(raw: object) -> Mapping[str, float]:
    if not isinstance(raw, Mapping):
        raise SidecarSchemaError(
            "download_bbox",
            f"Sidecar 'download_bbox' must be a JSON object; got {type(raw).__name__}.",
        )
    provided_keys = set(raw.keys())
    missing = _REQUIRED_BBOX_KEYS - provided_keys
    if missing:
        offending = sorted(missing)[0]
        raise SidecarSchemaError(
            offending,
            f"Sidecar 'download_bbox' is missing required key {offending!r}.",
        )
    extra = provided_keys - _REQUIRED_BBOX_KEYS
    if extra:
        offending = sorted(extra)[0]
        raise SidecarSchemaError(
            offending,
            f"Sidecar 'download_bbox' has unexpected key {offending!r}.",
        )
    values: dict[str, float] = {}
    for key in _REQUIRED_BBOX_KEYS:
        entry = raw[key]
        if isinstance(entry, bool) or not isinstance(entry, (int, float)):
            raise SidecarSchemaError(
                key,
                f"Sidecar 'download_bbox.{key}' must be a number; got {type(entry).__name__}.",
            )
        value = float(entry)
        if math.isnan(value) or math.isinf(value):
            raise SidecarSchemaError(
                key,
                f"Sidecar 'download_bbox.{key}' must be finite; got {entry!r}.",
            )
        values[key] = value
    if values["south"] >= values["north"]:
        raise InvalidBboxRectangleError(
            "download_bbox",
            f"Sidecar 'download_bbox' has south ({values['south']}) >= north "
            f"({values['north']}).",
        )
    if values["west"] >= values["east"]:
        raise InvalidBboxRectangleError(
            "download_bbox",
            f"Sidecar 'download_bbox' has west ({values['west']}) >= east "
            f"({values['east']}).",
        )
    return values


def _cross_check_bbox_against_axes(
    bbox: Mapping[str, float],
    *,
    longitudes: tuple[float, ...],
    latitudes: tuple[float, ...],
) -> None:
    axis_south = min(latitudes)
    axis_north = max(latitudes)
    axis_west = min(longitudes)
    axis_east = max(longitudes)
    checks = (
        ("south", bbox["south"], axis_south),
        ("north", bbox["north"], axis_north),
        ("west", bbox["west"], axis_west),
        ("east", bbox["east"], axis_east),
    )
    for corner, sidecar_value, axis_value in checks:
        if abs(sidecar_value - axis_value) > _BBOX_TOL:
            raise BboxAxisDisagreementError(
                corner,
                f"Sidecar 'download_bbox.{corner}' ({sidecar_value}) disagrees with "
                f"grid.json axis outer edge ({axis_value}) beyond tolerance {_BBOX_TOL}.",
            )


# -----------------------------------------------------------------------------
# Cell list
# -----------------------------------------------------------------------------


def _build_cells(
    *,
    longitudes: tuple[float, ...],
    latitudes: tuple[float, ...],
) -> tuple[CellInput, ...]:
    """Compute the ordered cell list matching producer.py:1470-1479 byte-for-byte."""
    from workers.forcing_producer.producer import _normalize_longitude

    cells: list[CellInput] = []
    for index, (latitude, longitude) in enumerate(
        (lat, lon) for lat in latitudes for lon in longitudes
    ):
        cells.append(
            CellInput(
                grid_cell_id=str(index),
                canonical_ordinal=index + 1,
                longitude=_normalize_longitude(longitude),
                latitude=latitude,
            )
        )
    return tuple(cells)
