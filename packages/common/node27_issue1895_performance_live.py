"""Live SQL/HTTP adapters and two-artifact publication for the product-curve oracle."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.node27_issue1895_catalog import observe_intersecting_groups
from packages.common.node27_issue1895_commit import (
    publish_performance_artifacts,
    publish_performance_receipt,
)
from packages.common.node27_issue1895_display_runtime import DISPLAY_ENV_PATH, read_display_port
from packages.common.node27_issue1895_dsn import assert_readonly_identity, read_display_env_text, resolve_readonly_dsn
from packages.common.node27_issue1895_http import (
    NoRedirect,
    close_response,
    default_opener,
    open_local_get,
    read_bounded_json_body,
)
from packages.common.node27_issue1895_identity import (
    IDENTITY_BODY_LIMIT,
    bind_hot_identities,
    exact_identity_params,
    identity_only_url,
    validate_identity_only_product,
)
from packages.common.node27_issue1895_lanes import (
    COLD_SEARCH_BOUND,
    EXACT_IDENTITY_SQL,
    HISTORICAL_IDENTITY_SQL,
    LANE_NAMES,
    SOURCES,
    WINDOW_ROW_PROOF_SQL,
    bind_lane,
    freeze_lanes,
    lane_kind,
    lane_source,
    prove_window_rows,
    select_cold_identity,
)
from packages.common.node27_issue1895_performance import (
    HTTP_BODY_LIMIT_BYTES,
    HTTP_TIMEOUT_SECONDS,
    assert_forecast_series_path,
)
from packages.common.node27_issue1895_query import (
    record_explicit_cycle_curve,
    validate_river_series_response,
)
from packages.common.node27_issue1895_types import Issue1895ReadinessError

APPLICATION_NAME = "nhms-issue1895-performance"
CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 5000
LOCK_TIMEOUT = "2s"
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
SEGMENT_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
BASIN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,96}$")
LOCAL_ORIGIN_RE = re.compile(r"^http://127\.0\.0\.1:(?:[1-9][0-9]{0,4})$")
MAX_CANDIDATES = 64


def validate_sha(value: str, *, label: str) -> str:
    text = str(value or "").strip()
    if HEAD_RE.fullmatch(text) is None:
        raise Issue1895ReadinessError(
            f"{label} is not a 40-character lowercase hex SHA",
            code="INPUT_SHA_INVALID",
            stage="performance",
        )
    return text


def validate_segment_id(value: str) -> str:
    text = str(value or "").strip()
    if SEGMENT_RE.fullmatch(text) is None:
        raise Issue1895ReadinessError(
            "segment id is not a bounded identifier",
            code="INPUT_SEGMENT_INVALID",
            stage="performance",
        )
    return text


def validate_basin_id(value: str) -> str:
    text = str(value or "").strip()
    if BASIN_RE.fullmatch(text) is None:
        raise Issue1895ReadinessError(
            "basin id is not a bounded identifier",
            code="INPUT_BASIN_INVALID",
            stage="performance",
        )
    return text


def validate_window_start(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise Issue1895ReadinessError(
            "window start is not a timezone-aware instant",
            code="INPUT_WINDOW_INVALID",
            stage="performance",
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Issue1895ReadinessError(
            "window start must be timezone-aware",
            code="INPUT_WINDOW_INVALID",
            stage="performance",
        )
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def local_display_origin(
    *,
    display_env_path: Path = DISPLAY_ENV_PATH,
    expected_display_env: Path = DISPLAY_ENV_PATH,
) -> str:
    """Read the C1-pinned display port; ambient process variables never decide it."""

    try:
        port, _provenance = read_display_port(display_env_path, expected_display_env=expected_display_env)
    except Issue1895ReadinessError as error:
        raise Issue1895ReadinessError(
            "display.env port provenance is invalid",
            code="INPUT_ORIGIN_INVALID",
            stage="performance",
        ) from error
    origin = f"http://127.0.0.1:{port}"
    if LOCAL_ORIGIN_RE.fullmatch(origin) is None:
        raise Issue1895ReadinessError(
            "display origin is not the local loopback contract",
            code="INPUT_ORIGIN_INVALID",
            stage="performance",
        )
    return origin


def resolve_live_dsn(
    *,
    display_env_path: Path = DISPLAY_ENV_PATH,
    environ: dict[str, str] | None = None,
) -> str:
    env = os.environ if environ is None else environ
    text = read_display_env_text(display_env_path)
    return resolve_readonly_dsn(display_env_text=text, environ=dict(env))


def _attributed_connect(dsn: str) -> Any:
    import psycopg2  # type: ignore[import-untyped]
    from psycopg2.extras import RealDictCursor  # type: ignore[import-untyped]

    return psycopg2.connect(
        dsn,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        fallback_application_name=APPLICATION_NAME,
        cursor_factory=RealDictCursor,
    )


def open_readonly_performance_connection(
    dsn: str,
    *,
    connect: Callable[[str], Any] | None = None,
) -> Any:
    opener = connect if connect is not None else _attributed_connect
    connection: Any | None = None
    try:
        connection = opener(dsn)
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
            cursor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    except Issue1895ReadinessError:
        if connection is not None:
            close_performance_connection(connection)
        raise
    except Exception:
        if connection is not None:
            close_performance_connection(connection)
        raise Issue1895ReadinessError(
            "readonly performance connection failed",
            code="SQL_CONNECT_FAILED",
            stage="performance",
        ) from None
    return connection


def close_performance_connection(connection: Any) -> None:
    try:
        connection.rollback()
    except Exception:
        pass
    try:
        connection.close()
    except Exception:
        pass


def _fetchone(cursor: Any) -> Any:
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, Mapping):
        return next(iter(row.values()))
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
        return row[0]
    return row


def _mapping_row(row: object) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return dict(row)
    raise Issue1895ReadinessError(
        "SQL row is not a mapping",
        code="SQL_ROW_INVALID",
        stage="performance",
    )


def _column_names(cursor: Any) -> tuple[str, ...]:
    description = getattr(cursor, "description", None)
    if description is None:
        raise Issue1895ReadinessError(
            "cursor description is missing",
            code="SQL_DESCRIPTION_MISSING",
            stage="performance",
        )
    names: list[str] = []
    seen: set[str] = set()
    for item in description:
        if isinstance(item, Mapping):
            raw = item.get("name") or item.get("column")
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            raw = item[0] if item else None
        else:
            raw = getattr(item, "name", None)
        if raw is not None and not isinstance(raw, str):
            raw = getattr(raw, "name", None)
        name = str(raw or "").strip()
        if not name:
            raise Issue1895ReadinessError(
                "cursor description is missing a column name",
                code="SQL_DESCRIPTION_MISSING",
                stage="performance",
            )
        if name in seen:
            raise Issue1895ReadinessError(
                "cursor description has duplicate column names",
                code="SQL_DESCRIPTION_DUPLICATE",
                stage="performance",
            )
        seen.add(name)
        names.append(name)
    if not names:
        raise Issue1895ReadinessError(
            "cursor description is empty",
            code="SQL_DESCRIPTION_MISSING",
            stage="performance",
        )
    return tuple(names)


def mapping_rows_from_cursor(cursor: Any, rows: Sequence[Any] | None) -> list[dict[str, Any]]:
    """Adapt default psycopg2 tuples into named mappings via cursor.description."""

    out: list[dict[str, Any]] = []
    names: tuple[str, ...] | None = None
    for row in rows or ():
        if isinstance(row, Mapping):
            out.append(dict(row))
            continue
        if names is None:
            names = _column_names(cursor)
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            raise Issue1895ReadinessError(
                "discovery row is malformed",
                code="SQL_ROW_INVALID",
                stage="performance",
            )
        if len(row) != len(names):
            raise Issue1895ReadinessError(
                "row width does not match cursor.description",
                code="SQL_ROW_WIDTH",
                stage="performance",
            )
        out.append({name: row[index] for index, name in enumerate(names)})
    return out


def prove_readonly_session(connection: Any) -> dict[str, Any]:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('transaction_read_only')")
            read_only = str(_fetchone(cursor) or "").strip().lower()
            cursor.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
            row = cursor.fetchone()
    except Issue1895ReadinessError:
        raise
    except Exception:
        raise Issue1895ReadinessError(
            "readonly session proof failed",
            code="SQL_READONLY_PROOF_FAILED",
            stage="performance",
        ) from None
    if read_only not in {"on", "true"}:
        raise Issue1895ReadinessError(
            "transaction_read_only is not on",
            code="SQL_NOT_READONLY",
            stage="performance",
        )
    if isinstance(row, Mapping):
        current_user = str(row.get("current_user") or row.get("current_user".upper()) or "")
        rolsuper = bool(row.get("rolsuper"))
    elif isinstance(row, Sequence) and not isinstance(row, (str, bytes)) and len(row) >= 2:
        current_user = str(row[0])
        rolsuper = bool(row[1])
    else:
        raise Issue1895ReadinessError(
            "readonly session identity is missing",
            code="SQL_READONLY_PROOF_FAILED",
            stage="performance",
        )
    assert_readonly_identity(current_user=current_user, rolsuper=rolsuper)
    return {"transaction_read_only": True, "current_user": current_user}


def _execute_rows(connection: Any, sql: str, params: object) -> list[dict[str, Any]]:
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            return mapping_rows_from_cursor(cursor, rows)
    except Issue1895ReadinessError:
        raise
    except Exception:
        raise Issue1895ReadinessError(
            "readonly discovery query failed",
            code="SQL_DISCOVERY_FAILED",
            stage="performance",
        ) from None


def _binder(connection: Any):
    def execute(sql: str, params: object = None) -> list[Mapping[str, Any]]:
        return _execute_rows(connection, sql, params)

    return execute


def _observe_classified(
    connection: Any,
    *,
    window_start: str,
    window_end: str,
    kind: str,
    load_chunk: Any = None,
    collect_group: Any = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    if load_chunk is not None:
        kwargs["load_chunk"] = load_chunk
    if collect_group is not None:
        kwargs["collect_group"] = collect_group
    classified = observe_intersecting_groups(
        _binder(connection),
        window_start=window_start,
        window_end=window_end,
        kind=kind,
        **kwargs,
    )
    if int(classified["candidate_count"]) > MAX_CANDIDATES:
        raise Issue1895ReadinessError(
            "candidate chunk set exceeds the bound",
            code="PLAN_CANDIDATES_BOUND",
            stage="plan",
        )
    return classified


def load_candidate_chunk_names(
    connection: Any,
    *,
    window_start: str,
    window_end: str,
    kind: str,
    load_chunk: Any = None,
    collect_group: Any = None,
) -> tuple[str, ...]:
    classified = _observe_classified(
        connection,
        window_start=window_start,
        window_end=window_end,
        kind=kind,
        load_chunk=load_chunk,
        collect_group=collect_group,
    )
    return tuple(classified["candidate_chunk_names"])


def execute_explain(connection: Any, *, sql: str, parameters: Sequence[Any]) -> Any:
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, tuple(parameters))
            row = cursor.fetchone()
    except Issue1895ReadinessError:
        raise
    except Exception:
        raise Issue1895ReadinessError(
            "EXPLAIN JSON query failed",
            code="SQL_EXPLAIN_FAILED",
            stage="performance",
        ) from None
    if row is None:
        raise Issue1895ReadinessError(
            "EXPLAIN returned no plan",
            code="SQL_EXPLAIN_EMPTY",
            stage="performance",
        )
    if isinstance(row, Mapping):
        payload = next(iter(row.values()))
    else:
        payload = row[0]
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise Issue1895ReadinessError(
                "EXPLAIN JSON is not parseable",
                code="PLAN_JSON_INVALID",
                stage="plan",
            ) from None
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            raise Issue1895ReadinessError(
                "EXPLAIN JSON is not parseable",
                code="PLAN_JSON_INVALID",
                stage="plan",
            ) from None
    return payload


def make_sql_probe(
    connection: Any,
    *,
    explain_sql: str,
    parameters: Sequence[Any],
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[int], dict[str, Any]]:
    def probe(_index: int) -> dict[str, Any]:
        started = clock()
        payload = execute_explain(connection, sql=explain_sql, parameters=parameters)
        duration_ms = (clock() - started) * 1000.0
        return {"duration_ms": duration_ms, "explain_json": payload}

    return probe


_NoRedirect = NoRedirect


def fetch_local_api(
    *,
    origin: str,
    path: str,
    query: str,
    segment_id: str,
    issue_time: str,
    scenario: str,
    source: str,
    window_start: str,
    window_end: str,
    opener: Any | None = None,
    clock: Callable[[], float] = time.monotonic,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
    body_limit: int = HTTP_BODY_LIMIT_BYTES,
) -> dict[str, Any]:
    if LOCAL_ORIGIN_RE.fullmatch(origin) is None:
        raise Issue1895ReadinessError(
            "API origin is not the local loopback contract",
            code="INPUT_ORIGIN_INVALID",
            stage="performance",
        )
    path = assert_forecast_series_path(path)
    started = clock()
    _request, response = open_local_get(
        url=f"{origin}{path}?{query}",
        opener=opener,
        timeout_seconds=timeout_seconds,
        stage="performance",
    )
    try:
        status, body, payload = read_bounded_json_body(
            response, body_limit=body_limit, stage="performance"
        )
        validated = validate_river_series_response(
            payload,
            segment_id=segment_id,
            issue_time=issue_time,
            scenario=scenario,
            source=source,
            window_start=window_start,
            window_end=window_end,
        )
        duration_ms = (clock() - started) * 1000.0
        return {
            "duration_ms": duration_ms,
            "status": status,
            "body_len": len(body),
            "point_count": validated["point_count"],
        }
    finally:
        close_response(response)


def make_api_probe(
    *,
    origin: str,
    path: str,
    query: str,
    segment_id: str,
    issue_time: str,
    scenario: str,
    source: str,
    window_start: str,
    window_end: str,
    opener: Any | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[int], dict[str, Any]]:
    def probe(_index: int) -> dict[str, Any]:
        return fetch_local_api(
            origin=origin,
            path=path,
            query=query,
            segment_id=segment_id,
            issue_time=issue_time,
            scenario=scenario,
            source=source,
            window_start=window_start,
            window_end=window_end,
            opener=opener,
            clock=clock,
        )

    return probe


def fetch_identity_only_product(
    *,
    origin: str,
    source: str,
    basin_id: str,
    opener: Any | None = None,
    timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
    body_limit: int = IDENTITY_BODY_LIMIT,
) -> dict[str, Any]:
    if LOCAL_ORIGIN_RE.fullmatch(origin) is None:
        raise Issue1895ReadinessError(
            "API origin is not the local loopback contract",
            code="INPUT_ORIGIN_INVALID",
            stage="identity",
        )
    _request, response = open_local_get(
        url=identity_only_url(origin=origin, source=source, basin_id=basin_id),
        opener=opener,
        timeout_seconds=timeout_seconds,
        stage="identity",
    )
    try:
        _status, _body, payload = read_bounded_json_body(
            response, body_limit=body_limit, stage="identity"
        )
        return validate_identity_only_product(payload, source=source, basin_id=basin_id)
    finally:
        close_response(response)


def _prove_rows(connection: Any, *, identity: Mapping[str, Any], query: Mapping[str, Any]) -> None:
    rows = _execute_rows(
        connection,
        WINDOW_ROW_PROOF_SQL,
        (
            identity["run_id"],
            identity["model_id"],
            query["timeseries_segment_id"],
            query["window_start"],
            query["window_end"],
        ),
    )
    if not rows:
        prove_window_rows(0)
        return
    raw = rows[0]
    count = raw.get("row_count", raw.get("value"))
    prove_window_rows(int(count) if not isinstance(count, bool) and isinstance(count, int) else 0)


def _bind_observed_lane(
    connection: Any,
    *,
    name: str,
    identity: Mapping[str, Any],
    segment_id: str,
    load_chunk: Any = None,
    collect_group: Any = None,
) -> dict[str, Any]:
    recorded = record_explicit_cycle_curve(
        basin_version_id=str(identity["basin_version_id"]),
        segment_id=segment_id,
        river_network_version_id=str(identity["river_network_version_id"]),
        issue_time=str(identity["cycle_time"]),
        run_id=str(identity["run_id"]),
        model_id=str(identity["model_id"]),
        source=lane_source(name),
    )
    classified = _observe_classified(
        connection,
        window_start=recorded["window_start"],
        window_end=recorded["window_end"],
        kind=lane_kind(name),
        load_chunk=load_chunk,
        collect_group=collect_group,
    )
    lane = bind_lane(name=name, identity=identity, segment_id=segment_id, classified=classified)
    _prove_rows(connection, identity=identity, query=lane["query"])
    return lane


def discover_four_lanes(
    connection: Any,
    *,
    basin_id: str,
    segment_id: str,
    origin: str,
    opener: Any | None = None,
    load_chunk: Any = None,
    collect_group: Any = None,
) -> dict[str, dict[str, Any]]:
    api_products: dict[str, Any] = {}
    db_rows: dict[str, list[Mapping[str, Any]]] = {}
    for source in SOURCES:
        api_identity = fetch_identity_only_product(
            origin=origin,
            source=source,
            basin_id=basin_id,
            opener=opener,
        )
        api_products[source] = {
            "status": "ok",
            "data": {**api_identity, "status": "ready", "availability": {"ready": True}},
        }
        rows = _execute_rows(connection, EXACT_IDENTITY_SQL, exact_identity_params(api_identity, source=source))
        db_rows[source] = rows
    hot_by_source = bind_hot_identities(api_products=api_products, db_rows=db_rows, basin_id=basin_id)
    lanes: dict[str, dict[str, Any]] = {}
    for source in SOURCES:
        identity = hot_by_source[source]
        name = f"{source.lower()}_hot"
        lanes[name] = _bind_observed_lane(
            connection,
            name=name,
            identity=identity,
            segment_id=segment_id,
            load_chunk=load_chunk,
            collect_group=collect_group,
        )
    for source in SOURCES:
        hot = hot_by_source[source]
        historical = _execute_rows(
            connection,
            HISTORICAL_IDENTITY_SQL,
            (basin_id, source, hot["run_id"], COLD_SEARCH_BOUND),
        )
        chosen: dict[str, Any] | None = None
        last_error: Issue1895ReadinessError | None = None
        for row in historical:
            try:
                identity = select_cold_identity([row], hot_run_id=hot["run_id"], source=source)
                name = f"{source.lower()}_cold"
                chosen = _bind_observed_lane(
                    connection,
                    name=name,
                    identity=identity,
                    segment_id=segment_id,
                    load_chunk=load_chunk,
                    collect_group=collect_group,
                )
                break
            except Issue1895ReadinessError as error:
                last_error = error
                continue
        if chosen is None:
            if last_error is not None:
                raise last_error
            raise Issue1895ReadinessError(
                "no display-ready historical cold identity remains",
                code="LANE_COLD_EMPTY",
                stage="lanes",
            )
        lanes[f"{source.lower()}_cold"] = chosen
    if set(lanes) != set(LANE_NAMES):
        raise Issue1895ReadinessError(
            "discovered lane set is not the four closed product lanes",
            code="LANE_SET_INVALID",
            stage="lanes",
        )
    return {name: lanes[name] for name in LANE_NAMES}


def observe_lane_states(
    connection: Any,
    lanes: Mapping[str, Mapping[str, Any]],
    *,
    load_chunk: Any = None,
    collect_group: Any = None,
) -> dict[str, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {}
    for name in LANE_NAMES:
        lane = lanes[name]
        classified = _observe_classified(
            connection,
            window_start=lane["query"]["window_start"],
            window_end=lane["query"]["window_end"],
            kind=lane_kind(name),
            load_chunk=load_chunk,
            collect_group=collect_group,
        )
        rebound = bind_lane(
            name=name,
            identity=lane["identity"],
            segment_id=lane["query"]["segment_id"],
            classified=classified,
        )
        observed[name] = rebound
    freeze_lanes(observed)
    return observed


__all__ = (
    "APPLICATION_NAME",
    "CONNECT_TIMEOUT_SECONDS",
    "LOCK_TIMEOUT",
    "STATEMENT_TIMEOUT_MS",
    "_NoRedirect",
    "close_performance_connection",
    "default_opener",
    "discover_four_lanes",
    "execute_explain",
    "fetch_identity_only_product",
    "fetch_local_api",
    "load_candidate_chunk_names",
    "local_display_origin",
    "make_api_probe",
    "make_sql_probe",
    "mapping_rows_from_cursor",
    "observe_lane_states",
    "open_readonly_performance_connection",
    "prove_readonly_session",
    "publish_performance_artifacts",
    "publish_performance_receipt",
    "resolve_live_dsn",
    "validate_basin_id",
    "validate_segment_id",
    "validate_sha",
    "validate_window_start",
)
