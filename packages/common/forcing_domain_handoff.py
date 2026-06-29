from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.redaction import is_sensitive_key, redact_payload, redact_text
from packages.common.storage import validate_object_path

CONTRACT_ID = "nhms.forcing_domain_handoff.v1"
PACKAGE_CONTRACT_ID = "nhms.forcing_domain_handoff.package.v1"
SCHEMA_VERSION = "1.0"
MAX_HANDOFF_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_HANDOFF_PAYLOAD_BYTES = 64 * 1024 * 1024

FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD = "forcing_domain_package_manifest_uri"
FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD = "forcing_domain_package_manifest_checksum_sha256"
FORCING_PACKAGE_MANIFEST_URI_FIELD = "forcing_package_manifest_uri"
FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD = "forcing_package_manifest_checksum_sha256"

REASON_FIELD_MISSING = "HANDOFF_FIELD_MISSING"
REASON_IDENTITY_FIELD_MISSING = "HANDOFF_IDENTITY_FIELD_MISSING"
REASON_IDENTITY_MISMATCH = "HANDOFF_IDENTITY_MISMATCH"
REASON_TEMPORAL_FIELD_MISSING = "HANDOFF_TEMPORAL_FIELD_MISSING"
REASON_TEMPORAL_FIELD_MALFORMED = "HANDOFF_TEMPORAL_FIELD_MALFORMED"
REASON_TEMPORAL_WINDOW_INVALID = "HANDOFF_TEMPORAL_WINDOW_INVALID"
REASON_PACKAGE_MISSING = "HANDOFF_PACKAGE_MISSING"
REASON_PACKAGE_CHECKSUM_MISSING = "HANDOFF_PACKAGE_CHECKSUM_MISSING"
REASON_PACKAGE_CHECKSUM_MISMATCH = "HANDOFF_PACKAGE_CHECKSUM_MISMATCH"
REASON_PACKAGE_PATH_UNSAFE = "HANDOFF_PACKAGE_PATH_UNSAFE"
REASON_PACKAGE_MANIFEST_MALFORMED = "HANDOFF_PACKAGE_MANIFEST_MALFORMED"
REASON_PAYLOAD_MISSING = "HANDOFF_PAYLOAD_MISSING"
REASON_PAYLOAD_CHECKSUM_MISSING = "HANDOFF_PAYLOAD_CHECKSUM_MISSING"
REASON_PAYLOAD_CHECKSUM_MISMATCH = "HANDOFF_PAYLOAD_CHECKSUM_MISMATCH"
REASON_PAYLOAD_MALFORMED = "HANDOFF_PAYLOAD_MALFORMED"
REASON_PAYLOAD_PATH_UNSAFE = "HANDOFF_PAYLOAD_PATH_UNSAFE"
REASON_PAYLOAD_OUTSIDE_PACKAGE = "HANDOFF_PAYLOAD_OUTSIDE_PACKAGE"
REASON_ROW_COUNT_MISSING = "HANDOFF_ROW_COUNT_MISSING"
REASON_ROW_COUNT_MISMATCH = "HANDOFF_ROW_COUNT_MISMATCH"
REASON_STATION_COUNT_MISMATCH = "HANDOFF_STATION_COUNT_MISMATCH"
REASON_STATION_INVENTORY_DUPLICATE = "HANDOFF_STATION_INVENTORY_DUPLICATE"
REASON_STATION_TIMESERIES_VARIABLE_DUPLICATE = "HANDOFF_STATION_TIMESERIES_VARIABLE_DUPLICATE"
REASON_INTERP_WEIGHT_DUPLICATE = "HANDOFF_INTERP_WEIGHT_DUPLICATE"
REASON_COMPATIBILITY_URI_UNSAFE = "HANDOFF_COMPATIBILITY_URI_UNSAFE"
REASON_COMPATIBILITY_URI_MISMATCH = "HANDOFF_COMPATIBILITY_URI_MISMATCH"
REASON_TIMESERIES_LATTICE_MISSING = "HANDOFF_TIMESERIES_LATTICE_MISSING"
REASON_TIMESERIES_LATTICE_EXTRA = "HANDOFF_TIMESERIES_LATTICE_EXTRA"
REASON_TIMESERIES_LATTICE_DUPLICATE = "HANDOFF_TIMESERIES_LATTICE_DUPLICATE"
REASON_TIMESERIES_LATTICE_TOO_LARGE = "HANDOFF_TIMESERIES_LATTICE_TOO_LARGE"
REASON_OBJECT_STORE_ROOT_UNAVAILABLE = "HANDOFF_OBJECT_STORE_ROOT_UNAVAILABLE"
REASON_MANIFEST_UNREADABLE = "HANDOFF_MANIFEST_UNREADABLE"
REASON_MANIFEST_MALFORMED = "HANDOFF_MANIFEST_MALFORMED"

MAX_TIMESERIES_LATTICE_TUPLES = 5_000_000
MAX_TIMESERIES_LATTICE_TIME_POINTS = 250_000
MAX_LATTICE_SAMPLES = 5
MAX_ROW_DIAGNOSTIC_SAMPLES = 5

TimeLatticeSegment = tuple[frozenset[str] | None, datetime, datetime, str, timedelta]

IDENTITY_FIELDS = (
    "run_id",
    "source_id",
    "source",
    "model_id",
    "basin_id",
    "basin_version_id",
    "forcing_version_id",
    "scenario_id",
)
TEMPORAL_FIELDS = ("cycle_time", "start_time", "end_time")
COMPATIBILITY_URI_FIELDS = (
    "model_package_uri",
    "forcing_uri",
    "forcing_package_uri",
    FORCING_PACKAGE_MANIFEST_URI_FIELD,
    FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD,
    "run_manifest_uri",
    "output_uri",
)
TABLE_ROW_COUNT_FIELDS = (
    "met.forcing_version",
    "met.met_station",
    "met.forcing_station_timeseries",
    "met.interp_weight",
)
PAYLOAD_TABLES = {
    "station_inventory": "met.met_station",
    "station_timeseries": "met.forcing_station_timeseries",
    "interpolation_weights": "met.interp_weight",
}
PARSED_PAYLOAD_TABLE_ROW_FIELDS = {
    "station_inventory": (
        "station_id",
        "basin_version_id",
        "station_name",
        "longitude",
        "latitude",
        "geometry",
        "elevation_m",
        "station_role",
        "active_flag",
        "properties_json",
    ),
    "station_timeseries": (
        "forcing_version_id",
        "basin_version_id",
        "station_id",
        "valid_time",
        "source_id",
        "variable",
        "value",
        "unit",
        "native_resolution",
        "quality_flag",
    ),
    "interpolation_weights": (
        "source_id",
        "grid_id",
        "model_id",
        "station_id",
        "variable",
        "grid_cell_id",
        "weight",
        "method",
        "grid_signature",
    ),
}
PARSER_REQUIRED_PAYLOAD_ROW_FIELDS = {
    "station_inventory": (
        "station_id",
        "basin_version_id",
        "station_name",
        "elevation_m",
        "station_role",
        "active_flag",
        "properties_json",
    ),
    "station_timeseries": PARSED_PAYLOAD_TABLE_ROW_FIELDS["station_timeseries"],
    "interpolation_weights": (
        "source_id",
        "grid_id",
        "model_id",
        "station_id",
        "variable",
        "grid_cell_id",
        "weight",
        "method",
    ),
}
PARSER_BUSINESS_SIGNATURE_KEYS = frozenset({"grid_signature", "source_grid_signature"})

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def validate_forcing_domain_handoff_path(
    manifest_path: str | Path,
    *,
    object_store_root: str | Path,
    object_store_prefix: str = "",
) -> dict[str, Any]:
    """Validate one declared forcing-domain handoff manifest without writing files."""

    store, manifest, manifest_uri, _manifest_checksum, reasons = _read_handoff_manifest_path(
        manifest_path,
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
    )
    if reasons or store is None:
        return _result(manifest, reasons, manifest_uri=manifest_uri)
    return validate_forcing_domain_handoff(manifest, store=store, manifest_uri=manifest_uri)


def parse_forcing_domain_handoff_path(
    manifest_path: str | Path,
    *,
    object_store_root: str | Path,
    object_store_prefix: str = "",
) -> dict[str, Any]:
    """Read a validated forcing-domain handoff package into table-shaped rows.

    The parser is intentionally gated by the existing validator: unavailable
    validation outcomes are returned with an empty ``parsed`` map and no partial
    rows.
    """

    store, manifest, manifest_uri, manifest_checksum, reasons = _read_handoff_manifest_path(
        manifest_path,
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
    )
    if reasons or store is None:
        result = _result(manifest, reasons, manifest_uri=manifest_uri)
        return _with_empty_parsed(
            _with_handoff_evidence(result, manifest_uri=manifest_uri, manifest_checksum=manifest_checksum)
        )

    validation_result = validate_forcing_domain_handoff(manifest, store=store, manifest_uri=manifest_uri)
    if not validation_result.get("available"):
        return _with_empty_parsed(
            _with_handoff_evidence(
                validation_result,
                manifest_uri=manifest_uri,
                manifest_checksum=manifest_checksum,
            )
        )

    parse_reasons: list[dict[str, Any]] = []
    payload_rows = _read_parser_payload_rows(manifest, store, parse_reasons)
    if not parse_reasons:
        _validate_parser_payload_row_shapes(payload_rows, parse_reasons)
    if parse_reasons:
        return _parser_unavailable_result(
            _with_handoff_evidence(
                validation_result,
                manifest_uri=manifest_uri,
                manifest_checksum=manifest_checksum,
            ),
            parse_reasons,
        )

    parsed = _parsed_handoff_tables(manifest, payload_rows)
    evidence = dict(validation_result.get("evidence") or {})
    evidence["handoff"] = redact_payload(
        {
            "manifest_uri": manifest_uri,
            "manifest_checksum_sha256": manifest_checksum,
        }
    )
    evidence["parsed_table_row_counts"] = {table: len(rows) for table, rows in parsed.items()}
    return _parser_result(validation_result, evidence=evidence, parsed=parsed)


def validate_forcing_domain_handoff(
    manifest: Mapping[str, Any],
    *,
    store: LocalObjectStore,
    manifest_uri: str | None = None,
) -> dict[str, Any]:
    """Validate a forcing-domain handoff manifest and return credential-safe evidence."""

    reasons: list[dict[str, Any]] = []
    parsed_times: dict[str, datetime] = {}

    _validate_contract_fields(manifest, reasons)
    _validate_identity_fields(manifest, reasons)
    _validate_temporal_fields(manifest, reasons, parsed_times)
    top_level_valid = not reasons
    if top_level_valid:
        _validate_compatibility_uri_fields(manifest, store, reasons)
        top_level_valid = not reasons

    package = None
    payload_evidence: dict[str, dict[str, Any]] = {}
    table_row_counts: dict[str, int] = {}
    if top_level_valid:
        package_reason_count = len(reasons)
        package = _validate_package(manifest, store, reasons)
        if (
            len(reasons) == package_reason_count
            and package is not None
            and isinstance(package.get("manifest"), Mapping)
        ):
            payload_reason_count = len(reasons)
            payload_evidence = _validate_payloads(
                manifest,
                store,
                package["directory_key"],
                package["manifest"],
                parsed_times,
                reasons,
            )
            if len(reasons) > payload_reason_count:
                payload_evidence = {}
            else:
                row_count_reason_count = len(reasons)
                table_row_counts = _table_row_counts(manifest, reasons)
                _validate_row_count_evidence(manifest, payload_evidence, table_row_counts, reasons)
                if len(reasons) > row_count_reason_count:
                    payload_evidence = {}
                    table_row_counts = {}

    identity = {field: manifest.get(field) for field in IDENTITY_FIELDS if manifest.get(field) not in (None, "")}
    compatibility = {
        field: manifest.get(field) for field in COMPATIBILITY_URI_FIELDS if manifest.get(field) not in (None, "")
    }
    evidence = {
        "manifest_uri": manifest_uri,
        "identity": identity,
        "compatibility": compatibility,
        "temporal_bounds": {
            field: _format_time(parsed_times[field])
            for field in TEMPORAL_FIELDS
            if field in parsed_times
        },
        "forcing_version": {
            "forcing_version_id": manifest.get("forcing_version_id"),
            "forcing_package_uri": manifest.get("forcing_package_uri"),
            "forcing_package_manifest_uri": manifest.get(FORCING_PACKAGE_MANIFEST_URI_FIELD),
            "forcing_package_manifest_checksum_sha256": manifest.get(FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD),
            "forcing_domain_package_manifest_uri": manifest.get(FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD),
            "forcing_domain_package_manifest_checksum_sha256": manifest.get(
                FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD
            ),
            "station_count": manifest.get("station_count"),
        },
        "payloads": payload_evidence,
        "table_row_counts": table_row_counts,
    }
    return _result(manifest, reasons, evidence=evidence, manifest_uri=manifest_uri)


