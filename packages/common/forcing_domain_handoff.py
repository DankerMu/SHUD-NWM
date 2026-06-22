from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.object_store import LocalObjectStore
from packages.common.redaction import redact_payload
from packages.common.storage import validate_object_path

CONTRACT_ID = "nhms.forcing_domain_handoff.v1"
SCHEMA_VERSION = "1.0"
MAX_HANDOFF_MANIFEST_BYTES = 1024 * 1024
MAX_HANDOFF_PAYLOAD_BYTES = 8 * 1024 * 1024

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
REASON_PAYLOAD_MISSING = "HANDOFF_PAYLOAD_MISSING"
REASON_PAYLOAD_CHECKSUM_MISSING = "HANDOFF_PAYLOAD_CHECKSUM_MISSING"
REASON_PAYLOAD_CHECKSUM_MISMATCH = "HANDOFF_PAYLOAD_CHECKSUM_MISMATCH"
REASON_PAYLOAD_PATH_UNSAFE = "HANDOFF_PAYLOAD_PATH_UNSAFE"
REASON_PAYLOAD_OUTSIDE_PACKAGE = "HANDOFF_PAYLOAD_OUTSIDE_PACKAGE"
REASON_ROW_COUNT_MISSING = "HANDOFF_ROW_COUNT_MISSING"
REASON_ROW_COUNT_MISMATCH = "HANDOFF_ROW_COUNT_MISMATCH"
REASON_STATION_COUNT_MISMATCH = "HANDOFF_STATION_COUNT_MISMATCH"
REASON_OBJECT_STORE_ROOT_UNAVAILABLE = "HANDOFF_OBJECT_STORE_ROOT_UNAVAILABLE"
REASON_MANIFEST_UNREADABLE = "HANDOFF_MANIFEST_UNREADABLE"
REASON_MANIFEST_MALFORMED = "HANDOFF_MANIFEST_MALFORMED"

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

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def validate_forcing_domain_handoff_path(
    manifest_path: str | Path,
    *,
    object_store_root: str | Path,
    object_store_prefix: str = "",
) -> dict[str, Any]:
    """Validate one declared forcing-domain handoff manifest without writing files."""

    root = Path(object_store_root).expanduser()
    if not root.is_dir():
        return _result(
            {},
            [
                _reason(
                    REASON_OBJECT_STORE_ROOT_UNAVAILABLE,
                    field="object_store_root",
                )
            ],
        )

    store = LocalObjectStore(root, object_store_prefix)
    try:
        manifest_key = _manifest_key(manifest_path, root)
        manifest_bytes = store.read_bytes_limited(manifest_key, max_bytes=MAX_HANDOFF_MANIFEST_BYTES)
    except Exception as error:
        return _result(
            {},
            [_reason(REASON_MANIFEST_UNREADABLE, field="manifest_uri", detail=str(error))],
            manifest_uri=_safe_manifest_uri(manifest_path),
        )

    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return _result(
            {},
            [_reason(REASON_MANIFEST_MALFORMED, field="manifest_uri", detail=str(error))],
            manifest_uri=manifest_key,
        )
    if not isinstance(manifest, Mapping):
        return _result(
            {},
            [_reason(REASON_MANIFEST_MALFORMED, field="manifest_uri", detail="manifest root must be an object")],
            manifest_uri=manifest_key,
        )
    return validate_forcing_domain_handoff(manifest, store=store, manifest_uri=manifest_key)


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
    _validate_compatibility_uri_fields(manifest, reasons)

    package_key = _validate_package(manifest, store, reasons)
    package_dir = Path(package_key).parent.as_posix() if package_key else None
    payload_evidence = _validate_payloads(manifest, store, package_dir, reasons)

    table_row_counts = _table_row_counts(manifest, reasons)
    _validate_row_count_evidence(manifest, payload_evidence, table_row_counts, reasons)

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
            "checksum_sha256": manifest.get("forcing_package_checksum_sha256"),
            "station_count": manifest.get("station_count"),
        },
        "payloads": payload_evidence,
        "table_row_counts": table_row_counts,
    }
    return _result(manifest, reasons, evidence=evidence, manifest_uri=manifest_uri)


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


def _validate_compatibility_uri_fields(manifest: Mapping[str, Any], reasons: list[dict[str, Any]]) -> None:
    for field in COMPATIBILITY_URI_FIELDS:
        if not _present_text(manifest.get(field)):
            reasons.append(_reason(REASON_FIELD_MISSING, field=field))


