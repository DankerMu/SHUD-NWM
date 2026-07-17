#!/usr/bin/env python3
"""Capture one read-only production benchmark phase for issue #1069.

The helper derives both SQL statements from their production sources.  It has
no compression, decompression, retention, role, or service-management entry
point.  Each curve/MVT phase owns a fresh read-only database connection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from apps.api.routes.hydro_display import _postgis_tile_params
from packages.common.evidence_io import (
    BoundedEvidenceError,
    FileIdentity,
    assert_paths_disjoint,
    inspect_bounded_file_no_follow,
    read_bounded_bytes_no_follow,
    read_bounded_json_no_follow,
    reject_secret_material,
    validate_json_complexity,
)
from packages.common.forecast_store import (
    ForecastStoreError,
    PsycopgForecastStore,
)
from packages.common.node27_container_contract import CLIENT_BACKEND_TYPE
from packages.common.safe_fs import atomic_write_bytes_no_follow
from services.tiles.mvt import postgis_tile_sql

ROOT = Path(__file__).resolve().parents[1]
CURVE_SOURCE = ROOT / "packages/common/forecast_store.py"
MVT_SOURCE = ROOT / "services/tiles/mvt.py"
MVT_ROUTE_SOURCE = ROOT / "apps/api/routes/hydro_display.py"
EXPLAIN_PREFIX = "EXPLAIN (ANALYZE, BUFFERS, VERBOSE, FORMAT JSON) "
ACTIVITY_SQL = """
SELECT pid, backend_start, xact_start, query_start, state, wait_event_type,
       backend_type,
       usename,
       COALESCE(
         has_table_privilege(usename, 'hydro.river_timeseries',
                             'INSERT,UPDATE,DELETE'),
         false
       ) AS has_write_privilege_on_target,
       md5(regexp_replace(query, '\\s+', ' ', 'g')) AS query_signature
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
  AND state = 'active'