def _read_handoff_manifest_path(
    manifest_path: str | Path,
    *,
    object_store_root: str | Path,
    object_store_prefix: str = "",
) -> tuple[LocalObjectStore | None, Mapping[str, Any], str | None, str | None, list[dict[str, Any]]]:
    root = Path(object_store_root).expanduser()
    if not root.is_dir():
        return (
            None,
            {},
            None,
            None,
            [_reason(REASON_OBJECT_STORE_ROOT_UNAVAILABLE, field="object_store_root")],
        )

    try:
        store = LocalObjectStore(root, object_store_prefix)
    except Exception as error:
        return (
            None,
            {},
            None,
            None,
            [
                _reason(
                    REASON_OBJECT_STORE_ROOT_UNAVAILABLE,
                    field="object_store_root",
                    detail=str(error),
                )
            ],
        )

    try:
        manifest_key = _manifest_key(manifest_path, root)
        manifest_bytes = store.read_bytes_limited(manifest_key, max_bytes=MAX_HANDOFF_MANIFEST_BYTES)
    except Exception as error:
        return (
            store,
            {},
            _safe_manifest_uri(manifest_path),
            None,
            [_reason(REASON_MANIFEST_UNREADABLE, field="manifest_uri", detail=str(error))],
        )

    manifest_checksum = sha256_bytes(manifest_bytes)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return (
            store,
            {},
            manifest_key,
            manifest_checksum,
            [_reason(REASON_MANIFEST_MALFORMED, field="manifest_uri", detail=str(error))],
        )
    if not isinstance(manifest, Mapping):
        return (
            store,
            {},
            manifest_key,
            manifest_checksum,
            [
                _reason(
                    REASON_MANIFEST_MALFORMED,
                    field="manifest_uri",
                    detail="manifest root must be an object",
                )
            ],
        )
    return store, manifest, manifest_key, manifest_checksum, []


def _with_empty_parsed(result: Mapping[str, Any]) -> dict[str, Any]:
    parser_result = dict(result)
    parser_result["parsed"] = {}
    return _redact_parser_result(parser_result)


def _with_handoff_evidence(
    result: Mapping[str, Any],
    *,
    manifest_uri: str | None,
    manifest_checksum: str | None,
) -> dict[str, Any]:
    with_evidence = dict(result)
    if manifest_checksum is None:
        return with_evidence
    evidence = dict(with_evidence.get("evidence") or {})
    evidence["handoff"] = redact_payload(
        {
            "manifest_uri": manifest_uri,
            "manifest_checksum_sha256": manifest_checksum,
        }
    )
    with_evidence["evidence"] = evidence
    return with_evidence