def _validate_package(
    manifest: Mapping[str, Any],
    store: LocalObjectStore,
    reasons: list[dict[str, Any]],
) -> str | None:
    package_uri = manifest.get("forcing_package_uri")
    package_checksum = manifest.get("forcing_package_checksum_sha256")
    if not _present_text(package_checksum) or not _valid_checksum(str(package_checksum)):
        reasons.append(_reason(REASON_PACKAGE_CHECKSUM_MISSING, field="forcing_package_checksum_sha256"))

    package_key = _normalize_object_key(store, package_uri, reasons, REASON_PACKAGE_PATH_UNSAFE, "forcing_package_uri")
    if package_key is None:
        return None

    if not _present_text(package_checksum) or not _valid_checksum(str(package_checksum)):
        return package_key

    try:
        _size, actual_checksum = store.size_and_checksum_limited(package_key, max_bytes=MAX_HANDOFF_PAYLOAD_BYTES)
    except Exception as error:
        reasons.append(_reason(REASON_PACKAGE_MISSING, field="forcing_package_uri", detail=str(error)))
        return package_key
    if actual_checksum != package_checksum:
        reasons.append(
            _reason(
                REASON_PACKAGE_CHECKSUM_MISMATCH,
                field="forcing_package_checksum_sha256",
                expected=str(package_checksum),
                actual=actual_checksum,
            )
        )
    return package_key


def _validate_payloads(
    manifest: Mapping[str, Any],
    store: LocalObjectStore,
    package_dir: str | None,
    reasons: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    payloads = manifest.get("payloads")
    if not isinstance(payloads, Mapping):
        for role in PAYLOAD_TABLES:
            reasons.append(_reason(REASON_PAYLOAD_MISSING, field=f"payloads.{role}", role=role))
        return {}

    evidence: dict[str, dict[str, Any]] = {}
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
        if role == "station_timeseries":
            _validate_station_timeseries_metadata(payload, manifest, reasons)
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
        if package_dir is not None and not payload_key.startswith(f"{package_dir}/payloads/"):
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
            size, actual_checksum = store.size_and_checksum_limited(payload_key, max_bytes=MAX_HANDOFF_PAYLOAD_BYTES)
            content = store.read_bytes_limited(payload_key, max_bytes=MAX_HANDOFF_PAYLOAD_BYTES)
        except Exception as error:
            reasons.append(
                _reason(REASON_PAYLOAD_MISSING, field=f"payloads.{role}.uri", role=role, detail=str(error))
            )
            continue
        actual_row_count = _json_record_count(content)
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
        if row_count is not None and actual_row_count is not None and row_count != actual_row_count:
            reasons.append(
                _reason(
                    REASON_ROW_COUNT_MISMATCH,
                    field=f"payloads.{role}.row_count",
                    role=role,
                    table=table,
                    expected=row_count,
                    actual=actual_row_count,
                )
            )
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
) -> None:
    valid_time_start = _payload_time(payload, "valid_time_start", reasons)
    valid_time_end = _payload_time(payload, "valid_time_end", reasons)
    if valid_time_start is not None and valid_time_end is not None and valid_time_start > valid_time_end:
        reasons.append(_reason(REASON_TEMPORAL_WINDOW_INVALID, field="payloads.station_timeseries.valid_time_window"))

    start_time = _optional_manifest_time(manifest, "start_time")
    end_time = _optional_manifest_time(manifest, "end_time")
    if valid_time_start is not None and start_time is not None and valid_time_start < start_time:
        reasons.append(
            _reason(
                REASON_TEMPORAL_WINDOW_INVALID,
                field="payloads.station_timeseries.valid_time_start",
            )
        )
    if valid_time_end is not None and end_time is not None and valid_time_end > end_time:
        reasons.append(
            _reason(
                REASON_TEMPORAL_WINDOW_INVALID,
                field="payloads.station_timeseries.valid_time_end",
            )
        )

    variables = payload.get("variables")
    if not isinstance(variables, list) or not variables or not all(_present_text(item) for item in variables):
        reasons.append(_reason(REASON_FIELD_MISSING, field="payloads.station_timeseries.variables"))
        variables = []
    units = payload.get("units")
    if not isinstance(units, Mapping):
        reasons.append(_reason(REASON_FIELD_MISSING, field="payloads.station_timeseries.units"))
    else:
        for variable in variables:
            if not _present_text(units.get(variable)):
                reasons.append(
                    _reason(REASON_FIELD_MISSING, field=f"payloads.station_timeseries.units.{variable}")
                )
    if not _present_text(payload.get("native_resolution")):
        reasons.append(_reason(REASON_FIELD_MISSING, field="payloads.station_timeseries.native_resolution"))


def _payload_time(payload: Mapping[str, Any], field: str, reasons: list[dict[str, Any]]) -> datetime | None:
    value = payload.get(field)
    detail_field = f"payloads.station_timeseries.{field}"
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


def _json_record_count(content: bytes) -> int | None:
    try:
        payload = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, Mapping):
        rows = payload.get("rows")
        if isinstance(rows, list):
            return len(rows)
    return None


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