ORDER BY pid, backend_start
"""
STATEMENT_TIMEOUT_MS = 60_000
LOCK_TIMEOUT_MS = 5_000
PHASE_TIMEOUT_SECONDS = 900
MAX_SLICE_BYTES = 16 * 1024**2
MAX_RESULT_BYTES = 16 * 1024**2
MAX_RESULT_ROWS = 100_000
MAX_PLAN_BYTES = 8 * 1024**2
CONNECT_TIMEOUT_SECONDS = 10


class BenchmarkCaptureError(RuntimeError):
    """A fail-closed capture or publication error."""


class _Deadline:
    """One absolute monotonic wall covering connection through result capture."""

    def __init__(self, seconds: int = PHASE_TIMEOUT_SECONDS) -> None:
        self.started_at = datetime.now(UTC)
        self.expires_at = time.monotonic() + seconds

    def remaining(self, label: str) -> float:
        value = self.expires_at - time.monotonic()
        if value <= 0:
            raise BenchmarkCaptureError(f"benchmark wall deadline exceeded before {label}")
        return value

    def statement_timeout_ms(self, label: str) -> int:
        return max(1, min(STATEMENT_TIMEOUT_MS, int(self.remaining(label) * 1000)))


def _bounded_connection_cleanup(deadline: _Deadline, *connections: Any) -> None:
    """Bound driver rollback/close hooks without blocking the hard-wall owner."""

    errors: list[BaseException] = []

    def cleanup() -> None:
        try:
            for index, connection in enumerate(connections):
                if index == 0:
                    connection.rollback()
                connection.close()
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=cleanup, name="benchmark-connection-cleanup", daemon=True)
    worker.start()
    worker.join(deadline.remaining("connection cleanup"))
    if worker.is_alive():
        raise BenchmarkCaptureError("benchmark wall deadline exceeded during connection cleanup")
    if errors:
        raise BenchmarkCaptureError("benchmark connection cleanup failed") from errors[0]


class _RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, statement: str, parameters: Sequence[Any]) -> None:
        self.calls.append((statement, tuple(parameters)))

    def fetchall(self) -> list[dict[str, Any]]:
        return []


class _CaptureForecastStore(PsycopgForecastStore):
    """Recording adapter that exercises the public forecast-series owner."""

    def __init__(self, cursor: _RecordingCursor) -> None:
        super().__init__("recording-only")
        object.__setattr__(self, "_capture_cursor", cursor)

    @contextmanager
    def _transaction(self):  # type: ignore[no-untyped-def]
        yield self._capture_cursor

    def _validate_series_target(self, *args: Any, **kwargs: Any) -> None:
        # Target existence is a separate production query. The benchmark is
        # recording the public curve-owner's primary timeseries statement.
        return None


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BenchmarkCaptureError("timestamp must be ISO 8601") from error
    if parsed.tzinfo is None:
        raise BenchmarkCaptureError("timestamp must include an offset")
    return parsed.astimezone(UTC)


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise BenchmarkCaptureError("database result timestamp must include an offset")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        raise BenchmarkCaptureError("unexpected bytes in JSON curve result")
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise BenchmarkCaptureError(f"unsupported result type: {type(value).__name__}")


def _canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            _json_value(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _file_ref(path: Path) -> dict[str, Any]:
    if not path.is_absolute() or path not in {CURVE_SOURCE, MVT_SOURCE, MVT_ROUTE_SOURCE}:
        raise BenchmarkCaptureError("source path is not a canonical production owner")
    try:
        raw = read_bounded_bytes_no_follow(path, max_bytes=4 * 1024**2, label="production source")
    except BoundedEvidenceError as error:
        raise BenchmarkCaptureError(str(error)) from error
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _curve_query_and_binding(
    *,
    basin_version_id: str,
    river_segment_id: str,
    river_network_version_id: str,
    issue_time: datetime,
    end_time: datetime,
    scenario: str,
) -> tuple[str, list[str], tuple[Any, ...]]:
    if end_time != issue_time + timedelta(days=7):
        raise BenchmarkCaptureError("public curve owner supports the frozen seven-day window only")
    cursor = _RecordingCursor()
    try:
        _CaptureForecastStore(cursor).forecast_series(
            basin_version_id=basin_version_id,
            segment_id=river_segment_id,
            river_network_version_id=river_network_version_id,
            issue_time=issue_time.isoformat(),
            variables=["q_down"],
            scenarios=[scenario],
            include_analysis=False,
            run_types=["forecast"],
        )
    except ForecastStoreError as error:
        # The recording adapter intentionally returns no result rows. The
        # public owner raises after issuing its production query; only that
        # expected no-published-run outcome is admissible here.
        if error.code != "RUN_NOT_PUBLISHED":
            raise
    primary = [
        call
        for call in cursor.calls
        if "FROM hydro.river_timeseries rt" in call[0] and "h.run_type = 'forecast'" in call[0]
    ]
    if len(primary) != 1:
        raise BenchmarkCaptureError("production curve path did not yield exactly one primary SQL call")
    query_text, parameters = primary[0]
    names = [
        "basin_version_id",
        "river_segment_id",
        "river_network_version_id",
        "issue_time",
        "start_time",
        "end_time",
        "source_or_scenario_tokens",
        "scenario_tokens",
    ]
    if query_text.count("%s") != len(parameters) or len(names) != len(parameters):
        raise BenchmarkCaptureError("production curve SQL positional binding shape changed")
    return query_text, names, parameters


def _named_to_pyformat(statement: str) -> str:
    return str(text(statement).compile(dialect=postgresql.dialect(paramstyle="pyformat")))


def _row_mapping(cursor: Any, row: Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    description = getattr(cursor, "description", None)
    if description is None:
        raise BenchmarkCaptureError("cursor did not expose result column metadata")
    names = [str(item.name if hasattr(item, "name") else item[0]) for item in description]
    return dict(zip(names, row, strict=True))


def _fetch_all(
    cursor: Any,
    *,
    deadline: _Deadline,
    label: str,
    max_rows: int = MAX_RESULT_ROWS,
) -> list[dict[str, Any]]:
    """Fetch in bounded batches and enforce both row and serialized byte caps."""

    rows: list[dict[str, Any]] = []
    fetchmany = getattr(cursor, "fetchmany", None)
    while True:
        deadline.remaining(f"{label} fetch")
        batch = fetchmany(1000) if callable(fetchmany) else cursor.fetchall()
        if not batch:
            break
        rows.extend(_row_mapping(cursor, row) for row in batch)
        if len(rows) > max_rows:
            raise BenchmarkCaptureError(f"{label} exceeds the row ceiling")
        byte_values = [
            bytes(value).hex() if isinstance(value, (bytes, bytearray, memoryview)) else value
            for row in rows
            for value in row.values()
        ]
        if len(_canonical_json_bytes(byte_values)) > MAX_RESULT_BYTES:
            raise BenchmarkCaptureError(f"{label} exceeds the result byte ceiling")
        if not callable(fetchmany):
            break
    return rows


def _activity_snapshot(cursor: Any, *, deadline: _Deadline) -> tuple[list[dict[str, Any]], str]:
    timeout_ms = deadline.statement_timeout_ms("activity timeout setup")
    cursor.execute(f"SET statement_timeout = {timeout_ms}", ())
    cursor.execute(f"SET lock_timeout = {min(LOCK_TIMEOUT_MS, timeout_ms)}", ())
    deadline.remaining("activity query")
    cursor.execute(ACTIVITY_SQL, ())
    rows = _fetch_all(cursor, deadline=deadline, label="activity", max_rows=1000)
    sanitized: list[dict[str, Any]] = []
    for row in rows:
        sanitized.append(
            {
                "pid": row.get("pid"),
                "backend_start": _json_value(row.get("backend_start")),
                "xact_start": _json_value(row.get("xact_start")),
                "query_start": _json_value(row.get("query_start")),
                "state": row.get("state"),
                "wait_event_type": row.get("wait_event_type"),
                "backend_type": row.get("backend_type"),
                "usename": row.get("usename"),
                "has_write_privilege_on_target": bool(
                    row.get("has_write_privilege_on_target")
                ),
                "query_signature": row.get("query_signature"),
            }
        )
    return sanitized, datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _client_backend_sessions(sessions: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Project an activity snapshot onto the sessions the trust boundary targets.

    G14: a session is a trust-boundary threat only when it is both an external
    client backend AND holds INSERT/UPDATE/DELETE on our compression target.
    The display API's readonly role (``nhms_display_ro``) renders as a client
    backend too, but its ``has_write_privilege_on_target`` is false because
    the role has no write grants on the hypertable, so it cannot cause the
    session-identity drift this projection guards against.
    """

    return [
        session
        for session in sessions
        if session.get("backend_type") == CLIENT_BACKEND_TYPE
        and session.get("has_write_privilege_on_target") is True
    ]


