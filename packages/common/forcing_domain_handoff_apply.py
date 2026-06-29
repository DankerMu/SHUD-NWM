from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from psycopg2.extras import Json, execute_values

from packages.common.forcing_domain_handoff import (
    FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD,
    parse_forcing_domain_handoff_path,
)
from packages.common.redaction import redact_payload, redact_text
from packages.common.source_identity import normalize_source_id

APPLY_MODE = "object_store_forcing_domain_handoff"
APPLY_SAVEPOINT_NAME = "nhms_forcing_domain_handoff_apply"
STATION_COORDINATE_TOLERANCE = 1e-9

REASON_APPLY_CONNECTION_MISSING = "HANDOFF_APPLY_CONNECTION_MISSING"
REASON_APPLY_FIELD_MISSING = "HANDOFF_APPLY_FIELD_MISSING"
REASON_APPLY_SHAPE_CONFLICT = "HANDOFF_APPLY_SHAPE_CONFLICT"
REASON_APPLY_CHECKSUM_MISMATCH = "HANDOFF_APPLY_CHECKSUM_MISMATCH"
REASON_APPLY_STATION_COORDINATE_MISMATCH = "HANDOFF_APPLY_STATION_COORDINATE_MISMATCH"
REASON_APPLY_STATION_CONFLICT = "HANDOFF_APPLY_STATION_CONFLICT"
REASON_APPLY_FORCING_VERSION_CONFLICT = "HANDOFF_APPLY_FORCING_VERSION_CONFLICT"
REASON_APPLY_SQL_FAILURE = "HANDOFF_APPLY_SQL_FAILURE"

TARGET_TABLES = (
    "met.forcing_version",
    "met.met_station",
    "met.forcing_station_timeseries",
    "met.interp_weight",
)
FORCING_VERSION_COLUMNS = (
    "forcing_version_id",
    "model_id",
    "source_id",
    "cycle_time",
    "start_time",
    "end_time",
    "station_count",
    "forcing_package_uri",
    "checksum",
)
FORCING_VERSION_ROW_FIELDS = (*FORCING_VERSION_COLUMNS, "basin_id", "basin_version_id", "forcing_package_manifest_uri")
MET_STATION_REQUIRED_FIELDS = (
    "station_id",
    "basin_version_id",
    "station_name",
    "elevation_m",
    "station_role",
    "active_flag",
    "properties_json",
)
MET_STATION_SELECT_COLUMNS = (
    "station_id",
    "basin_version_id",
    "station_name",
    "longitude",
    "latitude",
    "elevation_m",
    "station_role",
    "active_flag",
    "properties_json",
)
FORCING_STATION_TIMESERIES_COLUMNS = (
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
)
INTERP_WEIGHT_COLUMNS = (
    "source_id",
    "grid_id",
    "model_id",
    "station_id",
    "variable",
    "grid_cell_id",
    "weight",
    "method",
    "grid_signature",
)


class ForcingDomainHandoffApplyError(RuntimeError):
    def __init__(self, reason: Mapping[str, Any]) -> None:
        self.reason = dict(reason)
        super().__init__(str(redact_payload(self.reason)))


