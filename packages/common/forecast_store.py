from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class ForecastStoreError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def default_database_url() -> str:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ForecastStoreError(
            status_code=500,
            code="DATABASE_URL_MISSING",
            message="DATABASE_URL is required for forecast API operations.",
        )
    return database_url


@dataclass(frozen=True)
class PsycopgForecastStore:
    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgForecastStore:
        return cls(default_database_url())

    def forecast_series(
        self,
        *,
        basin_version_id: str,
        segment_id: str,
        issue_time: str,
        variables: Sequence[str],
        scenarios: Sequence[str],
    ) -> dict[str, Any]:
        requested_variables = _normalized_tokens(variables)
        if "q_down" not in requested_variables:
            return _empty_forecast_response(segment_id=segment_id, issue_time=None)

        scenario_filter = _scenario_filter(scenarios)
        with self._transaction() as cursor:
            basin = self._fetch_optional(
                cursor,
                "SELECT basin_version_id FROM core.basin_version WHERE basin_version_id = %s",
                (basin_version_id,),
            )
            if basin is None:
                raise ForecastStoreError(
                    status_code=404,
                    code="SOURCE_NOT_FOUND",
                    message=f"Basin version not found: {basin_version_id}",
                    details={"basin_version_id": basin_version_id},
                )

            segment = self._fetch_optional(
                cursor,
                """
                SELECT
                    rs.river_segment_id,
                    rs.properties_json,
                    rnv.river_network_version_id
                FROM core.river_segment rs
                JOIN core.river_network_version rnv
                  ON rnv.river_network_version_id = rs.river_network_version_id
                WHERE rnv.basin_version_id = %s
                  AND rs.river_segment_id = %s
                ORDER BY rnv.created_at DESC, rnv.river_network_version_id DESC
                LIMIT 1
                """,
                (basin_version_id, segment_id),
            )
            if segment is None:
                raise ForecastStoreError(
                    status_code=404,
                    code="SEGMENT_NOT_FOUND",
                    message=f"River segment not found: {segment_id}",
                    details={"basin_version_id": basin_version_id, "segment_id": segment_id},
                )

            parsed_issue_time = None if issue_time == "latest" else _parse_datetime(issue_time)
            selected_issue_time = parsed_issue_time or self._latest_issue_time(
                cursor,
                basin_version_id=basin_version_id,
                segment_id=segment_id,
                scenario_filter=scenario_filter,
            )
            if selected_issue_time is None:
                return _empty_forecast_response(segment_id=segment_id, issue_time=None)

            rows = self._fetch_all(
                cursor,
                f"""
                SELECT
                    h.scenario_id,
                    h.cycle_time,
                    rt.valid_time,
                    rt.value,
                    rt.unit
                FROM hydro.river_timeseries rt
                JOIN hydro.hydro_run h ON h.run_id = rt.run_id
                WHERE rt.basin_version_id = %s
                  AND rt.river_segment_id = %s
                  AND rt.variable = 'q_down'
                  AND h.cycle_time = %s
                  {scenario_filter.sql}
                ORDER BY h.scenario_id, rt.valid_time
                """,
                (basin_version_id, segment_id, selected_issue_time, *scenario_filter.params),
            )

        if not rows and parsed_issue_time is not None:
            raise ForecastStoreError(
                status_code=404,
                code="RUN_NOT_PUBLISHED",
                message=f"No published forecast exists for issue_time {issue_time}.",
                details={
                    "basin_version_id": basin_version_id,
                    "segment_id": segment_id,
                    "issue_time": issue_time,
                },
            )

        return _forecast_response_from_rows(segment_id=segment_id, issue_time=selected_issue_time, rows=rows)

    def _latest_issue_time(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        scenario_filter: "_ScenarioFilter",
    ) -> datetime | None:
        row = self._fetch_optional(
            cursor,
            f"""
            SELECT h.cycle_time
            FROM hydro.river_timeseries rt
            JOIN hydro.hydro_run h ON h.run_id = rt.run_id
            WHERE rt.basin_version_id = %s
              AND rt.river_segment_id = %s
              AND rt.variable = 'q_down'
              AND h.cycle_time IS NOT NULL
              {scenario_filter.sql}
            ORDER BY h.cycle_time DESC
            LIMIT 1
            """,
            (basin_version_id, segment_id, *scenario_filter.params),
        )
        return _ensure_utc(row["cycle_time"]) if row is not None else None

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._transaction() as cursor:
            row = self._fetch_optional(
                cursor,
                """
                SELECT
                    h.*,
                    bv.basin_id,
                    COALESCE(ds.adapter_name, h.source_id) AS source
                FROM hydro.hydro_run h
                LEFT JOIN core.basin_version bv ON bv.basin_version_id = h.basin_version_id
                LEFT JOIN met.data_source ds ON ds.source_id = h.source_id
                WHERE h.run_id = %s
                """,
                (run_id,),
            )
        if row is None:
            raise ForecastStoreError(
                status_code=404,
                code="RUN_NOT_FOUND",
                message=f"Run not found: {run_id}",
                details={"run_id": run_id},
            )
        return _json_ready(row)

    def list_runs(
        self,
        *,
        basin_id: str | None,
        source: str | None,
        cycle_time: datetime | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if basin_id is not None:
            clauses.append("bv.basin_id = %s")
            params.append(basin_id)
        if source is not None:
            clauses.append("(LOWER(h.source_id) = LOWER(%s) OR LOWER(ds.adapter_name) = LOWER(%s))")
            params.extend([source, source])
        if cycle_time is not None:
            clauses.append("h.cycle_time = %s")
            params.append(_ensure_utc(cycle_time))
        if status is not None:
            clauses.append("h.status = %s")
            params.append(status)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._transaction() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total_count
                FROM hydro.hydro_run h
                LEFT JOIN core.basin_version bv ON bv.basin_version_id = h.basin_version_id
                LEFT JOIN met.data_source ds ON ds.source_id = h.source_id
                {where}
                """,
                tuple(params),
            )
            total_count = int(cursor.fetchone()["total_count"])
            rows = self._fetch_all(
                cursor,
                f"""
                SELECT
                    h.*,
                    bv.basin_id,
                    COALESCE(ds.adapter_name, h.source_id) AS source
                FROM hydro.hydro_run h
                LEFT JOIN core.basin_version bv ON bv.basin_version_id = h.basin_version_id
                LEFT JOIN met.data_source ds ON ds.source_id = h.source_id
                {where}
                ORDER BY h.cycle_time DESC NULLS LAST, h.created_at DESC, h.run_id
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
        return {
            "total_count": total_count,
            "items": [_json_ready(row) for row in rows],
            "limit": limit,
            "offset": offset,
        }

    def list_data_sources(self, *, limit: int, offset: int) -> dict[str, Any]:
        with self._transaction() as cursor:
            cursor.execute("SELECT COUNT(*) AS total_count FROM met.data_source")
            total_count = int(cursor.fetchone()["total_count"])
            rows = self._fetch_all(
                cursor,
                """
                SELECT *
                FROM met.data_source
                ORDER BY source_id
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
        return {
            "total_count": total_count,
            "items": [_data_source_response(row) for row in rows],
            "limit": limit,
            "offset": offset,
        }

    def list_cycles(
        self,
        *,
        source_id: str,
        from_time: datetime | None,
        to_time: datetime | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        clauses = ["source_id = %s"]
        params: list[Any] = [source_id]
        if from_time is not None:
            clauses.append("cycle_time >= %s")
            params.append(_ensure_utc(from_time))
        if to_time is not None:
            clauses.append("cycle_time <= %s")
            params.append(_ensure_utc(to_time))
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}"

        with self._transaction() as cursor:
            source = self._fetch_optional(
                cursor,
                "SELECT source_id FROM met.data_source WHERE source_id = %s",
                (source_id,),
            )
            if source is None:
                raise ForecastStoreError(
                    status_code=404,
                    code="SOURCE_NOT_FOUND",
                    message=f"Data source not found: {source_id}",
                    details={"source_id": source_id},
                )
            cursor.execute(f"SELECT COUNT(*) AS total_count FROM met.forecast_cycle {where}", tuple(params))
            total_count = int(cursor.fetchone()["total_count"])
            rows = self._fetch_all(
                cursor,
                f"""
                SELECT *
                FROM met.forecast_cycle
                {where}
                ORDER BY cycle_time DESC
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
        return {
            "total_count": total_count,
            "items": [_cycle_response(row) for row in rows],
            "limit": limit,
            "offset": offset,
        }

    def list_met_stations(
        self,
        *,
        basin_version_id: str | None,
        model_id: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        if basin_version_id is None and model_id is None:
            raise ForecastStoreError(
                status_code=422,
                code="MISSING_REQUIRED_FILTER",
                message="At least one of basin_version_id or model_id is required.",
                details={"required": ["basin_version_id", "model_id"]},
            )

        if model_id is not None:
            from_sql = """
                FROM met.met_station ms
                JOIN met.interp_weight iw ON iw.station_id = ms.station_id
            """
            clauses = ["iw.model_id = %s", "ms.active_flag = true"]
            params: list[Any] = [model_id]
            if basin_version_id is not None:
                clauses.append("ms.basin_version_id = %s")
                params.append(basin_version_id)
            distinct = "DISTINCT"
        else:
            from_sql = "FROM met.met_station ms"
            clauses = ["ms.basin_version_id = %s", "ms.active_flag = true"]
            params = [basin_version_id]
            distinct = ""

        where = f"WHERE {' AND '.join(clauses)}"
        with self._transaction() as cursor:
            cursor.execute(
                f"SELECT COUNT({distinct} ms.station_id) AS total_count {from_sql} {where}",
                tuple(params),
            )
            total_count = int(cursor.fetchone()["total_count"])
            rows = self._fetch_all(
                cursor,
                f"""
                SELECT {distinct}
                    ms.station_id,
                    ms.basin_version_id,
                    ms.station_name,
                    ST_X(ms.geom) AS longitude,
                    ST_Y(ms.geom) AS latitude,
                    ms.elevation_m,
                    ms.station_role,
                    ms.properties_json,
                    ms.created_at
                {from_sql}
                {where}
                ORDER BY ms.station_id
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
        return {
            "total_count": total_count,
            "items": [_station_response(row) for row in rows],
            "limit": limit,
            "offset": offset,
        }

    def _fetch_optional(self, cursor: Any, statement: str, parameters: Sequence[Any]) -> dict[str, Any] | None:
        rows = self._fetch_all(cursor, statement, parameters)
        return rows[0] if rows else None

    def _fetch_all(self, cursor: Any, statement: str, parameters: Sequence[Any]) -> list[dict[str, Any]]:
        cursor.execute(statement, tuple(parameters))
        return [dict(row) for row in cursor.fetchall()]

    def _transaction(self) -> Any:
        return _PsycopgTransaction(self.database_url)


@dataclass(frozen=True)
class _ScenarioFilter:
    sql: str
    params: tuple[Any, ...]


class _PsycopgTransaction:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.connection: Any | None = None
        self.psycopg2: Any | None = None

    def __enter__(self) -> Any:
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor, register_default_json, register_default_jsonb
        except ImportError as error:
            raise ForecastStoreError(
                status_code=500,
                code="PSYCOPG2_MISSING",
                message="psycopg2 is required for forecast API operations.",
            ) from error

        self.psycopg2 = psycopg2
        self.connection = psycopg2.connect(self.database_url)
        self.connection.autocommit = False
        register_default_json(conn_or_curs=self.connection)
        register_default_jsonb(conn_or_curs=self.connection)
        return self.connection.cursor(cursor_factory=RealDictCursor)

    def __exit__(self, exc_type: type[BaseException] | None, _exc: BaseException | None, _tb: Any) -> bool:
        if self.connection is None:
            return False
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
                if self.psycopg2 is not None and issubclass(exc_type, self.psycopg2.Error):
                    raise ForecastStoreError(
                        status_code=500,
                        code="DATABASE_ERROR",
                        message="Forecast API database operation failed.",
                    ) from _exc
        finally:
            self.connection.close()
        return False


def _scenario_filter(scenarios: Sequence[str]) -> _ScenarioFilter:
    tokens = _normalized_tokens(scenarios)
    if not tokens:
        return _ScenarioFilter("", ())

    source_ids = tuple(tokens)
    scenario_ids = set(tokens)
    for token in tokens:
        if not token.startswith("forecast_"):
            scenario_ids.add(f"forecast_{token}_deterministic")
    return _ScenarioFilter(
        "AND (LOWER(h.source_id) = ANY(%s) OR LOWER(h.scenario_id) = ANY(%s))",
        (list(source_ids), sorted(scenario_ids)),
    )


def _normalized_tokens(values: Sequence[str]) -> list[str]:
    tokens: list[str] = []
    for value in values:
        for token in str(value).split(","):
            normalized = token.strip().lower()
            if normalized:
                tokens.append(normalized)
    return tokens


def _parse_datetime(value: str) -> datetime:
    try:
        return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as error:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="issue_time must be 'latest' or an ISO 8601 timestamp.",
            details={"field": "issue_time", "rejected_value": value},
        ) from error


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _timestamp_ms(value: datetime) -> int:
    return int(_ensure_utc(value).timestamp() * 1000)


def _json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return _format_time(value)
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_ready(item) for item in value]
    return value


def _empty_forecast_response(*, segment_id: str, issue_time: datetime | None) -> dict[str, Any]:
    return {
        "segment_id": segment_id,
        "issue_time": _format_time(issue_time),
        "unit": "m3/s",
        "series": [],
        "frequency_thresholds": {},
    }


def _forecast_response_from_rows(
    *,
    segment_id: str,
    issue_time: datetime,
    rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    if not rows:
        return _empty_forecast_response(segment_id=segment_id, issue_time=issue_time)

    grouped: dict[str, dict[str, Any]] = {}
    unit = str(rows[0].get("unit") or "m3/s")
    for row in rows:
        scenario_id = str(row.get("scenario_id") or "forecast_gfs_deterministic")
        series = grouped.setdefault(
            scenario_id,
            {
                "scenario_id": scenario_id,
                "segment_role": "future_7_days",
                "points": [],
            },
        )
        series["points"].append([_timestamp_ms(row["valid_time"]), float(row["value"])])

    for series in grouped.values():
        series["points"].sort(key=lambda point: point[0])

    return {
        "segment_id": segment_id,
        "issue_time": _format_time(issue_time),
        "unit": unit,
        "series": list(grouped.values()),
        "frequency_thresholds": {},
    }


def _data_source_response(row: dict[str, Any]) -> dict[str, Any]:
    config = row.get("config_json") or {}
    source_id = str(row["source_id"])
    provider = config.get("provider")
    if provider is None and source_id.lower() == "gfs":
        provider = "NOAA/NCEP"
    return {
        **_json_ready(row),
        "provider": provider,
        "source": str(row.get("adapter_name") or source_id).replace("_adapter", ""),
        "format": row.get("native_format"),
        "description": config.get("description") or row.get("source_name"),
    }


def _cycle_response(row: dict[str, Any]) -> dict[str, Any]:
    manifest = str(row.get("manifest_uri") or "")
    return {
        **_json_ready(row),
        "file_count": 0 if not manifest else None,
        "quality_flag": "error" if row.get("error_code") else "ok",
    }


def _station_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **_json_ready(row),
        "name": row.get("station_name"),
        "longitude": float(row["longitude"]) if row.get("longitude") is not None else None,
        "latitude": float(row["latitude"]) if row.get("latitude") is not None else None,
        "elevation": float(row["elevation_m"]) if row.get("elevation_m") is not None else None,
    }