def _set_statement_bounds(cursor: Any, *, deadline: _Deadline) -> None:
    timeout_ms = deadline.statement_timeout_ms("statement timeout setup")
    cursor.execute(f"SET LOCAL statement_timeout = {timeout_ms}", ())
    cursor.execute(f"SET LOCAL lock_timeout = {min(LOCK_TIMEOUT_MS, timeout_ms)}", ())


def _plan_payload(cursor: Any, statement: str, parameters: Any, *, deadline: _Deadline) -> Any:
    deadline.remaining("EXPLAIN")
    cursor.execute(EXPLAIN_PREFIX + statement, parameters)
    deadline.remaining("EXPLAIN result")
    row = cursor.fetchone()
    if row is None:
        raise BenchmarkCaptureError("EXPLAIN returned no plan")
    if isinstance(row, Mapping):
        payload = next(iter(row.values()))
    else:
        payload = row[0]
    if not isinstance(payload, (list, Mapping)) or not payload:
        raise BenchmarkCaptureError("EXPLAIN did not return full FORMAT JSON")
    normalized = _json_value(payload)
    try:
        validate_json_complexity(
            normalized,
            label="EXPLAIN plan",
            max_depth=48,
            max_nodes=100_000,
            max_array_items=10_000,
        )
    except BoundedEvidenceError as error:
        raise BenchmarkCaptureError(str(error)) from error
    if len(_canonical_json_bytes(normalized)) > MAX_PLAN_BYTES:
        raise BenchmarkCaptureError("EXPLAIN plan exceeds the byte ceiling")
    return normalized