def apply_forcing_domain_handoff(
    parser_envelope: Mapping[str, Any] | None,
    *,
    connection: Any | None = None,
    cursor: Any | None = None,
) -> dict[str, Any]:
    """Apply a parsed forcing-domain handoff envelope to the four target DB tables.

    Passing ``connection`` lets this helper own commit/rollback. Passing
    ``cursor`` means the caller owns the surrounding transaction.
    """

    if not isinstance(parser_envelope, Mapping):
        return _unavailable_report(
            status="unavailable",
            reasons=[_reason(REASON_APPLY_FIELD_MISSING, field="parser_envelope")],
            writes_performed=False,
        )
    if parser_envelope.get("available") is not True:
        return _parser_unavailable_report(parser_envelope)

    try:
        prepared, reasons = _prepare_apply_rows(parser_envelope)
    except ForcingDomainHandoffApplyError as error:
        return _unavailable_report(
            status="failed",
            reasons=[error.reason],
            parser_envelope=parser_envelope,
            identity=_identity_from_envelope(parser_envelope),
            writes_performed=False,
        )
    if reasons:
        return _unavailable_report(
            status="failed",
            reasons=reasons,
            parser_envelope=parser_envelope,
            identity=_identity_from_envelope(parser_envelope),
            writes_performed=False,
        )
    if cursor is None and connection is None:
        return _unavailable_report(
            status="unavailable",
            reasons=[_reason(REASON_APPLY_CONNECTION_MISSING, field="connection")],
            parser_envelope=parser_envelope,
            identity=prepared["identity"],
            writes_performed=False,
        )

    owns_transaction = cursor is None
    try:
        if owns_transaction:
            with connection.cursor() as owned_cursor:
                report = _apply_with_cursor(owned_cursor, prepared, parser_envelope, owns_transaction=True)
            connection.commit()
            return report
        _begin_apply_savepoint(cursor)
        report = _apply_with_cursor(cursor, prepared, parser_envelope, owns_transaction=False)
        _release_apply_savepoint(cursor)
        return report
    except ForcingDomainHandoffApplyError as error:
        if owns_transaction:
            connection.rollback()
        else:
            _rollback_apply_savepoint(cursor)
        return _unavailable_report(
            status="failed",
            reasons=[error.reason],
            parser_envelope=parser_envelope,
            identity=prepared["identity"],
            writes_performed=False,
        )
    except Exception as error:
        if owns_transaction:
            connection.rollback()
        else:
            _rollback_apply_savepoint(cursor)
        return _unavailable_report(
            status="failed",
            reasons=[
                _reason(
                    REASON_APPLY_SQL_FAILURE,
                    detail=redact_text(str(error)),
                    exception_type=type(error).__name__,
                )
            ],
            parser_envelope=parser_envelope,
            identity=prepared["identity"],
            writes_performed=False,
        )


def apply_forcing_domain_handoff_path(
    manifest_path: str | Path,
    *,
    object_store_root: str | Path,
    object_store_prefix: str = "",
    connection: Any | None = None,
    cursor: Any | None = None,
) -> dict[str, Any]:
    """Parse a declared handoff manifest path and apply it if the parser envelope is available."""

    parser_envelope = parse_forcing_domain_handoff_path(
        manifest_path,
        object_store_root=object_store_root,
        object_store_prefix=object_store_prefix,
    )
    return apply_forcing_domain_handoff(parser_envelope, connection=connection, cursor=cursor)


def _apply_with_cursor(
    cursor: Any,
    prepared: Mapping[str, Any],
    parser_envelope: Mapping[str, Any],
    *,
    owns_transaction: bool,
) -> dict[str, Any]:
    forcing_version = prepared["forcing_version"]
    stations = prepared["stations"]
    timeseries = prepared["timeseries"]
    interp_weights = prepared["interp_weights"]
    identity = prepared["identity"]

    _verify_existing_station_rows(cursor, stations)
    _upsert_forcing_version(cursor, forcing_version, parser_envelope)
    _upsert_met_stations(cursor, stations)
    _replace_forcing_station_timeseries(cursor, forcing_version["forcing_version_id"], timeseries)
    _replace_interp_weights(cursor, prepared["interp_scopes"], interp_weights)

    row_counts = _verify_apply_row_counts(cursor, prepared)
    return _success_report(
        parser_envelope=parser_envelope,
        identity=identity,
        row_counts=row_counts,
        apply_evidence={
            "transaction": "owned" if owns_transaction else "caller_owned",
            "coordinate_sources": dict(Counter(station["coordinate_source"] for station in stations)),
            "interp_weight_scopes": [
                {"source_id": source_id, "grid_id": grid_id, "model_id": model_id}
                for source_id, grid_id, model_id in prepared["interp_scopes"]
            ],
            "direct_grid_rows": sum(
                1 for row in interp_weights if str(row.get("method", "")).lower() == "direct_grid"
            ),
        },
    )


