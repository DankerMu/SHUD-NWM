from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.object_store import LocalObjectStore, sha256_bytes
from packages.common.redaction import redact_payload
from packages.common.storage import validate_object_path

CONTRACT_ID = "nhms.forcing_domain_handoff.v1"
PACKAGE_CONTRACT_ID = "nhms.forcing_domain_handoff.package.v1"
SCHEMA_VERSION = "1.0"
MAX_HANDOFF_MANIFEST_BYTES = 1024 * 1024
MAX_HANDOFF_PAYLOAD_BYTES = 8 * 1024 * 1024

PACKAGE_MANIFEST_URI_FIELD = "forcing_domain_package_manifest_uri"
PACKAGE_MANIFEST_CHECKSUM_FIELD = "forcing_domain_package_manifest_checksum_sha256"

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
    PACKAGE_MANIFEST_URI_FIELD,
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

    try:
        store = LocalObjectStore(root, object_store_prefix)
    except Exception as error:
        return _result(
            {},
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

    package = _validate_package(manifest, store, reasons)
    payload_evidence = (
        _validate_payloads(
            manifest,
            store,
            package["directory_key"],
            package["manifest"],
            parsed_times,
            reasons,
        )
        if package is not None and isinstance(package.get("manifest"), Mapping)
        else {}
    )

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
            "package_manifest_uri": manifest.get(PACKAGE_MANIFEST_URI_FIELD),
            "package_manifest_checksum_sha256": manifest.get(PACKAGE_MANIFEST_CHECKSUM_FIELD),
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
) -> dict[str, Any] | None:
    package = _normalize_package_dir_key(store, manifest.get("forcing_package_uri"), reasons)
    if package is None:
        return None
    package_key, package_components = package
    if not _validate_package_path_identity(package_components, manifest, reasons):
        return {"directory_key": package_key, "manifest": None}

    manifest_key = _normalize_package_manifest_key(store, manifest, package_key, reasons)
    manifest_checksum = manifest.get(PACKAGE_MANIFEST_CHECKSUM_FIELD)
    if not _present_text(manifest_checksum) or not _valid_checksum(str(manifest_checksum)):
        reasons.append(_reason(REASON_PACKAGE_CHECKSUM_MISSING, field=PACKAGE_MANIFEST_CHECKSUM_FIELD))
        return {"directory_key": package_key, "manifest": None}

    if manifest_key is None:
        return {"directory_key": package_key, "manifest": None}

    try:
        content = store.read_bytes_limited(manifest_key, max_bytes=MAX_HANDOFF_MANIFEST_BYTES)
    except Exception as error:
        reasons.append(_reason(REASON_PACKAGE_MISSING, field=PACKAGE_MANIFEST_URI_FIELD, detail=str(error)))
        return {"directory_key": package_key, "manifest": None}

    actual_checksum = sha256_bytes(content)
    if actual_checksum != manifest_checksum:
        reasons.append(
            _reason(
                REASON_PACKAGE_CHECKSUM_MISMATCH,
                field=PACKAGE_MANIFEST_CHECKSUM_FIELD,
                expected=str(manifest_checksum),
                actual=actual_checksum,
            )
        )
        return {"directory_key": package_key, "manifest": None}

    try:
        package_manifest = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        reasons.append(_reason(REASON_PACKAGE_MANIFEST_MALFORMED, field=PACKAGE_MANIFEST_URI_FIELD, detail=str(error)))
        return {"directory_key": package_key, "manifest": None}
    if not isinstance(package_manifest, Mapping):
        reasons.append(
            _reason(
                REASON_PACKAGE_MANIFEST_MALFORMED,
                field=PACKAGE_MANIFEST_URI_FIELD,
                detail="package manifest root must be an object",
            )
        )
        return {"directory_key": package_key, "manifest": None}

    if _validate_package_manifest(package_manifest, manifest, reasons):
        return {"directory_key": package_key, "manifest_key": manifest_key, "manifest": package_manifest}
    return {"directory_key": package_key, "manifest_key": manifest_key, "manifest": None}


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
        manifest.get(PACKAGE_MANIFEST_URI_FIELD),
        reasons,
        REASON_PACKAGE_PATH_UNSAFE,
        PACKAGE_MANIFEST_URI_FIELD,
    )
    if manifest_key is None:
        return None
    if not manifest_key.startswith(f"{package_key}/") or manifest_key.startswith(f"{package_key}/payloads/"):
        reasons.append(_reason(REASON_PAYLOAD_OUTSIDE_PACKAGE, field=PACKAGE_MANIFEST_URI_FIELD))
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
        "timeseries_native_resolution": None,
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
        rows = _json_rows(content, role, reasons)
        actual_row_count = len(rows) if rows is not None else None
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
) -> None:
    valid_time_start = _payload_time(payload, "valid_time_start", reasons)
    valid_time_end = _payload_time(payload, "valid_time_end", reasons)
    if valid_time_start is not None and valid_time_end is not None and valid_time_start > valid_time_end:
        reasons.append(_reason(REASON_TEMPORAL_WINDOW_INVALID, field="payloads.station_timeseries.valid_time_window"))

    start_time = _optional_manifest_time(manifest, "start_time")
    end_time = _optional_manifest_time(manifest, "end_time")
    if valid_time_start is not None and start_time is not None and valid_time_start != start_time:
        reasons.append(
            _reason(
                REASON_TEMPORAL_WINDOW_INVALID,
                field="payloads.station_timeseries.valid_time_start",
                expected=_format_time(start_time),
                actual=_format_time(valid_time_start),
            )
        )
    if valid_time_end is not None and end_time is not None and valid_time_end != end_time:
        reasons.append(
            _reason(
                REASON_TEMPORAL_WINDOW_INVALID,
                field="payloads.station_timeseries.valid_time_end",
                expected=_format_time(end_time),
                actual=_format_time(valid_time_end),
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


def _validate_payload_rows(
    role: str,
    rows: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    payload: Mapping[str, Any],
    parsed_times: Mapping[str, datetime],
    row_context: dict[str, Any],
    reasons: list[dict[str, Any]],
) -> None:
    if role == "station_inventory":
        _validate_station_inventory_rows(rows, manifest, row_context, reasons)
    elif role == "station_timeseries":
        _validate_station_timeseries_rows(rows, manifest, payload, parsed_times, row_context, reasons)
    elif role == "interpolation_weights":
        _validate_interpolation_weight_rows(rows, manifest, row_context, reasons)


def _validate_station_inventory_rows(
    rows: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    row_context: dict[str, Any],
    reasons: list[dict[str, Any]],
) -> None:
    station_ids: set[str] = set()
    for index, row in enumerate(rows):
        station_id = _row_text(row, "station_id", "station_inventory", index, reasons)
        if station_id is not None:
            station_ids.add(station_id)
        _row_text_matches(
            row,
            "basin_version_id",
            manifest.get("basin_version_id"),
            "station_inventory",
            index,
            reasons,
        )
        if not _has_coordinate_evidence(row):
            reasons.append(
                _reason(
                    REASON_FIELD_MISSING,
                    field=f"payloads.station_inventory.rows[{index}].longitude/latitude",
                    role="station_inventory",
                )
            )

    station_count = _positive_int(manifest.get("station_count"))
    if station_count is not None and len(rows) != station_count:
        reasons.append(
            _reason(
                REASON_STATION_COUNT_MISMATCH,
                field="payloads.station_inventory.row_count",
                table="met.met_station",
                expected=station_count,
                actual=len(rows),
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
) -> None:
    variables = _payload_variable_set(payload)
    units = payload.get("units") if isinstance(payload.get("units"), Mapping) else {}
    native_resolution_value = payload.get("native_resolution")
    native_resolution = str(native_resolution_value) if _present_text(native_resolution_value) else None
    station_ids = row_context.get("station_ids")
    known_station_ids = station_ids if isinstance(station_ids, set) else set()
    valid_times: list[datetime] = []

    for index, row in enumerate(rows):
        _row_text_matches(
            row,
            "forcing_version_id",
            manifest.get("forcing_version_id"),
            "station_timeseries",
            index,
            reasons,
        )
        _row_text_matches(
            row,
            "basin_version_id",
            manifest.get("basin_version_id"),
            "station_timeseries",
            index,
            reasons,
        )
        _row_source_matches(row, manifest.get("source_id"), "station_timeseries", index, reasons)
        station_id = _row_text(row, "station_id", "station_timeseries", index, reasons)
        if station_id is not None and known_station_ids and station_id not in known_station_ids:
            reasons.append(
                _reason(
                    REASON_IDENTITY_MISMATCH,
                    field=f"payloads.station_timeseries.rows[{index}].station_id",
                    expected="station_inventory.station_id",
                    actual=station_id,
                )
            )

        valid_time = _row_time(row, "valid_time", "station_timeseries", index, reasons)
        if valid_time is not None:
            valid_times.append(valid_time)
            start_time = parsed_times.get("start_time")
            end_time = parsed_times.get("end_time")
            if start_time is not None and valid_time < start_time:
                reasons.append(
                    _reason(
                        REASON_TEMPORAL_WINDOW_INVALID,
                        field=f"payloads.station_timeseries.rows[{index}].valid_time",
                    )
                )
            if end_time is not None and valid_time > end_time:
                reasons.append(
                    _reason(
                        REASON_TEMPORAL_WINDOW_INVALID,
                        field=f"payloads.station_timeseries.rows[{index}].valid_time",
                    )
                )

        variable = _row_text(row, "variable", "station_timeseries", index, reasons)
        if variable is not None and variables and variable not in variables:
            reasons.append(
                _reason(
                    REASON_IDENTITY_MISMATCH,
                    field=f"payloads.station_timeseries.rows[{index}].variable",
                    expected=sorted(variables),
                    actual=variable,
                )
            )
        unit = _row_text(row, "unit", "station_timeseries", index, reasons)
        if variable is not None and unit is not None and isinstance(units, Mapping):
            expected_unit = units.get(variable)
            if _present_text(expected_unit) and unit != expected_unit:
                reasons.append(
                    _reason(
                        REASON_IDENTITY_MISMATCH,
                        field=f"payloads.station_timeseries.rows[{index}].unit",
                        expected=expected_unit,
                        actual=unit,
                    )
                )
        _row_text_matches(
            row,
            "native_resolution",
            native_resolution,
            "station_timeseries",
            index,
            reasons,
        )
        if not _finite_number(row.get("value")):
            reasons.append(
                _reason(REASON_FIELD_MISSING, field=f"payloads.station_timeseries.rows[{index}].value")
            )

    start_time = parsed_times.get("start_time")
    end_time = parsed_times.get("end_time")
    if valid_times and start_time is not None and min(valid_times) != start_time:
        reasons.append(
            _reason(
                REASON_TEMPORAL_WINDOW_INVALID,
                field="payloads.station_timeseries.valid_time_coverage_start",
                expected=_format_time(start_time),
                actual=_format_time(min(valid_times)),
            )
        )
    if valid_times and end_time is not None and max(valid_times) != end_time:
        reasons.append(
            _reason(
                REASON_TEMPORAL_WINDOW_INVALID,
                field="payloads.station_timeseries.valid_time_coverage_end",
                expected=_format_time(end_time),
                actual=_format_time(max(valid_times)),
            )
        )
    row_context["timeseries_variables"] = variables
    row_context["timeseries_units"] = units
    row_context["timeseries_native_resolution"] = native_resolution


def _validate_interpolation_weight_rows(
    rows: list[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    row_context: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> None:
    station_ids = row_context.get("station_ids")
    known_station_ids = station_ids if isinstance(station_ids, set) else set()
    variables = row_context.get("timeseries_variables")
    known_variables = variables if isinstance(variables, set) else set()

    for index, row in enumerate(rows):
        _row_source_matches(row, manifest.get("source_id"), "interpolation_weights", index, reasons)
        _row_text_matches(
            row,
            "model_id",
            manifest.get("model_id"),
            "interpolation_weights",
            index,
            reasons,
        )
        station_id = _row_text(row, "station_id", "interpolation_weights", index, reasons)
        if station_id is not None and known_station_ids and station_id not in known_station_ids:
            reasons.append(
                _reason(
                    REASON_IDENTITY_MISMATCH,
                    field=f"payloads.interpolation_weights.rows[{index}].station_id",
                    expected="station_inventory.station_id",
                    actual=station_id,
                )
            )
        variable = _row_text(row, "variable", "interpolation_weights", index, reasons)
        if variable is not None and known_variables and variable not in known_variables:
            reasons.append(
                _reason(
                    REASON_IDENTITY_MISMATCH,
                    field=f"payloads.interpolation_weights.rows[{index}].variable",
                    expected=sorted(known_variables),
                    actual=variable,
                )
            )
        _row_text(row, "grid_id", "interpolation_weights", index, reasons)
        _row_text(row, "grid_cell_id", "interpolation_weights", index, reasons)
        _row_text(row, "method", "interpolation_weights", index, reasons)
        if not _finite_number(row.get("weight")):
            reasons.append(
                _reason(REASON_FIELD_MISSING, field=f"payloads.interpolation_weights.rows[{index}].weight")
            )


def _payload_variable_set(payload: Mapping[str, Any]) -> set[str]:
    variables = payload.get("variables")
    if not isinstance(variables, list):
        return set()
    return {str(variable) for variable in variables if _present_text(variable)}


def _row_text(
    row: Mapping[str, Any],
    field: str,
    role: str,
    index: int,
    reasons: list[dict[str, Any]],
) -> str | None:
    value = row.get(field)
    if not _present_text(value):
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
) -> None:
    actual = _row_text(row, field, role, index, reasons)
    if actual is None or not _present_text(expected):
        return
    if actual != str(expected):
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
) -> None:
    actual = _row_text(row, "source_id", role, index, reasons)
    if actual is None or not _present_text(expected):
        return
    if actual.lower() != str(expected).lower():
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
) -> datetime | None:
    value = _row_text(row, field, role, index, reasons)
    if value is None:
        return None
    try:
        return _parse_time(value)
    except ValueError as error:
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