def _walk_metric(value: Any, key: str) -> int:
    if isinstance(value, Mapping):
        own = value.get(key, 0)
        if not isinstance(own, (int, float)) or isinstance(own, bool) or own < 0:
            raise BenchmarkCaptureError(f"EXPLAIN {key} is invalid")
        return int(own) + sum(_walk_metric(child, key) for child in value.values())
    if isinstance(value, list):
        return sum(_walk_metric(child, key) for child in value)
    return 0


def _measurement(cursor: Any, statement: str, parameters: Any, *, deadline: _Deadline) -> dict[str, Any]:
    payload = _plan_payload(cursor, statement, parameters, deadline=deadline)
    root = payload[0] if isinstance(payload, list) else payload
    if not isinstance(root, Mapping):
        raise BenchmarkCaptureError("EXPLAIN JSON root is invalid")
    planning_ms = root.get("Planning Time")
    execution_ms = root.get("Execution Time")
    if not isinstance(planning_ms, (int, float)) or not isinstance(execution_ms, (int, float)):
        raise BenchmarkCaptureError("EXPLAIN omitted planning/execution timing")
    return {
        "plan": payload,
        "planning_ms": float(planning_ms),
        "execution_ms": float(execution_ms),
        "shared_hit_blocks": _walk_metric(payload, "Shared Hit Blocks"),
        "shared_read_blocks": _walk_metric(payload, "Shared Read Blocks"),
    }