def _prepare_apply_rows(parser_envelope: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    reasons: list[dict[str, Any]] = []
    parsed = parser_envelope.get("parsed")
    if not isinstance(parsed, Mapping):
        return {}, [_reason(REASON_APPLY_FIELD_MISSING, field="parsed")]

    table_rows: dict[str, list[Mapping[str, Any]]] = {}
    for table in TARGET_TABLES:
        rows = parsed.get(table)
        if not isinstance(rows, list):
            reasons.append(_reason(REASON_APPLY_FIELD_MISSING, field=f"parsed.{table}", table=table))
            continue
        table_rows[table] = [row for row in rows if isinstance(row, Mapping)]
        if len(table_rows[table]) != len(rows):
            reasons.append(_reason(REASON_APPLY_SHAPE_CONFLICT, field=f"parsed.{table}", table=table))
    if reasons:
        return {}, reasons

    forcing_rows = table_rows["met.forcing_version"]
    if len(forcing_rows) != 1:
        reasons.append(
            _reason(
                REASON_APPLY_SHAPE_CONFLICT,
                field="parsed.met.forcing_version",
                expected=1,
                actual=len(forcing_rows),
            )
        )
        return {}, reasons

    forcing_version = _prepare_forcing_version_row(forcing_rows[0], parser_envelope, reasons)
    stations = _prepare_station_rows(table_rows["met.met_station"], reasons)
    timeseries = _prepare_timeseries_rows(table_rows["met.forcing_station_timeseries"], forcing_version, reasons)
    interp_weights = _prepare_interp_weight_rows(table_rows["met.interp_weight"], reasons, forcing_version)
    if reasons:
        return {}, reasons

    station_ids = {station["station_id"] for station in stations}
    _validate_child_station_ids(timeseries, station_ids, "met.forcing_station_timeseries", reasons)
    _validate_child_station_ids(interp_weights, station_ids, "met.interp_weight", reasons)
    if reasons:
        return {}, reasons

    interp_scopes = sorted(
        {
            (str(row["source_id"]), str(row["grid_id"]), str(row["model_id"]))
            for row in interp_weights
        }
    )
    return (
        {
            "identity": _identity_from_envelope(parser_envelope, forcing_version=forcing_version),
            "forcing_version": forcing_version,
            "stations": stations,
            "timeseries": timeseries,
            "interp_weights": interp_weights,
            "interp_scopes": interp_scopes,
            "expected_row_counts": {
                "met.forcing_version": 1,
                "met.met_station": len(stations),
                "met.forcing_station_timeseries": len(timeseries),
                "met.interp_weight": len(interp_weights),
            },
        },
        [],
    )


def _prepare_forcing_version_row(
    row: Mapping[str, Any],
    parser_envelope: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> dict[str, Any]:
    prepared = dict(row)
    for field in FORCING_VERSION_COLUMNS:
        if not _present(prepared.get(field)):
            reasons.append(_reason(REASON_APPLY_FIELD_MISSING, field=f"met.forcing_version.{field}"))
    _normalize_source_field(prepared, table="met.forcing_version", field="source_id", reasons=reasons)

    evidence = parser_envelope.get("evidence")
    forcing_evidence = evidence.get("forcing_version") if isinstance(evidence, Mapping) else None
    canonical_checksum = (
        forcing_evidence.get(FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD)
        if isinstance(forcing_evidence, Mapping)
        else None
    )
    parsed_checksum = prepared.get("checksum")
    if not _present(canonical_checksum):
        reasons.append(
            _reason(
                REASON_APPLY_FIELD_MISSING,
                field=f"evidence.forcing_version.{FORCING_PACKAGE_MANIFEST_CHECKSUM_FIELD}",
            )
        )
    elif parsed_checksum != canonical_checksum:
        reasons.append(
            _reason(
                REASON_APPLY_CHECKSUM_MISMATCH,
                field="met.forcing_version.checksum",
                expected=canonical_checksum,
                actual=parsed_checksum,
            )
        )
    return prepared


def _prepare_station_rows(rows: Sequence[Mapping[str, Any]], reasons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        station = dict(row)
        for field in MET_STATION_REQUIRED_FIELDS:
            if not _present(station.get(field)):
                reasons.append(_row_reason(REASON_APPLY_FIELD_MISSING, "met.met_station", field, index))
        if not isinstance(station.get("active_flag"), bool):
            reasons.append(_row_reason(REASON_APPLY_FIELD_MISSING, "met.met_station", "active_flag", index))
        if not isinstance(station.get("properties_json"), Mapping):
            reasons.append(_row_reason(REASON_APPLY_FIELD_MISSING, "met.met_station", "properties_json", index))
        if not _finite_number(station.get("elevation_m")):
            reasons.append(_row_reason(REASON_APPLY_FIELD_MISSING, "met.met_station", "elevation_m", index))

        station_id = station.get("station_id")
        if isinstance(station_id, str):
            if station_id in seen:
                reasons.append(_row_reason(REASON_APPLY_SHAPE_CONFLICT, "met.met_station", "station_id", index))
            seen.add(station_id)

        coordinates = _station_coordinates(station)
        if coordinates is None:
            reasons.append(
                _row_reason(
                    REASON_APPLY_FIELD_MISSING,
                    "met.met_station",
                    "longitude/latitude|geometry",
                    index,
                )
            )
            continue
        longitude, latitude, source = coordinates
        station["longitude"] = longitude
        station["latitude"] = latitude
        station["coordinate_source"] = source
        prepared.append(station)
    return prepared


def _prepare_timeseries_rows(
    rows: Sequence[Mapping[str, Any]],
    forcing_version: Mapping[str, Any],
    reasons: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    forcing_version_id = forcing_version.get("forcing_version_id")
    forcing_source_id = forcing_version.get("source_id")
    for index, row in enumerate(rows):
        item = dict(row)
        for field in FORCING_STATION_TIMESERIES_COLUMNS:
            if not _present(item.get(field)):
                reasons.append(_row_reason(REASON_APPLY_FIELD_MISSING, "met.forcing_station_timeseries", field, index))
        _normalize_source_field(
            item,
            table="met.forcing_station_timeseries",
            field="source_id",
            reasons=reasons,
            row_index=index,
        )
        if item.get("forcing_version_id") != forcing_version_id:
            reasons.append(
                _row_reason(
                    REASON_APPLY_SHAPE_CONFLICT,
                    "met.forcing_station_timeseries",
                    "forcing_version_id",
                    index,
                )
            )
        source_mismatch = (
            _present(item.get("source_id"))
            and _present(forcing_source_id)
            and item.get("source_id") != forcing_source_id
        )
        if source_mismatch:
            reasons.append(
                _row_reason(
                    REASON_APPLY_SHAPE_CONFLICT,
                    "met.forcing_station_timeseries",
                    "source_id",
                    index,
                )
            )
        key = (item.get("forcing_version_id"), item.get("station_id"), item.get("variable"), item.get("valid_time"))
        if key in seen:
            reasons.append(
                _row_reason(
                    REASON_APPLY_SHAPE_CONFLICT,
                    "met.forcing_station_timeseries",
                    "primary_key",
                    index,
                )
            )
        seen.add(key)
        if not _finite_number(item.get("value")):
            reasons.append(_row_reason(REASON_APPLY_FIELD_MISSING, "met.forcing_station_timeseries", "value", index))
        prepared.append(item)
    return prepared


def _prepare_interp_weight_rows(
    rows: Sequence[Mapping[str, Any]],
    reasons: list[dict[str, Any]],
    forcing_version: Mapping[str, Any],
) -> list[dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    forcing_source_id = forcing_version.get("source_id")
    for index, row in enumerate(rows):
        item = dict(row)
        for field in INTERP_WEIGHT_COLUMNS:
            if field == "grid_signature":
                continue
            if not _present(item.get(field)):
                reasons.append(_row_reason(REASON_APPLY_FIELD_MISSING, "met.interp_weight", field, index))
        _normalize_source_field(item, table="met.interp_weight", field="source_id", reasons=reasons, row_index=index)
        source_mismatch = (
            _present(item.get("source_id"))
            and _present(forcing_source_id)
            and item.get("source_id") != forcing_source_id
        )
        if source_mismatch:
            reasons.append(_row_reason(REASON_APPLY_SHAPE_CONFLICT, "met.interp_weight", "source_id", index))
        if "grid_signature" not in item:
            item["grid_signature"] = None

        key = (
            item.get("source_id"),
            item.get("grid_id"),
            item.get("model_id"),
            item.get("station_id"),
            item.get("variable"),
            item.get("grid_cell_id"),
        )
        if key in seen:
            reasons.append(_row_reason(REASON_APPLY_SHAPE_CONFLICT, "met.interp_weight", "unique_key", index))
        seen.add(key)
        if not _finite_number(item.get("weight")):
            reasons.append(_row_reason(REASON_APPLY_FIELD_MISSING, "met.interp_weight", "weight", index))

        method = str(item.get("method", ""))
        if method.lower() == "direct_grid":
            item["method"] = "direct_grid"
            if not _numbers_close(item.get("weight"), 1.0, tolerance=0.0):
                reasons.append(_row_reason(REASON_APPLY_FIELD_MISSING, "met.interp_weight", "weight", index))
            if not _present(item.get("grid_signature")):
                reasons.append(_row_reason(REASON_APPLY_FIELD_MISSING, "met.interp_weight", "grid_signature", index))
        prepared.append(item)
    return prepared


def _validate_child_station_ids(
    rows: Sequence[Mapping[str, Any]],
    station_ids: set[str],
    table: str,
    reasons: list[dict[str, Any]],
) -> None:
    for index, row in enumerate(rows):
        station_id = row.get("station_id")
        if station_id not in station_ids:
            reasons.append(_row_reason(REASON_APPLY_SHAPE_CONFLICT, table, "station_id", index))


def _verify_existing_station_rows(cursor: Any, stations: Sequence[Mapping[str, Any]]) -> None:
    station_ids = [station["station_id"] for station in stations]
    if not station_ids:
        return
    cursor.execute(
        """
        SELECT station_id,
               basin_version_id,
               station_name,
               ST_X(geom) AS longitude,
               ST_Y(geom) AS latitude,
               elevation_m,
               station_role,
               active_flag,
               properties_json
        FROM met.met_station
        WHERE station_id = ANY(%s)
        FOR UPDATE
        """,
        (station_ids,),
    )
    existing = {
        str(row["station_id"]): row
        for row in _fetchall_mappings(cursor, MET_STATION_SELECT_COLUMNS)
    }
    expected = {str(station["station_id"]): station for station in stations}
    for station_id, row in existing.items():
        station = expected.get(station_id)
        if station is None:
            continue
        if not _station_rows_compatible(row, station):
            raise ForcingDomainHandoffApplyError(
                _reason(REASON_APPLY_STATION_CONFLICT, table="met.met_station", station_id=station_id)
            )


def _upsert_forcing_version(cursor: Any, row: Mapping[str, Any], parser_envelope: Mapping[str, Any]) -> None:
    parser_evidence = parser_envelope.get("evidence") if isinstance(parser_envelope.get("evidence"), Mapping) else {}
    lineage_json = {
        "mode": APPLY_MODE,
        "parser_evidence": parser_evidence,
        "forcing_package_manifest_uri": row.get("forcing_package_manifest_uri"),
        "forcing_package_manifest_checksum_sha256": row.get("checksum"),
    }
    cursor.execute(
        """
        INSERT INTO met.forcing_version (
            forcing_version_id,
            model_id,
            source_id,
            cycle_time,
            start_time,
            end_time,
            station_count,
            forcing_package_uri,
            checksum,
            lineage_json
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (forcing_version_id) DO UPDATE SET
            start_time = EXCLUDED.start_time,
            end_time = EXCLUDED.end_time,
            station_count = EXCLUDED.station_count,
            forcing_package_uri = EXCLUDED.forcing_package_uri,
            checksum = EXCLUDED.checksum,
            lineage_json = EXCLUDED.lineage_json
        WHERE met.forcing_version.model_id = EXCLUDED.model_id
          AND met.forcing_version.source_id = EXCLUDED.source_id
          AND met.forcing_version.cycle_time IS NOT DISTINCT FROM EXCLUDED.cycle_time
          AND rtrim(met.forcing_version.forcing_package_uri, '/') = rtrim(EXCLUDED.forcing_package_uri, '/')
          AND (
              met.forcing_version.checksum IS NULL
              OR (
                  met.forcing_version.checksum = EXCLUDED.checksum
                  AND met.forcing_version.start_time = EXCLUDED.start_time
                  AND met.forcing_version.end_time = EXCLUDED.end_time
              )
          )
        RETURNING forcing_version_id
        """,
        (
            row["forcing_version_id"],
            row["model_id"],
            row["source_id"],
            row["cycle_time"],
            row["start_time"],
            row["end_time"],
            row["station_count"],
            row["forcing_package_uri"],
            row["checksum"],
            Json(redact_payload(lineage_json)),
        ),
    )
    if cursor.fetchone() is None:
        raise ForcingDomainHandoffApplyError(
            _reason(
                REASON_APPLY_FORCING_VERSION_CONFLICT,
                table="met.forcing_version",
                forcing_version_id=row["forcing_version_id"],
            )
        )


def _upsert_met_stations(cursor: Any, stations: Sequence[Mapping[str, Any]]) -> None:
    rows = [
        (
            station["station_id"],
            station["basin_version_id"],
            station["station_name"],
            station["longitude"],
            station["latitude"],
            station["elevation_m"],
            station["station_role"],
            station["active_flag"],
            Json(dict(station["properties_json"])),
        )
        for station in stations
    ]
    if not rows:
        return
    tolerance_sql = f"{STATION_COORDINATE_TOLERANCE:.12f}"
    returned = execute_values(
        cursor,
        f"""
        INSERT INTO met.met_station (
            station_id,
            basin_version_id,
            station_name,
            geom,
            elevation_m,
            station_role,
            active_flag,
            properties_json
        )
        VALUES %s
        ON CONFLICT (station_id) DO UPDATE SET
            station_id = met.met_station.station_id
        WHERE met.met_station.basin_version_id = EXCLUDED.basin_version_id
          AND met.met_station.station_name IS NOT DISTINCT FROM EXCLUDED.station_name
          AND ABS(ST_X(met.met_station.geom) - ST_X(EXCLUDED.geom)) <= {tolerance_sql}
          AND ABS(ST_Y(met.met_station.geom) - ST_Y(EXCLUDED.geom)) <= {tolerance_sql}
          AND met.met_station.elevation_m IS NOT DISTINCT FROM EXCLUDED.elevation_m
          AND met.met_station.station_role = EXCLUDED.station_role
          AND met.met_station.active_flag = EXCLUDED.active_flag
        RETURNING station_id
        """,
        rows,
        template=(
            "(%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4490), %s, %s, %s, %s)"
        ),
        page_size=5000,
        fetch=True,
    )
    if returned is not None and len(returned) != len(rows):
        raise ForcingDomainHandoffApplyError(
            _reason(
                REASON_APPLY_STATION_CONFLICT,
                table="met.met_station",
                expected=len(rows),
                actual=len(returned),
            )
        )


def _replace_forcing_station_timeseries(
    cursor: Any,
    forcing_version_id: str,
    rows: Sequence[Mapping[str, Any]],
) -> None:
    cursor.execute(
        "DELETE FROM met.forcing_station_timeseries WHERE forcing_version_id = %s",
        (forcing_version_id,),
    )
    tuples = [tuple(row[column] for column in FORCING_STATION_TIMESERIES_COLUMNS) for row in rows]
    if tuples:
        execute_values(
            cursor,
            """
            INSERT INTO met.forcing_station_timeseries (
                forcing_version_id,
                basin_version_id,
                station_id,
                valid_time,
                source_id,
                variable,
                value,
                unit,
                native_resolution,
                quality_flag
            )
            VALUES %s
            """,
            tuples,
            page_size=5000,
        )


def _replace_interp_weights(
    cursor: Any,
    scopes: Sequence[tuple[str, str, str]],
    rows: Sequence[Mapping[str, Any]],
) -> None:
    for source_id, grid_id, model_id in scopes:
        _lock_interp_weight_scope(cursor, source_id, grid_id, model_id)
        cursor.execute(
            """
            DELETE FROM met.interp_weight
            WHERE source_id = %s
              AND grid_id = %s
              AND model_id = %s
            """,
            (source_id, grid_id, model_id),
        )
    tuples = [tuple(row.get(column) for column in INTERP_WEIGHT_COLUMNS) for row in rows]
    if tuples:
        execute_values(
            cursor,
            """
            INSERT INTO met.interp_weight (
                source_id,
                grid_id,
                model_id,
                station_id,
                variable,
                grid_cell_id,
                weight,
                method,
                grid_signature
            )
            VALUES %s
            ON CONFLICT (source_id, grid_id, model_id, station_id, variable, grid_cell_id)
            DO UPDATE SET
                weight = EXCLUDED.weight,
                method = EXCLUDED.method,
                grid_signature = EXCLUDED.grid_signature
            """,
            tuples,
            page_size=5000,
        )


def _lock_interp_weight_scope(cursor: Any, source_id: str, grid_id: str, model_id: str) -> None:
    cursor.execute(
        """
        SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))
        """,
        (f"met.interp_weight:{source_id}\x1f{grid_id}\x1f{model_id}",),
    )


def _verify_apply_row_counts(cursor: Any, prepared: Mapping[str, Any]) -> dict[str, int]:
    forcing_version_id = prepared["forcing_version"]["forcing_version_id"]
    station_ids = [station["station_id"] for station in prepared["stations"]]
    scopes = prepared["interp_scopes"]

    row_counts = {
        "met.forcing_version": _select_count(
            cursor,
            "SELECT count(*) AS rows FROM met.forcing_version WHERE forcing_version_id = %s",
            (forcing_version_id,),
        ),
        "met.met_station": _select_count(
            cursor,
            "SELECT count(*) AS rows FROM met.met_station WHERE station_id = ANY(%s)",
            (station_ids,),
        ),
        "met.forcing_station_timeseries": _select_count(
            cursor,
            "SELECT count(*) AS rows FROM met.forcing_station_timeseries WHERE forcing_version_id = %s",
            (forcing_version_id,),
        ),
        "met.interp_weight": sum(_select_interp_weight_scope_count(cursor, scope) for scope in scopes),
    }
    expected = prepared["expected_row_counts"]
    if row_counts != expected:
        raise ForcingDomainHandoffApplyError(
            _reason(REASON_APPLY_SHAPE_CONFLICT, field="row_counts", expected=expected, actual=row_counts)
        )
    return row_counts


def _select_interp_weight_scope_count(cursor: Any, scope: tuple[str, str, str]) -> int:
    source_id, grid_id, model_id = scope
    return _select_count(
        cursor,
        """
        SELECT count(*) AS rows FROM met.interp_weight
        WHERE source_id = %s
          AND grid_id = %s
          AND model_id = %s
        """,
        (source_id, grid_id, model_id),
    )


def _select_count(cursor: Any, statement: str, parameters: tuple[Any, ...]) -> int:
    cursor.execute(statement, parameters)
    row = cursor.fetchone()
    value = _row_value(row, "rows", 0)
    return int(value or 0)


def _fetchall_mappings(cursor: Any, columns: Sequence[str]) -> list[dict[str, Any]]:
    return [_row_to_mapping(row, columns) for row in cursor.fetchall()]


def _row_to_mapping(row: Any, columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    return {column: row[index] for index, column in enumerate(columns)}


def _begin_apply_savepoint(cursor: Any) -> None:
    cursor.execute(f"SAVEPOINT {APPLY_SAVEPOINT_NAME}")


def _rollback_apply_savepoint(cursor: Any) -> None:
    cursor.execute(f"ROLLBACK TO SAVEPOINT {APPLY_SAVEPOINT_NAME}")
    cursor.execute(f"RELEASE SAVEPOINT {APPLY_SAVEPOINT_NAME}")


def _release_apply_savepoint(cursor: Any) -> None:
    cursor.execute(f"RELEASE SAVEPOINT {APPLY_SAVEPOINT_NAME}")


def _row_value(row: Any, key: str, index: int) -> Any:
    if isinstance(row, Mapping):
        return row.get(key)
    if isinstance(row, Sequence) and not isinstance(row, str | bytes | bytearray):
        return row[index]
    return None


def _station_rows_compatible(existing: Mapping[str, Any], station: Mapping[str, Any]) -> bool:
    return (
        existing.get("basin_version_id") == station.get("basin_version_id")
        and existing.get("station_name") == station.get("station_name")
        and _numbers_close(existing.get("longitude"), station.get("longitude"))
        and _numbers_close(existing.get("latitude"), station.get("latitude"))
        and _numbers_close(existing.get("elevation_m"), station.get("elevation_m"))
        and existing.get("station_role") == station.get("station_role")
        and existing.get("active_flag") == station.get("active_flag")
    )


def _station_coordinates(row: Mapping[str, Any]) -> tuple[float, float, str] | None:
    has_lon_lat = "longitude" in row or "latitude" in row
    lon_lat: tuple[float, float] | None = None
    if has_lon_lat:
        if not (_finite_number(row.get("longitude")) and _finite_number(row.get("latitude"))):
            return None
        lon_lat = (float(row["longitude"]), float(row["latitude"]))

    geometry = row.get("geometry")
    geometry_point = _geojson_point_coordinates(geometry) if geometry is not None else None
    if geometry is not None and geometry_point is None:
        return None
    if lon_lat is not None and geometry_point is not None:
        if not (
            _numbers_close(lon_lat[0], geometry_point[0])
            and _numbers_close(lon_lat[1], geometry_point[1])
        ):
            raise ForcingDomainHandoffApplyError(
                _reason(
                    REASON_APPLY_STATION_COORDINATE_MISMATCH,
                    table="met.met_station",
                    field="longitude/latitude|geometry",
                )
            )
        return lon_lat[0], lon_lat[1], "longitude_latitude+geometry"
    if lon_lat is not None:
        return lon_lat[0], lon_lat[1], "longitude_latitude"
    if geometry_point is not None:
        return geometry_point[0], geometry_point[1], "geometry"
    return None


def _geojson_point_coordinates(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, Mapping) or value.get("type") != "Point":
        return None
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) != 2:
        return None
    if not (_finite_number(coordinates[0]) and _finite_number(coordinates[1])):
        return None
    return float(coordinates[0]), float(coordinates[1])


def _identity_from_envelope(
    parser_envelope: Mapping[str, Any],
    *,
    forcing_version: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    evidence = parser_envelope.get("evidence")
    identity = dict(evidence.get("identity") or {}) if isinstance(evidence, Mapping) else {}
    if forcing_version:
        for field in (
            "forcing_version_id",
            "source_id",
            "cycle_time",
            "start_time",
            "end_time",
            "model_id",
            "basin_id",
            "basin_version_id",
        ):
            if field in forcing_version and forcing_version.get(field) not in (None, ""):
                identity[field] = forcing_version[field]
    return redact_payload(identity)


def _parser_unavailable_report(parser_envelope: Mapping[str, Any]) -> dict[str, Any]:
    reasons = parser_envelope.get("unavailable_reasons")
    return _unavailable_report(
        status="unavailable",
        reasons=list(reasons) if isinstance(reasons, list) else [],
        parser_envelope=parser_envelope,
        identity=_identity_from_envelope(parser_envelope),
        writes_performed=False,
    )


def _success_report(
    *,
    parser_envelope: Mapping[str, Any],
    identity: Mapping[str, Any],
    row_counts: Mapping[str, int],
    apply_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return redact_payload(
        {
            "mode": APPLY_MODE,
            "available": True,
            "status": "applied",
            "ready": True,
            "writes_performed": True,
            "identity": dict(identity),
            "row_counts": dict(row_counts),
            "parser_evidence": parser_envelope.get("evidence"),
            "apply_evidence": dict(apply_evidence),
            "unavailable_reasons": [],
        }
    )


def _unavailable_report(
    *,
    status: str,
    reasons: Sequence[Mapping[str, Any]],
    parser_envelope: Mapping[str, Any] | None = None,
    identity: Mapping[str, Any] | None = None,
    writes_performed: bool,
) -> dict[str, Any]:
    report = {
        "mode": APPLY_MODE,
        "available": False,
        "status": status,
        "ready": False,
        "writes_performed": writes_performed,
        "identity": dict(identity or {}),
        "row_counts": {},
        "parser_evidence": parser_envelope.get("evidence") if isinstance(parser_envelope, Mapping) else {},
        "apply_evidence": {},
        "unavailable_reasons": [dict(reason) for reason in reasons],
    }
    return redact_payload(report)


def _reason(code: str, **fields: Any) -> dict[str, Any]:
    reason = {"code": code}
    reason.update(fields)
    return redact_payload(reason)


def _row_reason(code: str, table: str, field: str, row_index: int) -> dict[str, Any]:
    return _reason(code, table=table, field=f"{table}.{field}", row_index=row_index)


def _normalize_source_field(
    row: dict[str, Any],
    *,
    table: str,
    field: str,
    reasons: list[dict[str, Any]],
    row_index: int | None = None,
) -> None:
    if not _present(row.get(field)):
        return
    try:
        row[field] = normalize_source_id(str(row[field]))
    except ValueError as error:
        reason_fields: dict[str, Any] = {
            "table": table,
            "field": f"{table}.{field}",
            "detail": redact_text(str(error)),
        }
        if row_index is not None:
            reason_fields["row_index"] = row_index
        reasons.append(_reason(REASON_APPLY_SHAPE_CONFLICT, **reason_fields))


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    return True


def _finite_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(float(value))


def _numbers_close(
    left: Any,
    right: Any,
    *,
    tolerance: float = STATION_COORDINATE_TOLERANCE,
) -> bool:
    if not (_finite_number(left) and _finite_number(right)):
        return False
    return abs(float(left) - float(right)) <= tolerance