def _parser_unavailable_result(
    validation_result: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> dict[str, Any]:
    result = dict(validation_result)
    result["available"] = False
    result["status"] = "unavailable"
    result["unavailable_reasons"] = reasons
    evidence = dict(result.get("evidence") or {})
    evidence["payloads"] = {}
    evidence["table_row_counts"] = {}
    result["evidence"] = evidence
    result["parsed"] = {}
    return _redact_parser_result(result)


def _parser_result(
    validation_result: Mapping[str, Any],
    *,
    evidence: Mapping[str, Any],
    parsed: Mapping[str, list[Mapping[str, Any]]],
) -> dict[str, Any]:
    result = dict(validation_result)
    result["available"] = True
    result["status"] = "available"
    result["unavailable_reasons"] = []
    result["evidence"] = dict(evidence)
    result["parsed"] = parsed
    return _redact_parser_result(result)


def _read_parser_payload_rows(
    manifest: Mapping[str, Any],
    store: LocalObjectStore,
    reasons: list[dict[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    package = _normalize_package_dir_key(store, manifest.get("forcing_package_uri"), reasons)
    if package is None:
        return {}
    package_key, package_components = package
    if not _validate_package_path_identity(package_components, manifest, reasons):
        return {}

    payloads = manifest.get("payloads")
    if not isinstance(payloads, Mapping):
        for role in PAYLOAD_TABLES:
            reasons.append(_reason(REASON_PAYLOAD_MISSING, field=f"payloads.{role}", role=role))
        return {}

    payload_rows: dict[str, list[Mapping[str, Any]]] = {}
    for role, table in PAYLOAD_TABLES.items():
        payload = payloads.get(role)
        if not isinstance(payload, Mapping):
            reasons.append(_reason(REASON_PAYLOAD_MISSING, field=f"payloads.{role}", role=role))
            continue

        declared_table = payload.get("table")
        if declared_table != table:
            reasons.append(
                _reason(REASON_ROW_COUNT_MISMATCH, field=f"payloads.{role}.table", role=role, table=table)
            )

        row_count = _positive_int(payload.get("row_count"))
        if row_count is None:
            reasons.append(_reason(REASON_ROW_COUNT_MISSING, field=f"payloads.{role}.row_count", role=role))

        checksum = payload.get("checksum_sha256")
        if not _present_text(checksum) or not _valid_checksum(str(checksum)):
            reasons.append(
                _reason(REASON_PAYLOAD_CHECKSUM_MISSING, field=f"payloads.{role}.checksum_sha256", role=role)
            )

        payload_key = _normalize_object_key(
            store,
            payload.get("uri"),
            reasons,
            REASON_PAYLOAD_PATH_UNSAFE,
            f"payloads.{role}.uri",
            role=role,
        )
        if payload_key is None:
            continue
        if not payload_key.startswith(f"{package_key}/payloads/"):
            reasons.append(
                _reason(
                    REASON_PAYLOAD_OUTSIDE_PACKAGE,
                    field=f"payloads.{role}.uri",
                    role=role,
                    table=table,
                )
            )
            continue
        if not _present_text(checksum) or not _valid_checksum(str(checksum)):
            continue

        try:
            content = store.read_bytes_limited(payload_key, max_bytes=MAX_HANDOFF_PAYLOAD_BYTES)
        except Exception as error:
            reasons.append(
                _reason(REASON_PAYLOAD_MISSING, field=f"payloads.{role}.uri", role=role, detail=str(error))
            )
            continue
        actual_checksum = sha256_bytes(content)
        if actual_checksum != checksum:
            reasons.append(
                _reason(
                    REASON_PAYLOAD_CHECKSUM_MISMATCH,
                    field=f"payloads.{role}.checksum_sha256",
                    role=role,
                    expected=str(checksum),
                    actual=actual_checksum,
                )
            )
            continue

        rows = _json_rows(content, role, reasons)
        actual_row_count = len(rows) if rows is not None else None
        if row_count is not None and actual_row_count != row_count:
            reasons.append(
                _reason(
                    REASON_ROW_COUNT_MISMATCH,
                    field=f"payloads.{role}.row_count",
                    role=role,
                    table=table,
                    expected=row_count,
                    actual=actual_row_count if actual_row_count is not None else "uncountable",
                )
            )
        if rows is not None:
            payload_rows[role] = [dict(row) for row in rows]
    return payload_rows


def _parsed_handoff_tables(
    manifest: Mapping[str, Any],
    payload_rows: Mapping[str, list[Mapping[str, Any]]],
) -> dict[str, list[Mapping[str, Any]]]:
    return {
        "met.forcing_version": [_forcing_version_row(manifest)],
        "met.met_station": _project_payload_table_rows(payload_rows.get("station_inventory", []), "station_inventory"),
        "met.forcing_station_timeseries": _project_payload_table_rows(
            payload_rows.get("station_timeseries", []),
            "station_timeseries",
        ),
        "met.interp_weight": _project_payload_table_rows(
            payload_rows.get("interpolation_weights", []),
            "interpolation_weights",
        ),
    }


def _project_payload_table_rows(
    rows: list[Mapping[str, Any]],
    role: str,
) -> list[dict[str, Any]]:
    fields = PARSED_PAYLOAD_TABLE_ROW_FIELDS[role]
    return [{field: row[field] for field in fields if field in row} for row in rows]


def _validate_parser_payload_row_shapes(
    payload_rows: Mapping[str, list[Mapping[str, Any]]],
    reasons: list[dict[str, Any]],
) -> None:
    row_diagnostics = _RowDiagnostics(reasons)
    for role in PAYLOAD_TABLES:
        rows = payload_rows.get(role, [])
        for index, row in enumerate(rows):
            if role == "station_inventory":
                _validate_parser_station_inventory_row_shape(row, index, row_diagnostics)
            elif role == "station_timeseries":
                _validate_parser_required_row_fields(row, role, index, row_diagnostics)
            elif role == "interpolation_weights":
                _validate_parser_interpolation_weight_row_shape(row, index, row_diagnostics)
    row_diagnostics.flush()


def _validate_parser_station_inventory_row_shape(
    row: Mapping[str, Any],
    index: int,
    row_diagnostics: _RowDiagnostics,
) -> None:
    _validate_parser_required_row_fields(row, "station_inventory", index, row_diagnostics)
    _validate_parser_station_coordinate_evidence(row, index, row_diagnostics)


def _validate_parser_station_coordinate_evidence(
    row: Mapping[str, Any],
    index: int,
    row_diagnostics: _RowDiagnostics,
) -> None:
    longitude_present = "longitude" in row
    latitude_present = "latitude" in row
    has_lon_lat_field = longitude_present or latitude_present
    lon_lat_valid = _finite_number(row.get("longitude")) and _finite_number(row.get("latitude"))
    if has_lon_lat_field and not lon_lat_valid:
        row_diagnostics.add(REASON_FIELD_MISSING, "station_inventory", "longitude/latitude", index)

    geometry_present = "geometry" in row
    if geometry_present:
        if not _valid_geojson_point_geometry(row.get("geometry")):
            row_diagnostics.add(REASON_FIELD_MISSING, "station_inventory", "geometry", index)
        return

    if not has_lon_lat_field:
        row_diagnostics.add(REASON_FIELD_MISSING, "station_inventory", "longitude/latitude|geometry", index)


def _valid_geojson_point_geometry(value: Any) -> bool:
    if not isinstance(value, Mapping) or value.get("type") != "Point":
        return False
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) != 2:
        return False
    return _finite_number(coordinates[0]) and _finite_number(coordinates[1])


def _validate_parser_interpolation_weight_row_shape(
    row: Mapping[str, Any],
    index: int,
    row_diagnostics: _RowDiagnostics,
) -> None:
    _validate_parser_required_row_fields(row, "interpolation_weights", index, row_diagnostics)
    method = row.get("method")
    if not _present_text(method) or str(method).lower() != "direct_grid":
        return
    weight = row.get("weight")
    if _finite_number(weight) and float(weight) != 1.0:
        row_diagnostics.add(REASON_FIELD_MISSING, "interpolation_weights", "weight", index)
    if not _present_text(row.get("grid_signature")):
        row_diagnostics.add(REASON_FIELD_MISSING, "interpolation_weights", "grid_signature", index)


def _validate_parser_required_row_fields(
    row: Mapping[str, Any],
    role: str,
    index: int,
    row_diagnostics: _RowDiagnostics,
) -> None:
    for field in PARSER_REQUIRED_PAYLOAD_ROW_FIELDS[role]:
        if not _parser_field_is_present(row, role, field):
            row_diagnostics.add(REASON_FIELD_MISSING, role, field, index)


def _parser_field_is_present(row: Mapping[str, Any], role: str, field: str) -> bool:
    if field not in row:
        return False
    value = row.get(field)
    if role == "station_inventory" and field == "elevation_m":
        return _finite_number(value)
    if role == "station_inventory" and field == "active_flag":
        return isinstance(value, bool)
    if role == "station_inventory" and field == "properties_json":
        return isinstance(value, Mapping)
    if role == "station_timeseries" and field == "value":
        return _finite_number(value)
    if role == "interpolation_weights" and field == "weight":
        return _finite_number(value)
    if isinstance(value, str):
        return value.strip() != ""
    return value is not None


def _forcing_version_row(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "forcing_version_id": manifest.get("forcing_version_id"),
        "source_id": manifest.get("source_id"),
        "cycle_time": manifest.get("cycle_time"),
        "start_time": manifest.get("start_time"),
        "end_time": manifest.get("end_time"),
        "basin_id": manifest.get("basin_id"),
        "basin_version_id": manifest.get("basin_version_id"),
        "model_id": manifest.get("model_id"),
        "station_count": manifest.get("station_count"),
        "forcing_package_uri": manifest.get("forcing_package_uri"),
        "forcing_package_manifest_uri": manifest.get(FORCING_PACKAGE_MANIFEST_URI_FIELD),
        "checksum": manifest.get(FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD),
    }


def _redact_parser_result(result: Mapping[str, Any]) -> dict[str, Any]:
    parsed = result.get("parsed")
    envelope = dict(result)
    envelope["parsed"] = {}
    redacted = redact_payload(envelope)
    redacted["parsed"] = _redact_parsed_tables(parsed) if isinstance(parsed, Mapping) else {}
    return redacted


def _redact_parsed_tables(parsed: Mapping[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for table, rows in parsed.items():
        table_key = str(table)
        if isinstance(rows, list):
            redacted[table_key] = [
                _redact_parsed_row(row) if isinstance(row, Mapping) else redact_payload(row) for row in rows
            ]
        else:
            redacted[table_key] = redact_payload(rows)
    return redacted


def _redact_parsed_row(row: Mapping[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in row.items():
        key_text = str(key)
        if key_text in PARSER_BUSINESS_SIGNATURE_KEYS:
            redacted[key_text] = redact_text(value) if isinstance(value, str) else redact_payload(value)
        else:
            redacted[key_text] = _redact_parser_business_metadata(value)
    return redacted


def _redact_parser_business_metadata(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, nested in value.items():
            key_text = str(key)
            if _is_parser_sensitive_metadata_key(key_text):
                redacted[key_text] = redact_payload({key_text: nested})[key_text]
            else:
                redacted[key_text] = _redact_parser_business_metadata(nested)
        return redacted
    if isinstance(value, tuple):
        return tuple(_redact_parser_business_metadata(item) for item in value)
    if isinstance(value, list):
        return [_redact_parser_business_metadata(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _is_parser_sensitive_metadata_key(key: str) -> bool:
    return key.lower() not in PARSER_BUSINESS_SIGNATURE_KEYS and is_sensitive_key(key)


def _validate_contract_fields(manifest: Mapping[str, Any], reasons: list[dict[str, Any]]) -> None:
    schema_version = manifest.get("schema_version")
    contract_id = manifest.get("contract_id")
    if schema_version != SCHEMA_VERSION:
        reasons.append(_reason(REASON_FIELD_MISSING, field="schema_version", expected=SCHEMA_VERSION))
    if contract_id != CONTRACT_ID:
        reasons.append(_reason(REASON_FIELD_MISSING, field="contract_id", expected=CONTRACT_ID))


def _validate_identity_fields(manifest: Mapping[str, Any], reasons: list[dict[str, Any]]) -> None:
    for field in IDENTITY_FIELDS:
        if not _present_text(manifest.get(field)):
            reasons.append(_reason(REASON_IDENTITY_FIELD_MISSING, field=field))

    source_id = manifest.get("source_id")
    source = manifest.get("source")
    if _present_text(source_id) and _present_text(source) and str(source_id).lower() != str(source).lower():
        reasons.append(_reason(REASON_IDENTITY_MISMATCH, field="source_id/source"))


def _validate_temporal_fields(
    manifest: Mapping[str, Any],
    reasons: list[dict[str, Any]],
    parsed_times: dict[str, datetime],
) -> None:
    for field in TEMPORAL_FIELDS:
        value = manifest.get(field)
        if not _present_text(value):
            reasons.append(_reason(REASON_TEMPORAL_FIELD_MISSING, field=field))
            continue
        try:
            parsed_times[field] = _parse_time(str(value))
        except ValueError as error:
            reasons.append(_reason(REASON_TEMPORAL_FIELD_MALFORMED, field=field, detail=str(error)))

    start_time = parsed_times.get("start_time")
    end_time = parsed_times.get("end_time")
    if start_time is not None and end_time is not None and start_time >= end_time:
        reasons.append(_reason(REASON_TEMPORAL_WINDOW_INVALID, field="start_time/end_time"))


def _validate_compatibility_uri_fields(
    manifest: Mapping[str, Any],
    store: LocalObjectStore,
    reasons: list[dict[str, Any]],
) -> None:
    for field in COMPATIBILITY_URI_FIELDS:
        if not _present_text(manifest.get(field)):
            reasons.append(_reason(REASON_FIELD_MISSING, field=field))
    if reasons:
        return

    _validate_model_package_uri(manifest, store, reasons)
    forcing_package = _normalize_forcing_compat_uri(
        manifest,
        store,
        reasons,
        field="forcing_package_uri",
    )
    forcing = _normalize_forcing_compat_uri(
        manifest,
        store,
        reasons,
        field="forcing_uri",
    )
    if forcing_package is not None and forcing is not None and forcing != forcing_package:
        reasons.append(
            _reason(
                REASON_COMPATIBILITY_URI_MISMATCH,
                field="forcing_uri",
                expected=forcing_package,
                actual=forcing,
            )
        )
    if forcing_package is not None:
        _validate_forcing_package_manifest_compat_uri(manifest, store, forcing_package, reasons)
        _validate_package_manifest_compat_uri(manifest, store, forcing_package, reasons)
    _validate_run_manifest_uri(manifest, store, reasons)
    _validate_output_uri(manifest, store, reasons)


def _validate_model_package_uri(
    manifest: Mapping[str, Any],
    store: LocalObjectStore,
    reasons: list[dict[str, Any]],
) -> None:
    field = "model_package_uri"
    key = _normalize_compat_object_key(manifest.get(field), store, reasons, field=field)
    if key is None:
        return
    validation = validate_object_path(key)
    if not validation.valid or validation.category != "models":
        reasons.append(_reason(REASON_COMPATIBILITY_URI_UNSAFE, field=field, detail=validation.error))
        return
    expected_model_id = manifest.get("model_id")
    actual_model_id = validation.components.get("model_id")
    if _present_text(expected_model_id) and actual_model_id != expected_model_id:
        reasons.append(
            _reason(
                REASON_COMPATIBILITY_URI_MISMATCH,
                field=field,
                expected=str(expected_model_id),
                actual=actual_model_id,
            )
        )


def _normalize_forcing_compat_uri(
    manifest: Mapping[str, Any],
    store: LocalObjectStore,
    reasons: list[dict[str, Any]],
    *,
    field: str,
) -> str | None:
    key = _normalize_compat_object_key(manifest.get(field), store, reasons, field=field, validate=False)
    if key is None:
        return None
    package_key = key.strip("/")
    parts = package_key.split("/")
    if len(parts) != 5 or parts[0] != "forcing" or any(part == "" for part in parts):
        reasons.append(
            _reason(
                REASON_COMPATIBILITY_URI_UNSAFE,
                field=field,
                detail=(
                    f"{field} must be a "
                    "forcing/{source}/{cycle_time}/{basin_version_id}/{model_id} package directory"
                ),
            )
        )
        return None
    validation = validate_object_path(f"{package_key}/_package")
    if not validation.valid or validation.category != "forcing":
        reasons.append(_reason(REASON_COMPATIBILITY_URI_UNSAFE, field=field, detail=validation.error))
        return None

    _validate_forcing_uri_identity(field, validation.components, manifest, reasons)
    return package_key


def _validate_forcing_uri_identity(
    field: str,
    components: Mapping[str, str],
    manifest: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> None:
    source = components.get("source")
    source_id = manifest.get("source_id")
    if _present_text(source) and _present_text(source_id) and source.lower() != str(source_id).lower():
        reasons.append(
            _reason(
                REASON_COMPATIBILITY_URI_MISMATCH,
                field=f"{field}.source",
                expected=str(source_id).lower(),
                actual=source,
            )
        )

    cycle = components.get("cycle_time")
    cycle_time = _optional_manifest_time(manifest, "cycle_time")
    expected_cycle = _compact_cycle_token(cycle_time) if cycle_time is not None else None
    if _present_text(cycle) and expected_cycle is not None and cycle != expected_cycle:
        reasons.append(
            _reason(
                REASON_COMPATIBILITY_URI_MISMATCH,
                field=f"{field}.cycle_time",
                expected=expected_cycle,
                actual=cycle,
            )
        )

    for component, manifest_field in (
        ("basin_version_id", "basin_version_id"),
        ("model_id", "model_id"),
    ):
        actual = components.get(component)
        expected = manifest.get(manifest_field)
        if _present_text(actual) and _present_text(expected) and actual != expected:
            reasons.append(
                _reason(
                    REASON_COMPATIBILITY_URI_MISMATCH,
                    field=f"{field}.{component}",
                    expected=str(expected),
                    actual=actual,
                )
            )


def _validate_package_manifest_compat_uri(
    manifest: Mapping[str, Any],
    store: LocalObjectStore,
    forcing_package_key: str,
    reasons: list[dict[str, Any]],
) -> None:
    field = FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD
    key = _normalize_compat_object_key(manifest.get(field), store, reasons, field=field)
    if key is None:
        return
    validation = validate_object_path(key)
    if not validation.valid or validation.category != "forcing":
        reasons.append(_reason(REASON_COMPATIBILITY_URI_UNSAFE, field=field, detail=validation.error))
        return
    if not key.startswith(f"{forcing_package_key}/"):
        reasons.append(
            _reason(
                REASON_COMPATIBILITY_URI_MISMATCH,
                field=field,
                expected=f"{forcing_package_key}/...",
                actual=key,
            )
        )
        return
    if key.startswith(f"{forcing_package_key}/payloads/"):
        reasons.append(
            _reason(
                REASON_COMPATIBILITY_URI_UNSAFE,
                field=field,
                detail="package manifest URI must not be under the package payloads directory",
            )
        )


def _validate_forcing_package_manifest_compat_uri(
    manifest: Mapping[str, Any],
    store: LocalObjectStore,
    forcing_package_key: str,
    reasons: list[dict[str, Any]],
) -> None:
    field = FORCING_PACKAGE_MANIFEST_URI_FIELD
    key = _normalize_compat_object_key(manifest.get(field), store, reasons, field=field)
    if key is None:
        return
    validation = validate_object_path(key)
    if not validation.valid or validation.category != "forcing":
        reasons.append(_reason(REASON_COMPATIBILITY_URI_UNSAFE, field=field, detail=validation.error))
        return
    expected_key = f"{forcing_package_key}/forcing_package.json"
    if key != expected_key:
        reasons.append(
            _reason(
                REASON_COMPATIBILITY_URI_MISMATCH,
                field=field,
                expected=expected_key,
                actual=key,
            )
        )


def _validate_run_manifest_uri(
    manifest: Mapping[str, Any],
    store: LocalObjectStore,
    reasons: list[dict[str, Any]],
) -> None:
    field = "run_manifest_uri"
    key = _normalize_compat_object_key(manifest.get(field), store, reasons, field=field)
    if key is None:
        return
    validation = validate_object_path(key)
    if not validation.valid or validation.category != "runs" or validation.components.get("sub_prefix") != "input":
        reasons.append(_reason(REASON_COMPATIBILITY_URI_UNSAFE, field=field, detail=validation.error))
        return
    expected_key = f"runs/{manifest.get('run_id')}/input/manifest.json"
    if key != expected_key:
        reasons.append(
            _reason(
                REASON_COMPATIBILITY_URI_MISMATCH,
                field=field,
                expected=expected_key,
                actual=key,
            )
        )


def _validate_output_uri(
    manifest: Mapping[str, Any],
    store: LocalObjectStore,
    reasons: list[dict[str, Any]],
) -> None:
    field = "output_uri"
    key = _normalize_compat_object_key(manifest.get(field), store, reasons, field=field, validate=False)
    if key is None:
        return
    expected_prefix = f"runs/{manifest.get('run_id')}/output"
    if key != expected_prefix and not key.startswith(f"{expected_prefix}/"):
        reasons.append(
            _reason(
                REASON_COMPATIBILITY_URI_MISMATCH,
                field=field,
                expected=f"{expected_prefix}/...",
                actual=key,
            )
        )


def _normalize_compat_object_key(
    uri: Any,
    store: LocalObjectStore,
    reasons: list[dict[str, Any]],
    *,
    field: str,
    validate: bool = True,
) -> str | None:
    if not _present_text(uri):
        return None
    try:
        key = store.normalize_key(str(uri)).strip("/")
    except Exception as error:
        reasons.append(_reason(REASON_COMPATIBILITY_URI_UNSAFE, field=field, detail=str(error)))
        return None
    if not key or any(part == "" for part in key.split("/")):
        reasons.append(_reason(REASON_COMPATIBILITY_URI_UNSAFE, field=field, detail="object key is empty or sparse"))
        return None
    if validate:
        validation = validate_object_path(key)
        if not validation.valid:
            reasons.append(_reason(REASON_COMPATIBILITY_URI_UNSAFE, field=field, detail=validation.error))
            return None
    return key


def _validate_package(
    manifest: Mapping[str, Any],
    store: LocalObjectStore,
    reasons: list[dict[str, Any]],
) -> dict[str, Any] | None:
    package = _normalize_package_dir_key(store, manifest.get("forcing_package_uri"), reasons)
    if package is None:
        return None
    package_key, package_components = package
    if not _validate_package_path_identity(package_components, manifest, reasons):
        return {"directory_key": package_key, "manifest": None}
    if not _validate_forcing_package_manifest_checksum(manifest, store, package_key, reasons):
        return {"directory_key": package_key, "manifest": None}

    manifest_key = _normalize_package_manifest_key(store, manifest, package_key, reasons)
    manifest_checksum = manifest.get(FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD)
    if not _present_text(manifest_checksum) or not _valid_checksum(str(manifest_checksum)):
        reasons.append(_reason(REASON_PACKAGE_CHECKSUM_MISSING, field=FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD))
        return {"directory_key": package_key, "manifest": None}

    if manifest_key is None:
        return {"directory_key": package_key, "manifest": None}

    try:
        content = store.read_bytes_limited(manifest_key, max_bytes=MAX_HANDOFF_MANIFEST_BYTES)
    except Exception as error:
        reasons.append(
            _reason(REASON_PACKAGE_MISSING, field=FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD, detail=str(error))
        )
        return {"directory_key": package_key, "manifest": None}

    actual_checksum = sha256_bytes(content)
    if actual_checksum != manifest_checksum:
        reasons.append(
            _reason(
                REASON_PACKAGE_CHECKSUM_MISMATCH,
                field=FORCING_DOMAIN_PACKAGE_MANIFEST_CHECKSUM_FIELD,
                expected=str(manifest_checksum),
                actual=actual_checksum,
            )
        )
        return {"directory_key": package_key, "manifest": None}

    try:
        package_manifest = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        reasons.append(
            _reason(
                REASON_PACKAGE_MANIFEST_MALFORMED,
                field=FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD,
                detail=str(error),
            )
        )
        return {"directory_key": package_key, "manifest": None}
    if not isinstance(package_manifest, Mapping):
        reasons.append(
            _reason(
                REASON_PACKAGE_MANIFEST_MALFORMED,
                field=FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD,
                detail="package manifest root must be an object",
            )
        )
        return {"directory_key": package_key, "manifest": None}

    if _validate_package_manifest(package_manifest, manifest, reasons):
        return {"directory_key": package_key, "manifest_key": manifest_key, "manifest": package_manifest}
    return {"directory_key": package_key, "manifest_key": manifest_key, "manifest": None}


def _validate_forcing_package_manifest_checksum(
    manifest: Mapping[str, Any],
    store: LocalObjectStore,
    package_key: str,
    reasons: list[dict[str, Any]],
) -> bool:
    manifest_key = _normalize_object_key(
        store,
        manifest.get(FORCING_PACKAGE_MANIFEST_URI_FIELD),
        reasons,
        REASON_PACKAGE_PATH_UNSAFE,
        FORCING_PACKAGE_MANIFEST_URI_FIELD,
    )
    if manifest_key is None:
        return False
    expected_key = f"{package_key}/forcing_package.json"
    if manifest_key != expected_key:
        reasons.append(
            _reason(
                REASON_PAYLOAD_OUTSIDE_PACKAGE,
                field=FORCING_PACKAGE_MANIFEST_URI_FIELD,
                expected=expected_key,
                actual=manifest_key,
            )
        )
        return False

    manifest_checksum = manifest.get(FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD)
    if not _present_text(manifest_checksum) or not _valid_checksum(str(manifest_checksum)):
        reasons.append(_reason(REASON_PACKAGE_CHECKSUM_MISSING, field=FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD))
        return False

    try:
        content = store.read_bytes_limited(manifest_key, max_bytes=MAX_HANDOFF_MANIFEST_BYTES)
    except Exception as error:
        reasons.append(_reason(REASON_PACKAGE_MISSING, field=FORCING_PACKAGE_MANIFEST_URI_FIELD, detail=str(error)))
        return False

    actual_checksum = sha256_bytes(content)
    if actual_checksum != manifest_checksum:
        reasons.append(
            _reason(
                REASON_PACKAGE_CHECKSUM_MISMATCH,
                field=FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD,
                expected=str(manifest_checksum),
                actual=actual_checksum,
            )
        )
        return False
    return True


def _normalize_package_dir_key(
    store: LocalObjectStore,
    package_uri: Any,
    reasons: list[dict[str, Any]],
) -> tuple[str, Mapping[str, str]] | None:
    if not _present_text(package_uri):
        reasons.append(_reason(REASON_PACKAGE_MISSING, field="forcing_package_uri"))
        return None
    try:
        package_key = store.normalize_key(str(package_uri)).strip("/")
    except Exception as error:
        reasons.append(_reason(REASON_PACKAGE_PATH_UNSAFE, field="forcing_package_uri", detail=str(error)))
        return None

    parts = package_key.split("/")
    if len(parts) != 5 or parts[0] != "forcing" or any(part == "" for part in parts):
        reasons.append(
            _reason(
                REASON_PACKAGE_PATH_UNSAFE,
                field="forcing_package_uri",
                detail=(
                    "forcing_package_uri must be a "
                    "forcing/{source}/{cycle_time}/{basin_version_id}/{model_id} package directory"
                ),
            )
        )
        return None

    validation = validate_object_path(f"{package_key}/_package")
    if not validation.valid or validation.category != "forcing":
        reasons.append(_reason(REASON_PACKAGE_PATH_UNSAFE, field="forcing_package_uri", detail=validation.error))
        return None
    return package_key, validation.components


def _normalize_package_manifest_key(
    store: LocalObjectStore,
    manifest: Mapping[str, Any],
    package_key: str,
    reasons: list[dict[str, Any]],
) -> str | None:
    manifest_key = _normalize_object_key(
        store,
        manifest.get(FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD),
        reasons,
        REASON_PACKAGE_PATH_UNSAFE,
        FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD,
    )
    if manifest_key is None:
        return None
    if not manifest_key.startswith(f"{package_key}/") or manifest_key.startswith(f"{package_key}/payloads/"):
        reasons.append(_reason(REASON_PAYLOAD_OUTSIDE_PACKAGE, field=FORCING_DOMAIN_PACKAGE_MANIFEST_URI_FIELD))
        return None
    return manifest_key


def _validate_package_path_identity(
    package_components: Mapping[str, str],
    manifest: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> bool:
    before = len(reasons)
    source = package_components.get("source")
    source_id = manifest.get("source_id")
    if _present_text(source) and _present_text(source_id) and str(source).lower() != str(source_id).lower():
        reasons.append(
            _reason(
                REASON_IDENTITY_MISMATCH,
                field="forcing_package_uri.source",
                expected=str(source_id).lower(),
                actual=source,
            )
        )

    cycle = package_components.get("cycle_time")
    cycle_time = _optional_manifest_time(manifest, "cycle_time")
    expected_cycle = _compact_cycle_token(cycle_time) if cycle_time is not None else None
    if _present_text(cycle) and expected_cycle is not None and cycle != expected_cycle:
        reasons.append(
            _reason(
                REASON_IDENTITY_MISMATCH,
                field="forcing_package_uri.cycle_time",
                expected=expected_cycle,
                actual=cycle,
            )
        )

    for component, field in (
        ("basin_version_id", "basin_version_id"),
        ("model_id", "model_id"),
    ):
        actual = package_components.get(component)
        expected = manifest.get(field)
        if _present_text(actual) and _present_text(expected) and actual != expected:
            reasons.append(
                _reason(
                    REASON_IDENTITY_MISMATCH,
                    field=f"forcing_package_uri.{component}",
                    expected=str(expected),
                    actual=actual,
                )
            )

    return len(reasons) == before


def _validate_package_manifest(
    package_manifest: Mapping[str, Any],
    manifest: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> bool:
    before = len(reasons)
    if package_manifest.get("schema_version") != SCHEMA_VERSION:
        reasons.append(
            _reason(REASON_FIELD_MISSING, field="package_manifest.schema_version", expected=SCHEMA_VERSION)
        )
    if package_manifest.get("contract_id") != PACKAGE_CONTRACT_ID:
        reasons.append(
            _reason(REASON_FIELD_MISSING, field="package_manifest.contract_id", expected=PACKAGE_CONTRACT_ID)
        )

    for field in (
        "run_id",
        "source_id",
        "source",
        "cycle_time",
        "start_time",
        "end_time",
        "model_id",
        "basin_id",
        "basin_version_id",
        "forcing_version_id",
    ):
        _validate_package_manifest_identity_field(package_manifest, manifest, field, reasons)

    _validate_package_manifest_station_count(package_manifest, manifest, reasons)
    _validate_package_manifest_payloads(package_manifest, manifest, reasons)
    _validate_package_manifest_table_counts(package_manifest, manifest, reasons)
    return len(reasons) == before


def _validate_package_manifest_identity_field(
    package_manifest: Mapping[str, Any],
    manifest: Mapping[str, Any],
    field: str,
    reasons: list[dict[str, Any]],
) -> None:
    package_value = package_manifest.get(field)
    manifest_value = manifest.get(field)
    if not _present_text(package_value):
        reasons.append(_reason(REASON_IDENTITY_FIELD_MISSING, field=f"package_manifest.{field}"))
        return
    if not _present_text(manifest_value):
        return

    if field in {"source_id", "source"}:
        matches = str(package_value).lower() == str(manifest_value).lower()
    elif field in TEMPORAL_FIELDS:
        try:
            matches = _parse_time(str(package_value)) == _parse_time(str(manifest_value))
        except ValueError:
            matches = False
    else:
        matches = package_value == manifest_value
    if not matches:
        reasons.append(
            _reason(
                REASON_IDENTITY_MISMATCH,
                field=f"package_manifest.{field}",
                expected=str(manifest_value),
                actual=str(package_value),
            )
        )


def _validate_package_manifest_station_count(
    package_manifest: Mapping[str, Any],
    manifest: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> None:
    package_count = _positive_int(package_manifest.get("station_count"))
    manifest_count = _positive_int(manifest.get("station_count"))
    if package_count is None:
        reasons.append(_reason(REASON_ROW_COUNT_MISSING, field="package_manifest.station_count"))
        return
    if manifest_count is not None and package_count != manifest_count:
        reasons.append(
            _reason(
                REASON_STATION_COUNT_MISMATCH,
                field="package_manifest.station_count",
                expected=manifest_count,
                actual=package_count,
            )
        )


def _validate_package_manifest_payloads(
    package_manifest: Mapping[str, Any],
    manifest: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> None:
    package_payloads = package_manifest.get("payloads")
    manifest_payloads = manifest.get("payloads")
    if not isinstance(package_payloads, Mapping):
        for role in PAYLOAD_TABLES:
            reasons.append(_reason(REASON_PAYLOAD_MISSING, field=f"package_manifest.payloads.{role}", role=role))
        return
    if not isinstance(manifest_payloads, Mapping):
        return

    for role, table in PAYLOAD_TABLES.items():
        package_payload = package_payloads.get(role)
        manifest_payload = manifest_payloads.get(role)
        if not isinstance(package_payload, Mapping):
            reasons.append(_reason(REASON_PAYLOAD_MISSING, field=f"package_manifest.payloads.{role}", role=role))
            continue
        if not isinstance(manifest_payload, Mapping):
            continue
        for field in ("uri", "checksum_sha256", "table"):
            package_value = package_payload.get(field)
            manifest_value = manifest_payload.get(field)
            if package_value != manifest_value:
                reasons.append(
                    _reason(
                        REASON_IDENTITY_MISMATCH,
                        field=f"package_manifest.payloads.{role}.{field}",
                        role=role,
                        table=table,
                        expected=manifest_value,
                        actual=package_value,
                    )
                )
        package_count = _positive_int(package_payload.get("row_count"))
        manifest_count = _positive_int(manifest_payload.get("row_count"))
        if package_count is None:
            reasons.append(_reason(REASON_ROW_COUNT_MISSING, field=f"package_manifest.payloads.{role}.row_count"))
        elif manifest_count is not None and package_count != manifest_count:
            reasons.append(
                _reason(
                    REASON_ROW_COUNT_MISMATCH,
                    field=f"package_manifest.payloads.{role}.row_count",
                    role=role,
                    table=table,
                    expected=manifest_count,
                    actual=package_count,
                )
            )


def _validate_package_manifest_table_counts(
    package_manifest: Mapping[str, Any],
    manifest: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> None:
    package_counts = package_manifest.get("table_row_counts")
    manifest_counts = manifest.get("table_row_counts")
    if not isinstance(package_counts, Mapping):
        for table in TABLE_ROW_COUNT_FIELDS:
            reasons.append(_reason(REASON_ROW_COUNT_MISSING, field=f"package_manifest.table_row_counts.{table}"))
        return
    if not isinstance(manifest_counts, Mapping):
        return

    for table in TABLE_ROW_COUNT_FIELDS:
        package_count = _positive_int(package_counts.get(table))
        manifest_count = _positive_int(manifest_counts.get(table))
        if package_count is None:
            reasons.append(_reason(REASON_ROW_COUNT_MISSING, field=f"package_manifest.table_row_counts.{table}"))
        elif manifest_count is not None and package_count != manifest_count:
            reasons.append(
                _reason(
                    REASON_ROW_COUNT_MISMATCH,
                    field=f"package_manifest.table_row_counts.{table}",
                    table=table,
                    expected=manifest_count,
                    actual=package_count,
                )
            )


def _validate_payloads(
    manifest: Mapping[str, Any],
    store: LocalObjectStore,
    package_dir: str,
    package_manifest: Mapping[str, Any],
    parsed_times: Mapping[str, datetime],
    reasons: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    payloads = manifest.get("payloads")
    if not isinstance(payloads, Mapping):
        for role in PAYLOAD_TABLES:
            reasons.append(_reason(REASON_PAYLOAD_MISSING, field=f"payloads.{role}", role=role))
        return {}

    package_payloads = package_manifest.get("payloads")
    if not isinstance(package_payloads, Mapping):
        package_payloads = {}

    evidence: dict[str, dict[str, Any]] = {}
    row_context: dict[str, Any] = {
        "station_ids": set(),
        "timeseries_variables": set(),
        "timeseries_units": {},
        "timeseries_lattice_segments": [],
    }
    for role, table in PAYLOAD_TABLES.items():
        payload = payloads.get(role)
        if not isinstance(payload, Mapping):
            reasons.append(_reason(REASON_PAYLOAD_MISSING, field=f"payloads.{role}", role=role))
            continue
        package_payload = package_payloads.get(role)
        if not isinstance(package_payload, Mapping):
            reasons.append(_reason(REASON_PAYLOAD_MISSING, field=f"package_manifest.payloads.{role}", role=role))
            continue
        declared_table = payload.get("table")
        if declared_table != table:
            reasons.append(
                _reason(REASON_ROW_COUNT_MISMATCH, field=f"payloads.{role}.table", role=role, table=table)
            )
        if role == "station_timeseries":
            row_context["timeseries_lattice_segments"] = _validate_station_timeseries_metadata(
                payload,
                manifest,
                reasons,
            )
        row_count = _positive_int(payload.get("row_count"))
        if row_count is None:
            reasons.append(_reason(REASON_ROW_COUNT_MISSING, field=f"payloads.{role}.row_count", role=role))

        checksum = payload.get("checksum_sha256")
        if not _present_text(checksum) or not _valid_checksum(str(checksum)):
            reasons.append(
                _reason(REASON_PAYLOAD_CHECKSUM_MISSING, field=f"payloads.{role}.checksum_sha256", role=role)
            )

        payload_key = _normalize_object_key(
            store,
            payload.get("uri"),
            reasons,
            REASON_PAYLOAD_PATH_UNSAFE,
            f"payloads.{role}.uri",
            role=role,
        )
        if payload_key is None:
            continue
        if not payload_key.startswith(f"{package_dir}/payloads/"):
            reasons.append(
                _reason(
                    REASON_PAYLOAD_OUTSIDE_PACKAGE,
                    field=f"payloads.{role}.uri",
                    role=role,
                    table=table,
                )
            )
            continue
        if not _present_text(checksum) or not _valid_checksum(str(checksum)):
            continue

        try:
            content = store.read_bytes_limited(payload_key, max_bytes=MAX_HANDOFF_PAYLOAD_BYTES)
        except Exception as error:
            reasons.append(
                _reason(REASON_PAYLOAD_MISSING, field=f"payloads.{role}.uri", role=role, detail=str(error))
            )
            continue
        size = len(content)
        actual_checksum = sha256_bytes(content)
        if actual_checksum != checksum:
            reasons.append(
                _reason(
                    REASON_PAYLOAD_CHECKSUM_MISMATCH,
                    field=f"payloads.{role}.checksum_sha256",
                    role=role,
                    expected=str(checksum),
                    actual=actual_checksum,
                )
            )
            continue

        rows = _json_rows(content, role, reasons)
        actual_row_count = len(rows) if rows is not None else None
        if row_count is not None and actual_row_count != row_count:
            reasons.append(
                _reason(
                    REASON_ROW_COUNT_MISMATCH,
                    field=f"payloads.{role}.row_count",
                    role=role,
                    table=table,
                    expected=row_count,
                    actual=actual_row_count if actual_row_count is not None else "uncountable",
                )
            )
        if rows is not None:
            _validate_payload_rows(role, rows, manifest, payload, parsed_times, row_context, reasons)
        evidence[role] = {
            "table": table,
            "uri": payload.get("uri"),
            "checksum_sha256": checksum,
            "actual_checksum_sha256": actual_checksum,
            "byte_count": size,
            "row_count": row_count,
            "actual_row_count": actual_row_count,
        }
    return evidence


def _validate_station_timeseries_metadata(
    payload: Mapping[str, Any],
    manifest: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> list[TimeLatticeSegment]:
    variables = payload.get("variables")
    if not isinstance(variables, list) or not variables or not all(_present_text(item) for item in variables):
        reasons.append(_reason(REASON_FIELD_MISSING, field="payloads.station_timeseries.variables"))
        variables = []
    else:
        duplicates = _duplicate_text_values(variables)
        if duplicates:
            reasons.append(
                _reason(
                    REASON_STATION_TIMESERIES_VARIABLE_DUPLICATE,
                    field="payloads.station_timeseries.variables",
                    duplicate_count=len(duplicates),
                    samples=duplicates[:MAX_LATTICE_SAMPLES],
                )
            )
    units = payload.get("units")
    if not isinstance(units, Mapping):
        reasons.append(_reason(REASON_FIELD_MISSING, field="payloads.station_timeseries.units"))
    else:
        for variable in variables:
            if not _present_text(units.get(variable)):
                reasons.append(
                    _reason(REASON_FIELD_MISSING, field=f"payloads.station_timeseries.units.{variable}")
                )

    declared_variables = {str(variable) for variable in variables if _present_text(variable)}
    return _station_timeseries_time_lattice(payload, manifest, reasons, declared_variables)


def _station_timeseries_time_lattice(
    payload: Mapping[str, Any],
    manifest: Mapping[str, Any],
    reasons: list[dict[str, Any]],
    declared_variables: set[str],
) -> list[TimeLatticeSegment]:
    raw_segments = payload.get("time_lattice")
    if not isinstance(raw_segments, list) or not raw_segments:
        reasons.append(_reason(REASON_FIELD_MISSING, field="payloads.station_timeseries.time_lattice"))
        return []

    segments: list[TimeLatticeSegment] = []
    for index, raw_segment in enumerate(raw_segments):
        field_prefix = f"payloads.station_timeseries.time_lattice[{index}]"
        if not isinstance(raw_segment, Mapping):
            reasons.append(
                _reason(
                    REASON_FIELD_MISSING,
                    field=field_prefix,
                    detail="time_lattice segment must be an object",
                )
            )
            continue
        variable_scope = _lattice_segment_variable_scope(
            raw_segment,
            field_prefix,
            declared_variables,
            reasons,
        )
        valid_time_start = _required_lattice_time(raw_segment, "valid_time_start", field_prefix, reasons)
        valid_time_end = _required_lattice_time(raw_segment, "valid_time_end", field_prefix, reasons)
        resolution_value = raw_segment.get("native_resolution")
        if not _present_text(resolution_value):
            reasons.append(_reason(REASON_FIELD_MISSING, field=f"{field_prefix}.native_resolution"))
            resolution = None
            resolution_label = None
        else:
            resolution_label = str(resolution_value).strip()
            resolution = _parse_duration(resolution_label)
            if resolution is None:
                reasons.append(
                    _reason(
                        REASON_TEMPORAL_FIELD_MALFORMED,
                        field=f"{field_prefix}.native_resolution",
                        detail="native_resolution must be an h/min duration such as 3h or 30min",
                    )
                )
        if (
            variable_scope == frozenset()
            or valid_time_start is None
            or valid_time_end is None
            or resolution is None
            or resolution_label is None
        ):
            continue
        if valid_time_start > valid_time_end:
            reasons.append(_reason(REASON_TEMPORAL_WINDOW_INVALID, field=f"{field_prefix}.valid_time_window"))
            continue
        segments.append((variable_scope, valid_time_start, valid_time_end, resolution_label, resolution))

    _validate_time_lattice_bounds(segments, manifest, reasons)
    return segments


def _validate_time_lattice_bounds(
    segments: list[TimeLatticeSegment],
    manifest: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> None:
    if not segments:
        return
    start_time = _optional_manifest_time(manifest, "start_time")
    end_time = _optional_manifest_time(manifest, "end_time")
    lattice_start = min(segment[1] for segment in segments)
    lattice_end = max(segment[2] for segment in segments)
    if start_time is not None and lattice_start != start_time:
        reasons.append(
            _reason(
                REASON_TEMPORAL_WINDOW_INVALID,
                field="payloads.station_timeseries.time_lattice.valid_time_start",
                expected=_format_time(start_time),
                actual=_format_time(lattice_start),
            )
        )
    if end_time is not None and lattice_end != end_time:
        reasons.append(
            _reason(
                REASON_TEMPORAL_WINDOW_INVALID,
                field="payloads.station_timeseries.time_lattice.valid_time_end",
                expected=_format_time(end_time),
                actual=_format_time(lattice_end),
            )
        )


def _lattice_segment_variable_scope(
    segment: Mapping[str, Any],
    field_prefix: str,
    declared_variables: set[str],
    reasons: list[dict[str, Any]],
) -> frozenset[str] | None:
    raw_variable = segment.get("variable")
    raw_variables = segment.get("variables")
    values: list[str] = []

    if raw_variable is not None:
        if _present_text(raw_variable):
            values.append(str(raw_variable))
        else:
            reasons.append(_reason(REASON_FIELD_MISSING, field=f"{field_prefix}.variable"))
            return frozenset()

    if raw_variables is not None:
        if not isinstance(raw_variables, list) or not raw_variables or not all(
            _present_text(item) for item in raw_variables
        ):
            reasons.append(_reason(REASON_FIELD_MISSING, field=f"{field_prefix}.variables"))
            return frozenset()
        values.extend(str(item) for item in raw_variables)

    if not values:
        return None

    duplicates = _duplicate_text_values(values)
    if duplicates:
        reasons.append(
            _reason(
                REASON_STATION_TIMESERIES_VARIABLE_DUPLICATE,
                field=f"{field_prefix}.variables",
                duplicate_count=len(duplicates),
                samples=duplicates[:MAX_LATTICE_SAMPLES],
            )
        )
        return frozenset()

    variable_scope = frozenset(values)
    unknown_variables = sorted(
        variable for variable in variable_scope if declared_variables and variable not in declared_variables
    )
    if unknown_variables:
        reasons.append(
            _reason(
                REASON_IDENTITY_MISMATCH,
                field=f"{field_prefix}.variables",
                expected=sorted(declared_variables),
                actual=unknown_variables[:MAX_LATTICE_SAMPLES],
            )
        )
        return frozenset()
    return variable_scope


def _json_rows(content: bytes, role: str, reasons: list[dict[str, Any]]) -> list[Mapping[str, Any]] | None:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        reasons.append(_reason(REASON_PAYLOAD_MALFORMED, field=f"payloads.{role}.uri", role=role, detail=str(error)))
        return None
    if isinstance(payload, list):
        raw_rows = payload
    elif isinstance(payload, Mapping) and isinstance(payload.get("rows"), list):
        raw_rows = payload["rows"]
    else:
        reasons.append(
            _reason(
                REASON_PAYLOAD_MALFORMED,
                field=f"payloads.{role}.rows",
                role=role,
                detail="payload must be a row array or an object with a rows array",
            )
        )
        return None
    if not raw_rows:
        reasons.append(_reason(REASON_PAYLOAD_MALFORMED, field=f"payloads.{role}.rows", role=role, detail="empty rows"))
        return []
    rows: list[Mapping[str, Any]] = []
    for index, row in enumerate(raw_rows):
        if not isinstance(row, Mapping):
            reasons.append(
                _reason(
                    REASON_PAYLOAD_MALFORMED,
                    field=f"payloads.{role}.rows[{index}]",
                    role=role,
                    detail="row must be an object",
                )
            )
            return None
        rows.append(row)
    return rows


class _RowDiagnostics:
    def __init__(self, reasons: list[dict[str, Any]]) -> None:
        self._reasons = reasons
        self._items: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add(self, code: str, role: str, field: str, index: int, **details: Any) -> None:
        key = (code, role, field)
        item = self._items.setdefault(
            key,
            {
                "code": code,
                "field": f"payloads.{role}.rows.{field}",
                "role": role,
                "occurrence_count": 0,
                "samples": [],
            },
        )
        item["occurrence_count"] += 1
        samples = item["samples"]
        if isinstance(samples, list) and len(samples) < MAX_ROW_DIAGNOSTIC_SAMPLES:
            sample = {"row_index": index}
            sample.update({key: value for key, value in details.items() if value not in (None, "")})
            samples.append(sample)

    def flush(self) -> None:
        self._reasons.extend(self._items.values())


def _validate_payload_rows(
    role: str,
    rows: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    payload: Mapping[str, Any],
    parsed_times: Mapping[str, datetime],
    row_context: dict[str, Any],
    reasons: list[dict[str, Any]],
) -> None:
    row_diagnostics = _RowDiagnostics(reasons)
    if role == "station_inventory":
        _validate_station_inventory_rows(rows, manifest, row_context, reasons, row_diagnostics)
    elif role == "station_timeseries":
        _validate_station_timeseries_rows(rows, manifest, payload, parsed_times, row_context, reasons, row_diagnostics)
    elif role == "interpolation_weights":
        _validate_interpolation_weight_rows(rows, manifest, row_context, reasons, row_diagnostics)
    row_diagnostics.flush()


def _validate_station_inventory_rows(
    rows: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    row_context: dict[str, Any],
    reasons: list[dict[str, Any]],
    row_diagnostics: _RowDiagnostics,
) -> None:
    station_ids: set[str] = set()
    for index, row in enumerate(rows):
        station_id = _row_text(row, "station_id", "station_inventory", index, reasons, row_diagnostics)
        if station_id is not None:
            station_ids.add(station_id)
        _row_text_matches(
            row,
            "basin_version_id",
            manifest.get("basin_version_id"),
            "station_inventory",
            index,
            reasons,
            row_diagnostics,
        )
        if not _has_coordinate_evidence(row):
            row_diagnostics.add(REASON_FIELD_MISSING, "station_inventory", "longitude/latitude", index)

    station_count = _positive_int(manifest.get("station_count"))
    duplicate_station_ids = _duplicate_text_values(
        [row.get("station_id") for row in rows if _present_text(row.get("station_id"))]
    )
    if duplicate_station_ids:
        reasons.append(
            _reason(
                REASON_STATION_INVENTORY_DUPLICATE,
                field="payloads.station_inventory.rows.station_id",
                duplicate_count=len(duplicate_station_ids),
                samples=duplicate_station_ids[:MAX_LATTICE_SAMPLES],
            )
        )
    if station_count is not None and len(station_ids) != station_count:
        reasons.append(
            _reason(
                REASON_STATION_COUNT_MISMATCH,
                field="payloads.station_inventory.unique_station_id_count",
                table="met.met_station",
                expected=station_count,
                actual=len(station_ids),
            )
        )
    row_context["station_ids"] = station_ids


def _validate_station_timeseries_rows(
    rows: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    payload: Mapping[str, Any],
    parsed_times: Mapping[str, datetime],
    row_context: dict[str, Any],
    reasons: list[dict[str, Any]],
    row_diagnostics: _RowDiagnostics,
) -> None:
    variables = _payload_variable_set(payload)
    units = payload.get("units") if isinstance(payload.get("units"), Mapping) else {}
    segments = row_context.get("timeseries_lattice_segments")
    time_lattice_segments = segments if isinstance(segments, list) else []
    time_resolution_by_variable_time = _time_resolution_by_variable_time(time_lattice_segments, variables, reasons)
    lattice_too_large = _has_reason_code(reasons, REASON_TIMESERIES_LATTICE_TOO_LARGE)
    if not lattice_too_large:
        _validate_time_lattice_declared_variable_bounds(
            time_resolution_by_variable_time,
            variables,
            parsed_times.get("start_time"),
            parsed_times.get("end_time"),
            reasons,
        )
    station_ids = row_context.get("station_ids")
    known_station_ids = station_ids if isinstance(station_ids, set) else set()
    valid_times: list[datetime] = []
    actual_tuples: list[tuple[str, str, str]] = []

    for index, row in enumerate(rows):
        _row_text_matches(
            row,
            "forcing_version_id",
            manifest.get("forcing_version_id"),
            "station_timeseries",
            index,
            reasons,
            row_diagnostics,
        )
        _row_text_matches(
            row,
            "basin_version_id",
            manifest.get("basin_version_id"),
            "station_timeseries",
            index,
            reasons,
            row_diagnostics,
        )
        _row_source_matches(row, manifest.get("source_id"), "station_timeseries", index, reasons, row_diagnostics)
        station_id = _row_text(row, "station_id", "station_timeseries", index, reasons, row_diagnostics)
        if station_id is not None and known_station_ids and station_id not in known_station_ids:
            row_diagnostics.add(
                REASON_IDENTITY_MISMATCH,
                "station_timeseries",
                "station_id",
                index,
                expected="station_inventory.station_id",
                actual=station_id,
            )

        valid_time = _row_time(row, "valid_time", "station_timeseries", index, reasons, row_diagnostics)
        if valid_time is not None:
            valid_times.append(valid_time)
            start_time = parsed_times.get("start_time")
            end_time = parsed_times.get("end_time")
            if start_time is not None and valid_time < start_time:
                row_diagnostics.add(REASON_TEMPORAL_WINDOW_INVALID, "station_timeseries", "valid_time", index)
            if end_time is not None and valid_time > end_time:
                row_diagnostics.add(REASON_TEMPORAL_WINDOW_INVALID, "station_timeseries", "valid_time", index)

        variable = _row_text(row, "variable", "station_timeseries", index, reasons, row_diagnostics)
        if variable is not None and variables and variable not in variables:
            row_diagnostics.add(
                REASON_IDENTITY_MISMATCH,
                "station_timeseries",
                "variable",
                index,
                expected=sorted(variables),
                actual=variable,
            )
        if station_id is not None and variable is not None and valid_time is not None:
            actual_tuples.append((station_id, variable, _format_time(valid_time)))
        unit = _row_text(row, "unit", "station_timeseries", index, reasons, row_diagnostics)
        if variable is not None and unit is not None and isinstance(units, Mapping):
            expected_unit = units.get(variable)
            if _present_text(expected_unit) and unit != expected_unit:
                row_diagnostics.add(
                    REASON_IDENTITY_MISMATCH,
                    "station_timeseries",
                    "unit",
                    index,
                    expected=expected_unit,
                    actual=unit,
                )
        native_resolution = _row_text(row, "native_resolution", "station_timeseries", index, reasons, row_diagnostics)
        if native_resolution is not None and valid_time is not None and variable is not None:
            expected_resolution = time_resolution_by_variable_time.get((variable, _format_time(valid_time)))
            if expected_resolution is not None and native_resolution != expected_resolution:
                row_diagnostics.add(
                    REASON_IDENTITY_MISMATCH,
                    "station_timeseries",
                    "native_resolution",
                    index,
                    expected=expected_resolution,
                    actual=native_resolution,
                )
        if not _finite_number(row.get("value")):
            row_diagnostics.add(REASON_FIELD_MISSING, "station_timeseries", "value", index)

    start_time = parsed_times.get("start_time")
    end_time = parsed_times.get("end_time")
    if not lattice_too_large and valid_times and start_time is not None and min(valid_times) != start_time:
        reasons.append(
            _reason(
                REASON_TEMPORAL_WINDOW_INVALID,
                field="payloads.station_timeseries.valid_time_coverage_start",
                expected=_format_time(start_time),
                actual=_format_time(min(valid_times)),
            )
        )
    if not lattice_too_large and valid_times and end_time is not None and max(valid_times) != end_time:
        reasons.append(
            _reason(
                REASON_TEMPORAL_WINDOW_INVALID,
                field="payloads.station_timeseries.valid_time_coverage_end",
                expected=_format_time(end_time),
                actual=_format_time(max(valid_times)),
            )
        )
    _validate_station_timeseries_lattice(time_resolution_by_variable_time, known_station_ids, actual_tuples, reasons)
    row_context["timeseries_variables"] = variables
    row_context["timeseries_units"] = units


def _validate_interpolation_weight_rows(
    rows: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    row_context: Mapping[str, Any],
    reasons: list[dict[str, Any]],
    row_diagnostics: _RowDiagnostics,
) -> None:
    station_ids = row_context.get("station_ids")
    known_station_ids = station_ids if isinstance(station_ids, set) else set()
    variables = row_context.get("timeseries_variables")
    known_variables = variables if isinstance(variables, set) else set()
    duplicate_key_fields = ("source_id", "grid_id", "model_id", "station_id", "variable", "grid_cell_id")
    direct_grid_duplicate_key_fields = ("source_id", "grid_id", "model_id", "station_id", "variable")
    row_keys: list[tuple[str, ...]] = []
    direct_grid_row_keys: list[tuple[str, ...]] = []

    for index, row in enumerate(rows):
        _row_source_matches(row, manifest.get("source_id"), "interpolation_weights", index, reasons, row_diagnostics)
        _row_text_matches(
            row,
            "model_id",
            manifest.get("model_id"),
            "interpolation_weights",
            index,
            reasons,
            row_diagnostics,
        )
        station_id = _row_text(row, "station_id", "interpolation_weights", index, reasons, row_diagnostics)
        if station_id is not None and known_station_ids and station_id not in known_station_ids:
            row_diagnostics.add(
                REASON_IDENTITY_MISMATCH,
                "interpolation_weights",
                "station_id",
                index,
                expected="station_inventory.station_id",
                actual=station_id,
            )
        variable = _row_text(row, "variable", "interpolation_weights", index, reasons, row_diagnostics)
        if variable is not None and known_variables and variable not in known_variables:
            row_diagnostics.add(
                REASON_IDENTITY_MISMATCH,
                "interpolation_weights",
                "variable",
                index,
                expected=sorted(known_variables),
                actual=variable,
            )
        _row_text(row, "grid_id", "interpolation_weights", index, reasons, row_diagnostics)
        _row_text(row, "grid_cell_id", "interpolation_weights", index, reasons, row_diagnostics)
        _row_text(row, "method", "interpolation_weights", index, reasons, row_diagnostics)
        if not _finite_number(row.get("weight")):
            row_diagnostics.add(REASON_FIELD_MISSING, "interpolation_weights", "weight", index)
        if all(_present_text(row.get(field)) for field in duplicate_key_fields):
            row_keys.append(tuple(str(row[field]) for field in duplicate_key_fields))
        method = row.get("method")
        if _present_text(method) and str(method).lower() == "direct_grid" and all(
            _present_text(row.get(field)) for field in direct_grid_duplicate_key_fields
        ):
            direct_grid_row_keys.append(tuple(str(row[field]) for field in direct_grid_duplicate_key_fields))

    _append_interp_weight_duplicate_reason(reasons, duplicate_key_fields, row_keys)
    _append_interp_weight_duplicate_reason(
        reasons,
        direct_grid_duplicate_key_fields,
        direct_grid_row_keys,
        method="direct_grid",
    )


def _append_interp_weight_duplicate_reason(
    reasons: list[dict[str, Any]],
    duplicate_key_fields: tuple[str, ...],
    row_keys: list[tuple[str, ...]],
    *,
    method: str | None = None,
) -> None:
    duplicate_keys = [key for key, count in Counter(row_keys).items() if count > 1]
    if not duplicate_keys:
        return
    details = {"method": method} if method is not None else {}
    reasons.append(
        _reason(
            REASON_INTERP_WEIGHT_DUPLICATE,
            field="payloads.interpolation_weights.rows",
            duplicate_count=len(duplicate_keys),
            samples=[
                dict(zip(duplicate_key_fields, key, strict=True))
                for key in sorted(duplicate_keys)[:MAX_LATTICE_SAMPLES]
            ],
            **details,
        )
    )


def _validate_time_lattice_declared_variable_bounds(
    time_resolution_by_variable_time: Mapping[tuple[str, str], str],
    declared_variables: set[str],
    start_time: datetime | None,
    end_time: datetime | None,
    reasons: list[dict[str, Any]],
) -> None:
    if not time_resolution_by_variable_time or not declared_variables or start_time is None or end_time is None:
        return

    times_by_variable: dict[str, list[datetime]] = {variable: [] for variable in declared_variables}
    for variable, valid_time in time_resolution_by_variable_time:
        if variable in times_by_variable:
            times_by_variable[variable].append(_parse_time(valid_time))

    missing_count = 0
    samples: list[dict[str, str]] = []
    expected_start = _format_time(start_time)
    expected_end = _format_time(end_time)
    for variable in sorted(declared_variables):
        variable_times = times_by_variable.get(variable, [])
        if not variable_times:
            missing_count += 1
            if len(samples) < MAX_LATTICE_SAMPLES:
                samples.append(
                    {
                        "variable": variable,
                        "missing": "time_lattice",
                        "expected_start": expected_start,
                        "expected_end": expected_end,
                    }
                )
            continue
        variable_start = min(variable_times)
        variable_end = max(variable_times)
        if variable_start != start_time:
            missing_count += 1
            if len(samples) < MAX_LATTICE_SAMPLES:
                samples.append(
                    {
                        "variable": variable,
                        "missing": "valid_time_start",
                        "expected": expected_start,
                        "actual": _format_time(variable_start),
                    }
                )
        if variable_end != end_time:
            missing_count += 1
            if len(samples) < MAX_LATTICE_SAMPLES:
                samples.append(
                    {
                        "variable": variable,
                        "missing": "valid_time_end",
                        "expected": expected_end,
                        "actual": _format_time(variable_end),
                    }
                )

    if missing_count:
        reasons.append(
            _reason(
                REASON_TIMESERIES_LATTICE_MISSING,
                field="payloads.station_timeseries.time_lattice",
                missing_count=missing_count,
                samples=samples,
            )
        )


def _validate_station_timeseries_lattice(
    time_resolution_by_variable_time: Mapping[tuple[str, str], str],
    station_ids: set[str],
    actual_tuples: list[tuple[str, str, str]],
    reasons: list[dict[str, Any]],
) -> None:
    if not station_ids or not time_resolution_by_variable_time:
        return

    expected_variable_times = sorted(time_resolution_by_variable_time)
    expected_count = len(station_ids) * len(expected_variable_times)
    if expected_count > MAX_TIMESERIES_LATTICE_TUPLES:
        _append_lattice_too_large_reason(
            reasons,
            station_count=len(station_ids),
            variable_time_point_count=len(expected_variable_times),
            expected_tuple_count=expected_count,
        )
        return

    actual_counts = Counter(actual_tuples)
    duplicate_count = 0
    duplicate_samples: list[tuple[str, str, str]] = []
    extra_count = 0
    extra_samples: list[tuple[str, str, str]] = []
    expected_actual_count = 0
    for item, count in actual_counts.items():
        if count > 1:
            duplicate_count += 1
            if len(duplicate_samples) < MAX_LATTICE_SAMPLES:
                duplicate_samples.append(item)
        station_id, variable, valid_time = item
        if station_id in station_ids and (variable, valid_time) in time_resolution_by_variable_time:
            expected_actual_count += 1
            continue
        extra_count += 1
        if len(extra_samples) < MAX_LATTICE_SAMPLES:
            extra_samples.append(item)

    if duplicate_count:
        reasons.append(
            _reason(
                REASON_TIMESERIES_LATTICE_DUPLICATE,
                field="payloads.station_timeseries.rows",
                duplicate_count=duplicate_count,
                samples=_tuple_samples(duplicate_samples),
            )
        )

    missing_count = expected_count - expected_actual_count
    if missing_count:
        reasons.append(
            _reason(
                REASON_TIMESERIES_LATTICE_MISSING,
                field="payloads.station_timeseries.rows",
                missing_count=missing_count,
                samples=_missing_lattice_samples(station_ids, expected_variable_times, actual_counts),
            )
        )

    if extra_count:
        reasons.append(
            _reason(
                REASON_TIMESERIES_LATTICE_EXTRA,
                field="payloads.station_timeseries.rows",
                extra_count=extra_count,
                samples=_tuple_samples(extra_samples),
            )
        )


def _time_resolution_by_variable_time(
    segments: list[TimeLatticeSegment],
    declared_variables: set[str],
    reasons: list[dict[str, Any]],
) -> dict[tuple[str, str], str]:
    if not segments or not declared_variables:
        return {}

    total_points = 0
    segment_point_counts: list[int] = []
    invalid = False
    for index, (variable_scope, start, end, _resolution_label, resolution) in enumerate(segments):
        delta = end - start
        if delta % resolution != timedelta(0):
            reasons.append(
                _reason(
                    REASON_TEMPORAL_WINDOW_INVALID,
                    field=f"payloads.station_timeseries.time_lattice[{index}].native_resolution",
                    detail="valid_time_start/end must align to native_resolution",
                )
            )
            invalid = True
            segment_point_counts.append(0)
            continue
        point_count = delta // resolution + 1
        segment_variables = _segment_variables(variable_scope, declared_variables)
        total_points += point_count * len(segment_variables)
        segment_point_counts.append(point_count)
        if total_points > MAX_TIMESERIES_LATTICE_TIME_POINTS:
            _append_lattice_too_large_reason(reasons, expected_variable_time_point_count=total_points)
            return {}
    if invalid:
        return {}

    time_resolution_by_variable_time: dict[tuple[str, str], str] = {}
    duplicate_variable_times: list[dict[str, str]] = []
    duplicate_count = 0
    for (variable_scope, start, _end, resolution_label, resolution), point_count in zip(
        segments,
        segment_point_counts,
        strict=True,
    ):
        for variable in sorted(_segment_variables(variable_scope, declared_variables)):
            current = start
            for _ in range(point_count):
                formatted_time = _format_time(current)
                key = (variable, formatted_time)
                if key in time_resolution_by_variable_time:
                    duplicate_count += 1
                    if len(duplicate_variable_times) < MAX_LATTICE_SAMPLES:
                        duplicate_variable_times.append({"variable": variable, "valid_time": formatted_time})
                else:
                    time_resolution_by_variable_time[key] = resolution_label
                current += resolution

    if duplicate_count:
        reasons.append(
            _reason(
                REASON_TIMESERIES_LATTICE_DUPLICATE,
                field="payloads.station_timeseries.time_lattice",
                duplicate_count=duplicate_count,
                samples=duplicate_variable_times,
            )
        )
        return {}
    return time_resolution_by_variable_time


def _segment_variables(variable_scope: frozenset[str] | None, declared_variables: set[str]) -> set[str]:
    if variable_scope is None:
        return set(declared_variables)
    return {variable for variable in variable_scope if variable in declared_variables}


def _append_lattice_too_large_reason(reasons: list[dict[str, Any]], **details: Any) -> None:
    reasons.append(
        _reason(
            REASON_TIMESERIES_LATTICE_TOO_LARGE,
            field="payloads.station_timeseries.time_lattice",
            max_tuple_count=MAX_TIMESERIES_LATTICE_TUPLES,
            max_time_point_count=MAX_TIMESERIES_LATTICE_TIME_POINTS,
            **details,
        )
    )


def _missing_lattice_samples(
    station_ids: set[str],
    expected_variable_times: list[tuple[str, str]],
    actual_counts: Counter[tuple[str, str, str]],
) -> list[dict[str, str]]:
    samples: list[tuple[str, str, str]] = []
    for station_id in sorted(station_ids):
        for variable, valid_time in expected_variable_times:
            item = (station_id, variable, valid_time)
            if item in actual_counts:
                continue
            samples.append(item)
            if len(samples) >= MAX_LATTICE_SAMPLES:
                return _tuple_samples(samples)
    return _tuple_samples(samples)


def _tuple_samples(tuples: list[tuple[str, str, str]]) -> list[dict[str, str]]:
    return [
        {"station_id": station_id, "variable": variable, "valid_time": valid_time}
        for station_id, variable, valid_time in tuples[:MAX_LATTICE_SAMPLES]
    ]


def _payload_variable_set(payload: Mapping[str, Any]) -> set[str]:
    variables = payload.get("variables")
    if not isinstance(variables, list):
        return set()
    return {str(variable) for variable in variables if _present_text(variable)}


def _duplicate_text_values(values: list[Any]) -> list[str]:
    counts = Counter(str(value) for value in values if _present_text(value))
    return sorted(value for value, count in counts.items() if count > 1)


def _has_reason_code(reasons: list[dict[str, Any]], code: str) -> bool:
    return any(reason.get("code") == code for reason in reasons)


def _row_text(
    row: Mapping[str, Any],
    field: str,
    role: str,
    index: int,
    reasons: list[dict[str, Any]],
    row_diagnostics: _RowDiagnostics | None = None,
) -> str | None:
    value = row.get(field)
    if not _present_text(value):
        if row_diagnostics is not None:
            row_diagnostics.add(REASON_FIELD_MISSING, role, field, index)
        else:
            reasons.append(_reason(REASON_FIELD_MISSING, field=f"payloads.{role}.rows[{index}].{field}", role=role))
        return None
    return str(value)


def _row_text_matches(
    row: Mapping[str, Any],
    field: str,
    expected: Any,
    role: str,
    index: int,
    reasons: list[dict[str, Any]],
    row_diagnostics: _RowDiagnostics | None = None,
) -> None:
    actual = _row_text(row, field, role, index, reasons, row_diagnostics)
    if actual is None or not _present_text(expected):
        return
    if actual != str(expected):
        if row_diagnostics is not None:
            row_diagnostics.add(
                REASON_IDENTITY_MISMATCH,
                role,
                field,
                index,
                expected=str(expected),
                actual=actual,
            )
        else:
            reasons.append(
                _reason(
                    REASON_IDENTITY_MISMATCH,
                    field=f"payloads.{role}.rows[{index}].{field}",
                    expected=str(expected),
                    actual=actual,
                )
            )


def _row_source_matches(
    row: Mapping[str, Any],
    expected: Any,
    role: str,
    index: int,
    reasons: list[dict[str, Any]],
    row_diagnostics: _RowDiagnostics | None = None,
) -> None:
    actual = _row_text(row, "source_id", role, index, reasons, row_diagnostics)
    if actual is None or not _present_text(expected):
        return
    if actual.lower() != str(expected).lower():
        if row_diagnostics is not None:
            row_diagnostics.add(
                REASON_IDENTITY_MISMATCH,
                role,
                "source_id",
                index,
                expected=str(expected),
                actual=actual,
            )
        else:
            reasons.append(
                _reason(
                    REASON_IDENTITY_MISMATCH,
                    field=f"payloads.{role}.rows[{index}].source_id",
                    expected=str(expected),
                    actual=actual,
                )
            )


def _row_time(
    row: Mapping[str, Any],
    field: str,
    role: str,
    index: int,
    reasons: list[dict[str, Any]],
    row_diagnostics: _RowDiagnostics | None = None,
) -> datetime | None:
    value = _row_text(row, field, role, index, reasons, row_diagnostics)
    if value is None:
        return None
    try:
        return _parse_time(value)
    except ValueError as error:
        if row_diagnostics is not None:
            row_diagnostics.add(REASON_TEMPORAL_FIELD_MALFORMED, role, field, index, detail=str(error))
        else:
            reasons.append(
                _reason(
                    REASON_TEMPORAL_FIELD_MALFORMED,
                    field=f"payloads.{role}.rows[{index}].{field}",
                    detail=str(error),
                )
            )
        return None


def _has_coordinate_evidence(row: Mapping[str, Any]) -> bool:
    if _finite_number(row.get("longitude")) and _finite_number(row.get("latitude")):
        return True
    geometry = row.get("geometry")
    return isinstance(geometry, Mapping) and geometry != {}


def _finite_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


def _required_lattice_time(
    segment: Mapping[str, Any],
    field: str,
    field_prefix: str,
    reasons: list[dict[str, Any]],
) -> datetime | None:
    value = segment.get(field)
    detail_field = f"{field_prefix}.{field}"
    if not _present_text(value):
        reasons.append(_reason(REASON_TEMPORAL_FIELD_MISSING, field=detail_field))
        return None
    try:
        return _parse_time(str(value))
    except ValueError as error:
        reasons.append(_reason(REASON_TEMPORAL_FIELD_MALFORMED, field=detail_field, detail=str(error)))
        return None


def _optional_manifest_time(manifest: Mapping[str, Any], field: str) -> datetime | None:
    value = manifest.get(field)
    if not _present_text(value):
        return None
    try:
        return _parse_time(str(value))
    except ValueError:
        return None


def _table_row_counts(manifest: Mapping[str, Any], reasons: list[dict[str, Any]]) -> dict[str, int]:
    raw = manifest.get("table_row_counts")
    if not isinstance(raw, Mapping):
        for table in TABLE_ROW_COUNT_FIELDS:
            reasons.append(_reason(REASON_ROW_COUNT_MISSING, field=f"table_row_counts.{table}", table=table))
        return {}

    table_row_counts: dict[str, int] = {}
    for table in TABLE_ROW_COUNT_FIELDS:
        count = _positive_int(raw.get(table))
        if count is None:
            reasons.append(_reason(REASON_ROW_COUNT_MISSING, field=f"table_row_counts.{table}", table=table))
            continue
        table_row_counts[table] = count
    return table_row_counts


def _validate_row_count_evidence(
    manifest: Mapping[str, Any],
    payload_evidence: Mapping[str, Mapping[str, Any]],
    table_row_counts: Mapping[str, int],
    reasons: list[dict[str, Any]],
) -> None:
    station_count = _positive_int(manifest.get("station_count"))
    if station_count is None:
        reasons.append(_reason(REASON_ROW_COUNT_MISSING, field="station_count", table="met.met_station"))
    elif table_row_counts.get("met.met_station") is not None and station_count != table_row_counts["met.met_station"]:
        reasons.append(
            _reason(
                REASON_STATION_COUNT_MISMATCH,
                field="station_count",
                table="met.met_station",
                expected=station_count,
                actual=table_row_counts["met.met_station"],
            )
        )

    if table_row_counts.get("met.forcing_version") is not None and table_row_counts["met.forcing_version"] != 1:
        reasons.append(
            _reason(
                REASON_ROW_COUNT_MISMATCH,
                field="table_row_counts.met.forcing_version",
                table="met.forcing_version",
                expected=1,
                actual=table_row_counts["met.forcing_version"],
            )
        )

    for role, table in PAYLOAD_TABLES.items():
        payload = payload_evidence.get(role)
        if not payload:
            continue
        expected = table_row_counts.get(table)
        row_count = payload.get("row_count")
        if expected is not None and row_count is not None and expected != row_count:
            reasons.append(
                _reason(
                    REASON_ROW_COUNT_MISMATCH,
                    field=f"payloads.{role}.row_count",
                    role=role,
                    table=table,
                    expected=expected,
                    actual=row_count,
                )
            )


def _normalize_object_key(
    store: LocalObjectStore,
    uri: Any,
    reasons: list[dict[str, Any]],
    reason_code: str,
    field: str,
    *,
    role: str | None = None,
) -> str | None:
    if not _present_text(uri):
        missing_code = (
            REASON_PAYLOAD_MISSING
            if reason_code == REASON_PAYLOAD_PATH_UNSAFE
            else REASON_PACKAGE_MISSING
            if reason_code == REASON_PACKAGE_PATH_UNSAFE
            else reason_code
        )
        reasons.append(_reason(missing_code, field=field, role=role))
        return None
    try:
        key = store.normalize_key(str(uri))
    except Exception as error:
        reasons.append(_reason(reason_code, field=field, role=role, detail=str(error)))
        return None
    validation = validate_object_path(key)
    if not validation.valid:
        reasons.append(_reason(reason_code, field=field, role=role, detail=validation.error))
        return None
    return key


def _manifest_key(manifest_path: str | Path, root: Path) -> str:
    candidate = Path(manifest_path).expanduser()
    if candidate.is_absolute() or candidate.exists():
        absolute_candidate = candidate if candidate.is_absolute() else Path.cwd() / candidate
        absolute_root = root if root.is_absolute() else Path.cwd() / root
        return absolute_candidate.relative_to(absolute_root).as_posix()
    return str(manifest_path)


def _safe_manifest_uri(manifest_path: str | Path) -> str:
    return Path(manifest_path).name if Path(manifest_path).is_absolute() else str(manifest_path)


def _parse_time(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_duration(value: str) -> timedelta | None:
    match = re.fullmatch(r"\s*(\d+)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\s*", value, re.IGNORECASE)
    if match is None:
        return None
    amount = int(match.group(1))
    if amount < 1:
        return None
    unit = match.group(2).lower()
    if unit.startswith("h"):
        return timedelta(hours=amount)
    return timedelta(minutes=amount)


def _compact_cycle_token(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%d%H")


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 1:
        return value
    return None


def _valid_checksum(value: str) -> bool:
    return bool(_SHA256_RE.fullmatch(value))


def _present_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _reason(code: str, **details: Any) -> dict[str, Any]:
    clean_details = {key: value for key, value in details.items() if value not in (None, "")}
    return {"code": code, **clean_details}


def _result(
    manifest: Mapping[str, Any],
    reasons: list[dict[str, Any]],
    *,
    evidence: Mapping[str, Any] | None = None,
    manifest_uri: str | None = None,
) -> dict[str, Any]:
    result = {
        "available": not reasons,
        "status": "available" if not reasons else "unavailable",
        "contract_id": manifest.get("contract_id"),
        "schema_version": manifest.get("schema_version"),
        "run_id": manifest.get("run_id"),
        "forcing_version_id": manifest.get("forcing_version_id"),
        "manifest_uri": manifest_uri,
        "unavailable_reasons": reasons,
        "evidence": dict(evidence or {}),
    }
    return redact_payload(result)