def _capture_phase(
    connection: Any,
    *,
    monitor_connection: Any,
    statement: str,
    parameters: Any,
    result_kind: str,
    deadline: _Deadline | None = None,
) -> dict[str, Any]:
    deadline = deadline or _Deadline()
    try:
        phase_started_at = datetime.now(UTC)
        connection.set_session(isolation_level="REPEATABLE READ", readonly=True, autocommit=False)
        cursor = connection.cursor()
        monitor_connection.set_session(readonly=True, autocommit=True)
        monitor = monitor_connection.cursor()

        def bounded_measurement() -> dict[str, Any]:
            deadline.remaining("measurement")
            _set_statement_bounds(cursor, deadline=deadline)
            return _measurement(cursor, statement, parameters, deadline=deadline)

        activity_before, activity_before_at = _activity_snapshot(monitor, deadline=deadline)
        cold = bounded_measurement()
        activity_after_cold, activity_after_cold_at = _activity_snapshot(monitor, deadline=deadline)
        warmups = [bounded_measurement() for _ in range(2)]
        while warmups[-1]["shared_read_blocks"] > 0 and len(warmups) < 5:
            warmups.append(bounded_measurement())
        activity_before_measurements, activity_before_measurements_at = _activity_snapshot(monitor, deadline=deadline)
        measurements = []
        activity_mid: list[dict[str, Any]] | None = None
        activity_mid_at = ""
        for index in range(7):
            measurements.append(bounded_measurement())
            if index == 2:
                activity_mid, activity_mid_at = _activity_snapshot(monitor, deadline=deadline)
        _set_statement_bounds(cursor, deadline=deadline)
        deadline.remaining("result query")
        cursor.execute(statement, parameters)
        rows = _fetch_all(cursor, deadline=deadline, label=f"{result_kind} result")
        if result_kind == "curve":
            result_payload: Any = _json_value(rows)
            result_raw = _canonical_json_bytes(result_payload)
            result_rows = len(rows)
        elif result_kind == "mvt":
            if len(rows) != 1 or not isinstance(rows[0].get("tile"), (bytes, bytearray, memoryview)):
                raise BenchmarkCaptureError("production MVT query did not return one bytea tile")
            result_raw = bytes(rows[0]["tile"])
            if not result_raw:
                raise BenchmarkCaptureError("production MVT query returned an empty tile")
            result_payload = result_raw.hex()
            result_rows = 1
        else:
            raise BenchmarkCaptureError("unknown benchmark result kind")

        activity_after, activity_after_at = _activity_snapshot(monitor, deadline=deadline)
        signatures = [
            activity_before,
            activity_after_cold,
            activity_before_measurements,
            activity_mid or [],
            activity_after,
        ]
        # G9: the stability judgment considers external client backends only.
        # PostgreSQL-owned workers (autovacuum, TimescaleDB background workers)
        # deterministically start/stop around a real mutation and are not the
        # concurrent-writer identity this flag guards; the full session lists
        # (all backend types) stay persisted unchanged in the samples below.
        client_signatures = [_client_backend_sessions(value) for value in signatures]
        stable = all(value == client_signatures[0] for value in client_signatures[1:])
        activities = [
            {
                "captured_at": activity_before_at,
                "stage": "before_cold",
                "sessions": activity_before,
                "material_load_stable": stable,
            },
            {
                "captured_at": activity_after_cold_at,
                "stage": "after_cold",
                "sessions": activity_after_cold,
                "material_load_stable": stable,
            },
            {
                "captured_at": activity_before_measurements_at,
                "stage": "before_measurements",
                "sessions": activity_before_measurements,
                "material_load_stable": stable,
            },
            {
                "captured_at": activity_mid_at,
                "stage": "mid_measurements",
                "sessions": activity_mid or [],
                "material_load_stable": stable,
            },
            {
                "captured_at": activity_after_at,
                "stage": "after_result",
                "sessions": activity_after,
                "material_load_stable": stable,
            },
        ]
        phase_finished_at = datetime.now(UTC)
        return {
            "result_payload": result_payload,
            "result_sha256": hashlib.sha256(result_raw).hexdigest(),
            "rows": result_rows,
            "bytes": len(result_raw),
            "cache_class": "warm-cache" if warmups[-1]["shared_read_blocks"] == 0 else "mixed-cache",
            "cold": cold,
            "warmups": warmups,
            "measurements": measurements,
            "activity_samples": activities,
            "execution_bounds": {
                "statement_timeout_ms": STATEMENT_TIMEOUT_MS,
                "lock_timeout_ms": LOCK_TIMEOUT_MS,
                "phase_timeout_seconds": PHASE_TIMEOUT_SECONDS,
                "started_at": phase_started_at.isoformat().replace("+00:00", "Z"),
                "finished_at": phase_finished_at.isoformat().replace("+00:00", "Z"),
            },
        }
    finally:
        failure_in_flight = sys.exc_info()[0] is not None
        try:
            _bounded_connection_cleanup(deadline, connection, monitor_connection)
        except BenchmarkCaptureError:
            if not failure_in_flight:
                raise


def _default_connect(database_url: str, *, connect_timeout: int = CONNECT_TIMEOUT_SECONDS) -> Any:
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except ImportError as error:
        raise BenchmarkCaptureError("psycopg2 is required") from error
    return psycopg2.connect(
        database_url,
        connect_timeout=connect_timeout,
        cursor_factory=RealDictCursor,
    )


def _reject_secrets(document: Mapping[str, Any], database_url: str) -> None:
    rendered = _canonical_json_bytes(document).decode("utf-8")
    if database_url and database_url in rendered:
        raise BenchmarkCaptureError("refusing to publish potential credential material")
    try:
        reject_secret_material(document, label="benchmark evidence")
    except BoundedEvidenceError as error:
        raise BenchmarkCaptureError(str(error)) from error


