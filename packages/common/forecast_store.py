from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
        include_analysis: bool = False,
        run_types: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        requested_variables = _normalized_tokens(variables)
        if "q_down" not in requested_variables:
            if include_analysis:
                return _empty_spliced_response(
                    river_segment_id=segment_id,
                    issue_time=None,
                    variable=_response_variable_name(requested_variables),
                )
            return _empty_forecast_response(segment_id=segment_id, issue_time=None)

        run_type_tokens = _run_type_tokens(run_types)
        scenario_filter = _scenario_filter(scenarios)
        with self._transaction() as cursor:
            self._validate_series_target(cursor, basin_version_id=basin_version_id, segment_id=segment_id)

            if "hindcast" in run_type_tokens and not include_analysis:
                parsed_issue_time = None if issue_time == "latest" else _parse_datetime(issue_time)
                selected_issue_time = parsed_issue_time or self._latest_run_type_valid_time(
                    cursor,
                    basin_version_id=basin_version_id,
                    segment_id=segment_id,
                    run_types=run_type_tokens,
                )
                if selected_issue_time is None:
                    return _empty_forecast_response(segment_id=segment_id, issue_time=None)
                rows = self._fetch_run_type_segment_rows(
                    cursor,
                    basin_version_id=basin_version_id,
                    segment_id=segment_id,
                    run_types=run_type_tokens,
                    end_time=selected_issue_time,
                )
                return _forecast_response_from_rows(segment_id=segment_id, issue_time=selected_issue_time, rows=rows)

            parsed_issue_time = None if issue_time == "latest" else _parse_datetime(issue_time)
            latest_cycles_by_scenario: dict[str, datetime] = {}
            if parsed_issue_time is None:
                latest_cycles_by_scenario = self._per_source_latest_cycles(
                    cursor,
                    basin_version_id=basin_version_id,
                    segment_id=segment_id,
                    scenario_filter=scenario_filter,
                )
            selected_issue_time = parsed_issue_time or _latest_cycle_time(latest_cycles_by_scenario)
            if include_analysis and selected_issue_time is None:
                selected_issue_time = self._latest_analysis_issue_time(
                    cursor,
                    basin_version_id=basin_version_id,
                    segment_id=segment_id,
                )
            if selected_issue_time is None:
                if include_analysis:
                    return _empty_spliced_response(
                        river_segment_id=segment_id,
                        issue_time=None,
                        variable=_response_variable_name(requested_variables),
                    )
                return _empty_forecast_response(segment_id=segment_id, issue_time=None)

            if include_analysis:
                analysis_start, analysis_end = analysis_window_for_issue_time(selected_issue_time)
                forecast_end = selected_issue_time + timedelta(days=7)
                analysis_rows = self._fetch_analysis_segment_rows(
                    cursor,
                    basin_version_id=basin_version_id,
                    segment_id=segment_id,
                    start_time=analysis_start,
                    end_time=analysis_end,
                )
                forecast_rows = self._fetch_forecast_segment_rows(
                    cursor,
                    basin_version_id=basin_version_id,
                    segment_id=segment_id,
                    issue_time=selected_issue_time,
                    scenario_filter=scenario_filter,
                    cycle_times_by_scenario=None if parsed_issue_time is not None else latest_cycles_by_scenario,
                    end_time=forecast_end,
                )
                return _spliced_response_from_rows(
                    river_segment_id=segment_id,
                    issue_time=selected_issue_time,
                    variable=_response_variable_name(requested_variables),
                    analysis_rows=analysis_rows,
                    forecast_rows=forecast_rows,
                )

            rows = self._fetch_forecast_segment_rows(
                cursor,
                basin_version_id=basin_version_id,
                segment_id=segment_id,
                issue_time=selected_issue_time,
                scenario_filter=scenario_filter,
                cycle_times_by_scenario=None if parsed_issue_time is not None else latest_cycles_by_scenario,
                end_time=selected_issue_time + timedelta(days=7),
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

    def _validate_series_target(self, cursor: Any, *, basin_version_id: str, segment_id: str) -> None:
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

    def _per_source_latest_cycles(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        scenario_filter: "_ScenarioFilter",
    ) -> dict[str, datetime]:
        rows = self._fetch_all(
            cursor,
            f"""
            SELECT
                h.scenario_id,
                MAX(h.cycle_time) AS cycle_time
            FROM hydro.river_timeseries rt
            JOIN hydro.hydro_run h ON h.run_id = rt.run_id
            WHERE rt.basin_version_id = %s
              AND rt.river_segment_id = %s
              AND rt.variable = 'q_down'
              AND h.run_type = 'forecast'
              AND h.cycle_time IS NOT NULL
              {scenario_filter.sql}
            GROUP BY h.scenario_id
            ORDER BY h.scenario_id
            """,
            (basin_version_id, segment_id, *scenario_filter.params),
        )
        return {
            str(row["scenario_id"]): _ensure_utc(row["cycle_time"])
            for row in rows
            if row.get("scenario_id") and row.get("cycle_time") is not None
        }

    def _latest_analysis_issue_time(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
    ) -> datetime | None:
        row = self._fetch_optional(
            cursor,
            """
            SELECT h.end_time
            FROM hydro.river_timeseries rt
            JOIN hydro.hydro_run h ON h.run_id = rt.run_id
            WHERE rt.basin_version_id = %s
              AND rt.river_segment_id = %s
              AND rt.variable = 'q_down'
              AND h.scenario_id = 'analysis_true_field'
              AND h.end_time IS NOT NULL
            ORDER BY h.end_time DESC, h.created_at DESC
            LIMIT 1
            """,
            (basin_version_id, segment_id),
        )
        return _ensure_utc(row["end_time"]) if row is not None else None

    def _fetch_analysis_segment_rows(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        return self._fetch_all(
            cursor,
            """
            SELECT DISTINCT ON (rt.valid_time)
                h.scenario_id,
                h.source_id,
                fv.lineage_json,
                rt.valid_time,
                rt.value,
                rt.unit
            FROM hydro.river_timeseries rt
            JOIN hydro.hydro_run h ON h.run_id = rt.run_id
            LEFT JOIN met.forcing_version fv ON fv.forcing_version_id = h.forcing_version_id
            WHERE rt.basin_version_id = %s
              AND rt.river_segment_id = %s
              AND rt.variable = 'q_down'
              AND h.scenario_id = 'analysis_true_field'
              AND rt.valid_time >= %s
              AND rt.valid_time < %s
            ORDER BY rt.valid_time, h.end_time DESC, h.created_at DESC
            """,
            (basin_version_id, segment_id, start_time, end_time),
        )

    def _fetch_forecast_segment_rows(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        issue_time: datetime,
        scenario_filter: "_ScenarioFilter",
        cycle_times_by_scenario: Mapping[str, datetime] | None = None,
        end_time: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if cycle_times_by_scenario is not None:
            if not cycle_times_by_scenario:
                return []
            selected_cycle_values = ", ".join(["(%s, %s::timestamptz)"] * len(cycle_times_by_scenario))
            selected_cycle_params: list[Any] = []
            for scenario_id, cycle_time in cycle_times_by_scenario.items():
                selected_cycle_params.extend([scenario_id, _ensure_utc(cycle_time)])
            return self._fetch_all(
                cursor,
                f"""
                WITH selected_cycles(scenario_id, cycle_time) AS (
                    VALUES {selected_cycle_values}
                )
                SELECT
                    h.scenario_id,
                    h.source_id,
                    h.cycle_time,
                    h.end_time AS run_end_time,
                    fv.lineage_json,
                    rt.valid_time,
                    rt.value,
                    rt.unit
                FROM hydro.river_timeseries rt
                JOIN hydro.hydro_run h ON h.run_id = rt.run_id
                JOIN selected_cycles sc
                  ON sc.scenario_id = h.scenario_id
                 AND sc.cycle_time = h.cycle_time
                LEFT JOIN met.forcing_version fv ON fv.forcing_version_id = h.forcing_version_id
                WHERE rt.basin_version_id = %s
                  AND rt.river_segment_id = %s
                  AND rt.variable = 'q_down'
                  AND h.run_type = 'forecast'
                  AND rt.valid_time >= h.cycle_time
                  AND rt.valid_time <= h.cycle_time + INTERVAL '7 days'
                  {scenario_filter.sql}
                ORDER BY h.scenario_id, rt.valid_time
                """,
                (*selected_cycle_params, basin_version_id, segment_id, *scenario_filter.params),
            )

        forecast_end = end_time or issue_time + timedelta(days=7)
        return self._fetch_all(
            cursor,
            f"""
            SELECT
                h.scenario_id,
                h.source_id,
                h.cycle_time,
                h.end_time AS run_end_time,
                fv.lineage_json,
                rt.valid_time,
                rt.value,
                rt.unit
            FROM hydro.river_timeseries rt
            JOIN hydro.hydro_run h ON h.run_id = rt.run_id
            LEFT JOIN met.forcing_version fv ON fv.forcing_version_id = h.forcing_version_id
            WHERE rt.basin_version_id = %s
              AND rt.river_segment_id = %s
              AND rt.variable = 'q_down'
              AND h.run_type = 'forecast'
              AND h.cycle_time = %s
              AND rt.valid_time >= %s
              AND rt.valid_time <= %s
              {scenario_filter.sql}
            ORDER BY h.scenario_id, rt.valid_time
            """,
            (basin_version_id, segment_id, issue_time, issue_time, forecast_end, *scenario_filter.params),
        )

    def _latest_run_type_valid_time(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        run_types: Sequence[str],
    ) -> datetime | None:
        row = self._fetch_optional(
            cursor,
            """
            SELECT MAX(rt.valid_time) AS valid_time
            FROM hydro.river_timeseries rt
            JOIN hydro.hydro_run h ON h.run_id = rt.run_id
            WHERE rt.basin_version_id = %s
              AND rt.river_segment_id = %s
              AND rt.variable = 'q_down'
              AND LOWER(h.run_type) = ANY(%s)
            """,
            (basin_version_id, segment_id, list(run_types)),
        )
        return _ensure_utc(row["valid_time"]) if row is not None and row.get("valid_time") is not None else None

    def _fetch_run_type_segment_rows(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        run_types: Sequence[str],
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        start_time = end_time - timedelta(days=7)
        return self._fetch_all(
            cursor,
            """
            SELECT
                h.scenario_id,
                h.source_id,
                h.cycle_time,
                h.end_time AS run_end_time,
                fv.lineage_json,
                rt.valid_time,
                rt.value,
                rt.unit
            FROM hydro.river_timeseries rt
            JOIN hydro.hydro_run h ON h.run_id = rt.run_id
            LEFT JOIN met.forcing_version fv ON fv.forcing_version_id = h.forcing_version_id
            WHERE rt.basin_version_id = %s
              AND rt.river_segment_id = %s
              AND rt.variable = 'q_down'
              AND LOWER(h.run_type) = ANY(%s)
              AND rt.valid_time >= %s
              AND rt.valid_time <= %s
            ORDER BY h.scenario_id, rt.valid_time
            """,
            (basin_version_id, segment_id, list(run_types), start_time, end_time),
        )

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


def _run_type_tokens(run_types: Sequence[str] | None) -> list[str]:
    tokens = _normalized_tokens(run_types or [])
    if not tokens:
        return ["forecast"]
    return tokens


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


def _datetime_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if not value:
        return None
    if isinstance(value, str):
        try:
            return _ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _format_time_value(value: Any) -> str | None:
    return _format_time(_datetime_value(value))


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


def analysis_window_for_issue_time(issue_time: datetime) -> tuple[datetime, datetime]:
    end_time = _ensure_utc(issue_time)
    return end_time - timedelta(days=7), end_time


def _latest_cycle_time(cycle_times_by_scenario: Mapping[str, datetime]) -> datetime | None:
    if not cycle_times_by_scenario:
        return None
    return max(_ensure_utc(cycle_time) for cycle_time in cycle_times_by_scenario.values())


def _empty_forecast_response(*, segment_id: str, issue_time: datetime | None) -> dict[str, Any]:
    return {
        "segment_id": segment_id,
        "issue_time": _format_time(issue_time),
        "unit": "m3/s",
        "series": [],
        "frequency_thresholds": {},
    }


def _empty_spliced_response(
    *,
    river_segment_id: str,
    issue_time: datetime | None,
    variable: str,
    unit: str = "m3/s",
) -> dict[str, Any]:
    return {
        "segments": [],
        "issue_time": _format_time(issue_time),
        "river_segment_id": river_segment_id,
        "variable": variable,
        "unit": unit,
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
                **_forecast_series_metadata(row),
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


def _spliced_response_from_rows(
    *,
    river_segment_id: str,
    issue_time: datetime,
    variable: str,
    analysis_rows: Sequence[dict[str, Any]],
    forecast_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    unit = _unit_from_rows((*analysis_rows, *forecast_rows))
    normalized_issue_time = _ensure_utc(issue_time)
    analysis_data = [
        _segment_point(row) for row in analysis_rows if _ensure_utc(row["valid_time"]) < normalized_issue_time
    ]
    if analysis_data:
        segments.append(
            {
                "scenario": "analysis_true_field",
                "scenario_id": "analysis_true_field",
                "source": _source_label(analysis_rows[0]),
                "segment_role": "past_7_days",
                "data": analysis_data,
            }
        )

    forecast_by_scenario: dict[str, list[dict[str, Any]]] = {}
    forecast_source_by_scenario: dict[str, str] = {}
    forecast_metadata_by_scenario: dict[str, dict[str, Any]] = {}
    for row in forecast_rows:
        scenario = str(row.get("scenario_id") or "forecast_gfs_deterministic")
        forecast_by_scenario.setdefault(scenario, []).append(_segment_point(row))
        forecast_source_by_scenario.setdefault(scenario, _source_label(row))
        forecast_metadata_by_scenario.setdefault(scenario, _forecast_series_metadata(row))

    for scenario, data in forecast_by_scenario.items():
        segments.append(
            {
                "scenario": scenario,
                "scenario_id": scenario,
                "source": forecast_source_by_scenario[scenario],
                "segment_role": "future_7_days",
                **forecast_metadata_by_scenario[scenario],
                "data": sorted(data, key=lambda point: point["valid_time"]),
            }
        )

    return {
        "segments": segments,
        "issue_time": _format_time(normalized_issue_time),
        "river_segment_id": river_segment_id,
        "variable": variable,
        "unit": unit,
    }


def _segment_point(row: dict[str, Any]) -> dict[str, Any]:
    return {"valid_time": _format_time(row["valid_time"]), "value": float(row["value"])}


def _unit_from_rows(rows: Sequence[dict[str, Any]]) -> str:
    for row in rows:
        unit = row.get("unit")
        if unit:
            return str(unit)
    return "m3/s"


def _source_label(row: dict[str, Any]) -> str:
    source_id = str(row.get("source_id") or "unknown")
    lineage = row.get("lineage_json") or {}
    if isinstance(lineage, dict) and lineage.get("fallback_reason"):
        fallback_source = str(lineage.get("fallback_source_id") or source_id)
        return f"{_display_source_id(fallback_source)} fallback"
    return _display_source_id(source_id)


def _display_source_id(source_id: str) -> str:
    normalized = source_id.strip()
    return normalized.upper() if normalized else "unknown"


def _forecast_series_metadata(row: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    source_id = _forecast_source_id(row)
    if source_id is not None:
        metadata["source_id"] = source_id
    cycle_time = _format_time_value(row.get("cycle_time"))
    if cycle_time is not None:
        metadata["cycle_time"] = cycle_time
    available_lead_hours = _available_lead_hours(row)
    if available_lead_hours is not None:
        metadata["available_lead_hours"] = available_lead_hours
    return metadata


def _forecast_source_id(row: dict[str, Any]) -> str | None:
    raw_source_id = row.get("source_id")
    if raw_source_id:
        return _display_source_id(str(raw_source_id))

    scenario_id = str(row.get("scenario_id") or "").strip().lower()
    prefix = "forecast_"
    suffix = "_deterministic"
    if scenario_id.startswith(prefix) and scenario_id.endswith(suffix):
        source_id = scenario_id[len(prefix) : -len(suffix)]
        return _display_source_id(source_id)
    return None


def _available_lead_hours(row: dict[str, Any]) -> int | None:
    explicit_lead_hours = _optional_int(row.get("available_lead_hours"))
    if explicit_lead_hours is not None:
        return explicit_lead_hours

    lineage_lead_hours = _optional_int(_lineage_dict(row.get("lineage_json")).get("max_lead_hours"))
    if lineage_lead_hours is not None:
        return lineage_lead_hours

    cycle_time = _datetime_value(row.get("cycle_time"))
    source_id = _forecast_source_id(row)
    if source_id == "IFS" and cycle_time is not None:
        if cycle_time.hour in {6, 18}:
            return 144
        if cycle_time.hour in {0, 12}:
            return 168

    run_end_time = _datetime_value(row.get("run_end_time") or row.get("end_time"))
    elapsed_lead_hours = _elapsed_lead_hours(cycle_time, run_end_time)
    if elapsed_lead_hours is not None:
        return elapsed_lead_hours

    if source_id is not None:
        return 168
    return None


def _lineage_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _elapsed_lead_hours(start_time: datetime | None, end_time: datetime | None) -> int | None:
    if start_time is None or end_time is None:
        return None
    elapsed_seconds = (_ensure_utc(end_time) - _ensure_utc(start_time)).total_seconds()
    if elapsed_seconds <= 0:
        return None
    return int(round(elapsed_seconds / 3600.0))


def _response_variable_name(requested_variables: Sequence[str]) -> str:
    return "discharge" if "q_down" in requested_variables or not requested_variables else str(requested_variables[0])


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