def _capture_with_connections(
    *,
    connect: Callable[[str], Any],
    database_url: str,
    statement: str,
    parameters: Any,
    result_kind: str,
    deadline: _Deadline,
) -> dict[str, Any]:
    """Acquire both connections without leaking a partially acquired primary."""

    deadline.remaining(f"{result_kind} primary connection")
    remaining = max(1, math.ceil(deadline.remaining(f"{result_kind} primary connect bound")))
    if connect is _default_connect:
        primary = connect(database_url, connect_timeout=min(CONNECT_TIMEOUT_SECONDS, remaining))
    else:
        primary = connect(database_url)
    try:
        deadline.remaining(f"{result_kind} monitor connection")
        remaining = max(1, math.ceil(deadline.remaining(f"{result_kind} monitor connect bound")))
        if connect is _default_connect:
            monitor = connect(database_url, connect_timeout=min(CONNECT_TIMEOUT_SECONDS, remaining))
        else:
            monitor = connect(database_url)
    except Exception:
        _bounded_connection_cleanup(deadline, primary)
        raise
    return _capture_phase(
        primary,
        monitor_connection=monitor,
        statement=statement,
        parameters=parameters,
        result_kind=result_kind,
        deadline=deadline,
    )


def capture_benchmark_phase(
    *,
    database_url: str,
    phase: str,
    curve_basin_version_id: str,
    curve_river_segment_id: str,
    curve_river_network_version_id: str,
    curve_issue_time: datetime,
    curve_end_time: datetime,
    curve_scenario: str,
    mvt_run_id: str,
    mvt_basin_version_id: str,
    mvt_river_network_version_id: str,
    mvt_valid_time: datetime,
    mvt_z: int,
    mvt_x: int,
    mvt_y: int,
    connect: Callable[[str], Any] = _default_connect,
) -> dict[str, Any]:
    deadline = _Deadline()
    capture_started_at = deadline.started_at
    deadline.remaining("capture input validation")
    if phase not in {"before", "after"}:
        raise BenchmarkCaptureError("phase must be before or after")
    curve_query, parameter_names, curve_parameters = _curve_query_and_binding(
        basin_version_id=curve_basin_version_id,
        river_segment_id=curve_river_segment_id,
        river_network_version_id=curve_river_network_version_id,
        issue_time=curve_issue_time,
        end_time=curve_end_time,
        scenario=curve_scenario,
    )
    mvt_query = postgis_tile_sql("hydro")
    mvt_binding = _postgis_tile_params(
        {
            "run_id": mvt_run_id,
            "basin_version_id": mvt_basin_version_id,
            "river_network_version_id": mvt_river_network_version_id,
            "variable": "q_down",
            "valid_time": mvt_valid_time,
        },
        z=mvt_z,
        x=mvt_x,
        y=mvt_y,
    )
    document = {
        "execution_bounds": {
            "started_at": capture_started_at.isoformat().replace("+00:00", "Z"),
            "finished_at": None,
            "wall_seconds": PHASE_TIMEOUT_SECONDS,
        },
        "queries": [
            {
                "name": "curve",
                "request": {
                    "basin_version_id": curve_basin_version_id,
                    "river_segment_id": curve_river_segment_id,
                    "river_network_version_id": curve_river_network_version_id,
                    "issue_time": _json_value(curve_issue_time),
                    "end_time": _json_value(curve_end_time),
                    "scenario": curve_scenario,
                },
                "source_refs": [_file_ref(CURVE_SOURCE)],
                "query_sha256": hashlib.sha256(curve_query.encode()).hexdigest(),
                "query_text": curve_query,
                "binding": {
                    "parameter_names": parameter_names,
                    "bound_parameters": _json_value(curve_parameters),
                },
                phase: _capture_with_connections(
                    connect=connect,
                    database_url=database_url,
                    statement=curve_query,
                    parameters=curve_parameters,
                    result_kind="curve",
                    deadline=deadline,
                ),
            },
            {
                "name": "mvt",
                "request": {
                    "run_id": mvt_run_id,
                    "basin_version_id": mvt_basin_version_id,
                    "river_network_version_id": mvt_river_network_version_id,
                    "valid_time": _json_value(mvt_valid_time),
                    "z": mvt_z,
                    "x": mvt_x,
                    "y": mvt_y,
                },
                "source_refs": [_file_ref(MVT_SOURCE), _file_ref(MVT_ROUTE_SOURCE)],
                "query_sha256": hashlib.sha256(mvt_query.encode()).hexdigest(),
                "query_text": mvt_query,
                "binding": _json_value(mvt_binding),
                phase: _capture_with_connections(
                    connect=connect,
                    database_url=database_url,
                    statement=_named_to_pyformat(mvt_query),
                    parameters=mvt_binding,
                    result_kind="mvt",
                    deadline=deadline,
                ),
            },
        ],
    }
    deadline.remaining("capture finalization")
    document["execution_bounds"]["finished_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _reject_secrets(document, database_url)
    return document


def merge_benchmark_slices(before_document: Mapping[str, Any], after_document: Mapping[str, Any]) -> dict[str, Any]:
    """Merge two immutable phase slices without weakening production identity."""
    before_queries = before_document.get("queries")
    after_queries = after_document.get("queries")
    if not isinstance(before_queries, list) or not isinstance(after_queries, list):
        raise BenchmarkCaptureError("benchmark slices must contain query arrays")
    if len(before_queries) != 2 or len(after_queries) != 2:
        raise BenchmarkCaptureError("benchmark slices must contain exactly curve then mvt")
    merged: list[dict[str, Any]] = []
    static_keys = {
        "name",
        "request",
        "source_refs",
        "query_sha256",
        "query_text",
        "binding",
    }
    for index, (before_value, after_value) in enumerate(zip(before_queries, after_queries, strict=True)):
        if not isinstance(before_value, Mapping) or not isinstance(after_value, Mapping):
            raise BenchmarkCaptureError("benchmark query slice must be an object")
        if set(before_value) != static_keys | {"before"}:
            raise BenchmarkCaptureError("before benchmark slice keys differ")
        if set(after_value) != static_keys | {"after"}:
            raise BenchmarkCaptureError("after benchmark slice keys differ")
        if any(before_value[key] != after_value[key] for key in static_keys):
            raise BenchmarkCaptureError(f"benchmark query identity drift at index {index}")
        merged.append({**dict(before_value), "after": after_value["after"]})
    if [query["name"] for query in merged] != ["curve", "mvt"]:
        raise BenchmarkCaptureError("benchmark query order differs")
    return {
        "execution_bounds": {
            "before": before_document.get("execution_bounds"),
            "after": after_document.get("execution_bounds"),
        },
        "queries": merged,
    }


def _read_json_file(path: Path, label: str) -> Mapping[str, Any]:
    try:
        _, value = read_bounded_json_no_follow(
            path,
            max_bytes=MAX_SLICE_BYTES,
            label=label,
            max_depth=48,
            max_nodes=250_000,
            max_array_items=25_000,
        )
    except BoundedEvidenceError as error:
        raise BenchmarkCaptureError(str(error)) from error
    if not isinstance(value, Mapping):
        raise BenchmarkCaptureError(f"{label} must be a JSON object")
    return value


def _referenced_paths(value: Any) -> list[Path]:
    paths: list[Path] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            if set(current) == {"path", "sha256", "bytes"}:
                path = Path(str(current["path"]))
                if path.is_absolute():
                    paths.append(path)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return paths


def _snapshot_inputs(paths: list[Path]) -> list[FileIdentity]:
    snapshots: list[FileIdentity] = []
    for index, path in enumerate(paths):
        snapshots.append(
            inspect_bounded_file_no_follow(
                path,
                max_bytes=MAX_SLICE_BYTES,
                label=f"benchmark retained input[{index}]",
            )
        )
    return snapshots


def _reverify_inputs(snapshots: list[FileIdentity]) -> None:
    for index, snapshot in enumerate(snapshots):
        current = inspect_bounded_file_no_follow(
            snapshot.path,
            max_bytes=MAX_SLICE_BYTES,
            label=f"benchmark retained input[{index}]",
        )
        if current != snapshot:
            raise BenchmarkCaptureError("benchmark retained input changed during publication")


def _publish_failure_tombstone(output: Path, *, phase: str, stage: str) -> None:
    payload = {
        "schema_version": "2.0",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "phase": phase,
        "outcome": "failed",
        "provenance_state": "unavailable",
        "failure": {"stage": stage, "mutation_state": "failed_before_mutation"},
    }
    try:
        atomic_write_bytes_no_follow(
            output,
            _canonical_json_bytes(payload),
            mode=stat.S_IRUSR | stat.S_IWUSR,
            require_durable_replace=True,
        )
    except Exception:
        return


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("before", "after"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--before-path", type=Path)
    parser.add_argument("--curve-basin-version-id", required=True)
    parser.add_argument("--curve-river-segment-id", required=True)
    parser.add_argument("--curve-river-network-version-id", required=True)
    parser.add_argument("--curve-issue-time", required=True)
    parser.add_argument("--curve-end-time")
    parser.add_argument("--curve-scenario", required=True)
    parser.add_argument("--mvt-run-id", required=True)
    parser.add_argument("--mvt-basin-version-id", required=True)
    parser.add_argument("--mvt-river-network-version-id", required=True)
    parser.add_argument("--mvt-valid-time", required=True)
    parser.add_argument("--mvt-z", required=True, type=int)
    parser.add_argument("--mvt-x", required=True, type=int)
    parser.add_argument("--mvt-y", required=True, type=int)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output_safe = False
    retained: list[FileIdentity] = []
    try:
        database_url = os.getenv("DATABASE_URL", "")
        if not database_url:
            raise BenchmarkCaptureError("DATABASE_URL is required")
        if not args.output.is_absolute():
            raise BenchmarkCaptureError("output path must be absolute")
        if args.phase == "before" and args.before_path is not None:
            raise BenchmarkCaptureError("before capture cannot merge a prior slice")
        if args.phase == "after" and (args.before_path is None or not args.before_path.is_absolute()):
            raise BenchmarkCaptureError("after capture requires an absolute --before-path")
        before_document: Mapping[str, Any] | None = None
        input_paths = [CURVE_SOURCE, MVT_SOURCE, MVT_ROUTE_SOURCE]
        if args.before_path is not None:
            before_document = _read_json_file(args.before_path, "before benchmark slice")
            input_paths.extend([args.before_path, *_referenced_paths(before_document)])
        try:
            assert_paths_disjoint(args.output, input_paths, label="benchmark")
        except BoundedEvidenceError as error:
            raise BenchmarkCaptureError(str(error)) from error
        output_safe = True
        retained = _snapshot_inputs(input_paths)
        issue_time = _utc(args.curve_issue_time)
        end_time = _utc(args.curve_end_time) if args.curve_end_time else issue_time + timedelta(days=7)
        document = capture_benchmark_phase(
            database_url=database_url,
            phase=args.phase,
            curve_basin_version_id=args.curve_basin_version_id,
            curve_river_segment_id=args.curve_river_segment_id,
            curve_river_network_version_id=args.curve_river_network_version_id,
            curve_issue_time=issue_time,
            curve_end_time=end_time,
            curve_scenario=args.curve_scenario,
            mvt_run_id=args.mvt_run_id,
            mvt_basin_version_id=args.mvt_basin_version_id,
            mvt_river_network_version_id=args.mvt_river_network_version_id,
            mvt_valid_time=_utc(args.mvt_valid_time),
            mvt_z=args.mvt_z,
            mvt_x=args.mvt_x,
            mvt_y=args.mvt_y,
        )
        if args.phase == "after":
            if before_document is None:
                raise BenchmarkCaptureError("before benchmark slice was not retained")
            document = merge_benchmark_slices(before_document, document)
        atomic_write_bytes_no_follow(
            args.output,
            _canonical_json_bytes(document),
            mode=stat.S_IRUSR | stat.S_IWUSR,
            require_durable_replace=True,
        )
        _reverify_inputs(retained)
    except Exception as error:
        if output_safe:
            _publish_failure_tombstone(
                args.output,
                phase=args.phase,
                stage="capture_or_publish",
            )
        print(
            json.dumps(
                {"outcome": "refused", "error": type(error).__name__},
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
