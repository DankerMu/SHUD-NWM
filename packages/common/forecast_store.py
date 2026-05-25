from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

MVP_STATION_VARIABLES = ("PRCP", "TEMP", "RH", "wind", "Rn", "Press")
DEFAULT_STATION_SERIES_LIMIT = 500
MAX_STATION_SERIES_LIMIT = 10000
QHH_BASIN_ID = "basins_qhh"
QHH_LATEST_CANDIDATE_LIMIT = 100
QHH_LATEST_CONTEXT_LIMIT = 10
QHH_LATEST_EXPECTED_HORIZON_HOURS = 168
QHH_LATEST_SUPPORTED_SOURCES = ("GFS", "IFS")
QHH_LATEST_READY_RUN_STATUSES = ("frequency_done", "published")


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
        river_network_version_id: str,
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
            self._validate_series_target(
                cursor,
                basin_version_id=basin_version_id,
                segment_id=segment_id,
                river_network_version_id=river_network_version_id,
            )

            if "hindcast" in run_type_tokens and not include_analysis:
                parsed_issue_time = None if issue_time == "latest" else _parse_datetime(issue_time)
                selected_issue_time = parsed_issue_time or self._latest_run_type_valid_time(
                    cursor,
                    basin_version_id=basin_version_id,
                    segment_id=segment_id,
                    river_network_version_id=river_network_version_id,
                    run_types=run_type_tokens,
                )
                if selected_issue_time is None:
                    return _empty_forecast_response(segment_id=segment_id, issue_time=None)
                rows = self._fetch_run_type_segment_rows(
                    cursor,
                    basin_version_id=basin_version_id,
                    segment_id=segment_id,
                    river_network_version_id=river_network_version_id,
                    run_types=run_type_tokens,
                    end_time=selected_issue_time,
                )
                thresholds = self._frequency_thresholds_for_rows(cursor, rows, segment_id=segment_id)
                return _forecast_response_from_rows(
                    segment_id=segment_id,
                    issue_time=selected_issue_time,
                    rows=rows,
                    frequency_thresholds=thresholds,
                )

            parsed_issue_time = None if issue_time == "latest" else _parse_datetime(issue_time)
            latest_cycles_by_scenario: dict[str, datetime] = {}
            if parsed_issue_time is None:
                latest_cycles_by_scenario = self._per_source_latest_cycles(
                    cursor,
                    basin_version_id=basin_version_id,
                    segment_id=segment_id,
                    river_network_version_id=river_network_version_id,
                    scenario_filter=scenario_filter,
                )
            selected_issue_time = parsed_issue_time or _latest_cycle_time(latest_cycles_by_scenario)
            if include_analysis and selected_issue_time is None:
                selected_issue_time = self._latest_analysis_issue_time(
                    cursor,
                    basin_version_id=basin_version_id,
                    segment_id=segment_id,
                    river_network_version_id=river_network_version_id,
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
                    river_network_version_id=river_network_version_id,
                    start_time=analysis_start,
                    end_time=analysis_end,
                )
                forecast_rows = self._fetch_forecast_segment_rows(
                    cursor,
                    basin_version_id=basin_version_id,
                    segment_id=segment_id,
                    river_network_version_id=river_network_version_id,
                    issue_time=selected_issue_time,
                    scenario_filter=scenario_filter,
                    cycle_times_by_scenario=None if parsed_issue_time is not None else latest_cycles_by_scenario,
                    end_time=forecast_end,
                )
                thresholds = self._frequency_thresholds_for_rows(cursor, forecast_rows, segment_id=segment_id)
                return _spliced_response_from_rows(
                    river_segment_id=segment_id,
                    issue_time=selected_issue_time,
                    variable=_response_variable_name(requested_variables),
                    analysis_rows=analysis_rows,
                    forecast_rows=forecast_rows,
                    frequency_thresholds=thresholds,
                )

            rows = self._fetch_forecast_segment_rows(
                cursor,
                basin_version_id=basin_version_id,
                segment_id=segment_id,
                river_network_version_id=river_network_version_id,
                issue_time=selected_issue_time,
                scenario_filter=scenario_filter,
                cycle_times_by_scenario=None if parsed_issue_time is not None else latest_cycles_by_scenario,
                end_time=selected_issue_time + timedelta(days=7),
            )
            thresholds = self._frequency_thresholds_for_rows(cursor, rows, segment_id=segment_id)

        if not rows and parsed_issue_time is not None:
            raise ForecastStoreError(
                status_code=404,
                code="RUN_NOT_PUBLISHED",
                message=f"No published forecast exists for issue_time {issue_time}.",
                details={
                    "basin_version_id": basin_version_id,
                    "segment_id": segment_id,
                    "river_network_version_id": river_network_version_id,
                    "issue_time": issue_time,
                },
            )

        return _forecast_response_from_rows(
            segment_id=segment_id,
            issue_time=selected_issue_time,
            rows=rows,
            frequency_thresholds=thresholds,
        )

    def _validate_series_target(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        river_network_version_id: str,
    ) -> None:
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
              AND rs.river_network_version_id = %s
            LIMIT 1
            """,
            (basin_version_id, segment_id, river_network_version_id),
        )
        if segment is None:
            raise ForecastStoreError(
                status_code=404,
                code="SEGMENT_NOT_FOUND",
                message=f"River segment not found: {segment_id}",
                details={
                    "basin_version_id": basin_version_id,
                    "segment_id": segment_id,
                    "river_network_version_id": river_network_version_id,
                },
            )

    def _latest_issue_time(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        river_network_version_id: str,
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
              AND rt.river_network_version_id = %s
              AND rt.variable = 'q_down'
              AND h.cycle_time IS NOT NULL
              {scenario_filter.sql}
            ORDER BY h.cycle_time DESC
            LIMIT 1
            """,
            (basin_version_id, segment_id, river_network_version_id, *scenario_filter.params),
        )
        return _ensure_utc(row["cycle_time"]) if row is not None else None

    def _per_source_latest_cycles(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        river_network_version_id: str,
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
              AND rt.river_network_version_id = %s
              AND rt.variable = 'q_down'
              AND h.run_type = 'forecast'
              AND h.cycle_time IS NOT NULL
              {scenario_filter.sql}
            GROUP BY h.scenario_id
            ORDER BY h.scenario_id
            """,
            (basin_version_id, segment_id, river_network_version_id, *scenario_filter.params),
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
        river_network_version_id: str,
    ) -> datetime | None:
        row = self._fetch_optional(
            cursor,
            """
            SELECT h.end_time
            FROM hydro.river_timeseries rt
            JOIN hydro.hydro_run h ON h.run_id = rt.run_id
            WHERE rt.basin_version_id = %s
              AND rt.river_segment_id = %s
              AND rt.river_network_version_id = %s
              AND rt.variable = 'q_down'
              AND h.scenario_id = 'analysis_true_field'
              AND h.end_time IS NOT NULL
            ORDER BY h.end_time DESC, h.created_at DESC
            LIMIT 1
            """,
            (basin_version_id, segment_id, river_network_version_id),
        )
        return _ensure_utc(row["end_time"]) if row is not None else None

    def _fetch_analysis_segment_rows(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        river_network_version_id: str,
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
              AND rt.river_network_version_id = %s
              AND rt.variable = 'q_down'
              AND h.scenario_id = 'analysis_true_field'
              AND rt.valid_time >= %s
              AND rt.valid_time < %s
            ORDER BY rt.valid_time, h.end_time DESC, h.created_at DESC
            """,
            (basin_version_id, segment_id, river_network_version_id, start_time, end_time),
        )

    def _fetch_forecast_segment_rows(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        river_network_version_id: str,
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
                    h.model_id,
                    h.source_id,
                    h.cycle_time,
                    h.end_time AS run_end_time,
                    fv.lineage_json,
                    rt.river_network_version_id,
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
                  AND rt.river_network_version_id = %s
                  AND rt.variable = 'q_down'
                  AND h.run_type = 'forecast'
                  AND rt.valid_time >= h.cycle_time
                  AND rt.valid_time <= h.cycle_time + INTERVAL '7 days'
                  {scenario_filter.sql}
                ORDER BY h.scenario_id, rt.valid_time
                """,
                (
                    *selected_cycle_params,
                    basin_version_id,
                    segment_id,
                    river_network_version_id,
                    *scenario_filter.params,
                ),
            )

        forecast_end = end_time or issue_time + timedelta(days=7)
        return self._fetch_all(
            cursor,
            f"""
            SELECT
                h.scenario_id,
                h.model_id,
                h.source_id,
                h.cycle_time,
                h.end_time AS run_end_time,
                fv.lineage_json,
                rt.river_network_version_id,
                rt.valid_time,
                rt.value,
                rt.unit
            FROM hydro.river_timeseries rt
            JOIN hydro.hydro_run h ON h.run_id = rt.run_id
            LEFT JOIN met.forcing_version fv ON fv.forcing_version_id = h.forcing_version_id
            WHERE rt.basin_version_id = %s
              AND rt.river_segment_id = %s
              AND rt.river_network_version_id = %s
              AND rt.variable = 'q_down'
              AND h.run_type = 'forecast'
              AND h.cycle_time = %s
              AND rt.valid_time >= %s
              AND rt.valid_time <= %s
              {scenario_filter.sql}
            ORDER BY h.scenario_id, rt.valid_time
            """,
            (
                basin_version_id,
                segment_id,
                river_network_version_id,
                issue_time,
                issue_time,
                forecast_end,
                *scenario_filter.params,
            ),
        )

    def _latest_run_type_valid_time(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        river_network_version_id: str,
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
              AND rt.river_network_version_id = %s
              AND rt.variable = 'q_down'
              AND LOWER(h.run_type) = ANY(%s)
            """,
            (basin_version_id, segment_id, river_network_version_id, list(run_types)),
        )
        return _ensure_utc(row["valid_time"]) if row is not None and row.get("valid_time") is not None else None

    def _fetch_run_type_segment_rows(
        self,
        cursor: Any,
        *,
        basin_version_id: str,
        segment_id: str,
        river_network_version_id: str,
        run_types: Sequence[str],
        end_time: datetime,
    ) -> list[dict[str, Any]]:
        start_time = end_time - timedelta(days=7)
        return self._fetch_all(
            cursor,
            """
            SELECT
                h.scenario_id,
                h.model_id,
                h.source_id,
                h.cycle_time,
                h.end_time AS run_end_time,
                fv.lineage_json,
                rt.river_network_version_id,
                rt.valid_time,
                rt.value,
                rt.unit
            FROM hydro.river_timeseries rt
            JOIN hydro.hydro_run h ON h.run_id = rt.run_id
            LEFT JOIN met.forcing_version fv ON fv.forcing_version_id = h.forcing_version_id
            WHERE rt.basin_version_id = %s
              AND rt.river_segment_id = %s
              AND rt.river_network_version_id = %s
              AND rt.variable = 'q_down'
              AND LOWER(h.run_type) = ANY(%s)
              AND rt.valid_time >= %s
              AND rt.valid_time <= %s
            ORDER BY h.scenario_id, rt.valid_time
            """,
            (basin_version_id, segment_id, river_network_version_id, list(run_types), start_time, end_time),
        )

    def _frequency_thresholds_for_rows(
        self,
        cursor: Any,
        rows: Sequence[dict[str, Any]],
        *,
        segment_id: str,
    ) -> dict[str, Any] | None:
        for row in rows:
            model_id = row.get("model_id")
            river_network_version_id = row.get("river_network_version_id")
            if model_id and river_network_version_id:
                return self._fetch_frequency_thresholds(
                    cursor,
                    model_id=str(model_id),
                    river_network_version_id=str(river_network_version_id),
                    segment_id=segment_id,
                )
        return None

    def _fetch_frequency_thresholds(
        self,
        cursor: Any,
        *,
        model_id: str,
        river_network_version_id: str,
        segment_id: str,
    ) -> dict[str, Any] | None:
        row = self._fetch_optional(
            cursor,
            """
            SELECT q2, q5, q10, q20, q50, q100, parameters_json
            FROM flood.flood_frequency_curve
            WHERE model_id = %s
              AND river_network_version_id = %s
              AND river_segment_id = %s
              AND duration = '1h'
              AND quality_flag IN ('ok', 'partial_sample', 'monotonicity_corrected')
            ORDER BY sample_period_end DESC
            LIMIT 1
            """,
            (model_id, river_network_version_id, segment_id),
        )
        if row is None:
            return None
        thresholds: dict[str, Any] = {
            "Q2": _optional_float(row.get("q2")),
            "Q5": _optional_float(row.get("q5")),
            "Q10": _optional_float(row.get("q10")),
            "Q20": _optional_float(row.get("q20")),
            "Q50": _optional_float(row.get("q50")),
            "Q100": _optional_float(row.get("q100")),
        }
        sample_quality = _lineage_dict(row.get("parameters_json")).get("sample_quality")
        if isinstance(sample_quality, dict):
            thresholds["sample_quality"] = sample_quality
        return thresholds

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._transaction() as cursor:
            row = self._fetch_optional(
                cursor,
                f"""
                SELECT
                    h.*,
                    mi.river_network_version_id,
                    bv.basin_id,
                    COALESCE(ds.adapter_name, h.source_id) AS source,
                    {_flood_product_quality_select("fpq")}
                FROM hydro.hydro_run h
                LEFT JOIN core.model_instance mi ON mi.model_id = h.model_id
                LEFT JOIN core.basin_version bv ON bv.basin_version_id = h.basin_version_id
                LEFT JOIN met.data_source ds ON ds.source_id = h.source_id
                {_flood_product_quality_join("fpq")}
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
        return _hydro_run_response(row)

    def list_runs(
        self,
        *,
        basin_id: str | None,
        source: str | None,
        cycle_time: datetime | None,
        status: str | None,
        flood_product_ready: bool | None = None,
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
        if flood_product_ready is True:
            clauses.append("h.status IN ('frequency_done', 'published')")
            clauses.append(_flood_product_ready_sql("fpq"))

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._transaction() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total_count
                FROM hydro.hydro_run h
                LEFT JOIN core.basin_version bv ON bv.basin_version_id = h.basin_version_id
                LEFT JOIN met.data_source ds ON ds.source_id = h.source_id
                {_flood_product_quality_join("fpq")}
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
                    mi.river_network_version_id,
                    bv.basin_id,
                    COALESCE(ds.adapter_name, h.source_id) AS source,
                    {_flood_product_quality_select("fpq")}
                FROM hydro.hydro_run h
                LEFT JOIN core.model_instance mi ON mi.model_id = h.model_id
                LEFT JOIN core.basin_version bv ON bv.basin_version_id = h.basin_version_id
                LEFT JOIN met.data_source ds ON ds.source_id = h.source_id
                {_flood_product_quality_join("fpq")}
                {where}
                ORDER BY h.cycle_time DESC NULLS LAST, h.created_at DESC, h.run_id
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
        return {
            "total_count": total_count,
            "items": [_hydro_run_response(row) for row in rows],
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

    def station_series(
        self,
        *,
        station_id: str,
        forcing_version_id: str | None = None,
        model_id: str | None = None,
        source_id: str | None = None,
        cycle_time: datetime | str | None = None,
        variables: Sequence[str] | str | None = None,
        from_time: datetime | str | None = None,
        to_time: datetime | str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        station_id = _required_text(station_id, "station_id")
        requested_variables = _station_variable_tokens(variables)
        selected_limit = _station_series_limit(limit)
        requested_from = _optional_datetime_filter(from_time, "from")
        requested_to = _optional_datetime_filter(to_time, "to")
        _validate_time_range(requested_from, requested_to)

        with self._transaction() as cursor:
            station = self._fetch_station_for_series(cursor, station_id=station_id)
            forcing_version = self._select_forcing_version(
                cursor,
                forcing_version_id=forcing_version_id,
                model_id=model_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            valid_time_start, valid_time_end = _forcing_version_time_window(forcing_version)
            self._validate_station_forcing_membership(
                cursor,
                station_id=station_id,
                forcing_version=forcing_version,
                valid_time_start=valid_time_start,
                valid_time_end=valid_time_end,
            )
            rows = self._fetch_station_series_rows(
                cursor,
                station_id=station_id,
                forcing_version_id=str(forcing_version["forcing_version_id"]),
                valid_time_start=valid_time_start,
                valid_time_end=valid_time_end,
                variables=requested_variables,
                from_time=requested_from,
                to_time=requested_to,
                limit=selected_limit,
            )
            rows = _station_series_rows_within_window(
                rows,
                valid_time_start=valid_time_start,
                valid_time_end=valid_time_end,
            )

        return _station_series_response(
            station=station,
            forcing_version=forcing_version,
            requested_variables=requested_variables,
            requested_from=requested_from,
            requested_to=requested_to,
            limit=selected_limit,
            rows=rows,
        )

    def station_forcing_readiness(
        self,
        *,
        forcing_version_id: str | None = None,
        model_id: str | None = None,
        source_id: str | None = None,
        cycle_time: datetime | str | None = None,
        expected_station_count: int | None = None,
        required_variables: Sequence[str] | str | None = None,
    ) -> dict[str, Any]:
        variables = _station_variable_tokens(required_variables)
        expected_count = _optional_non_negative_int(expected_station_count, "expected_station_count")

        with self._transaction() as cursor:
            forcing_version = self._select_forcing_version(
                cursor,
                forcing_version_id=forcing_version_id,
                model_id=model_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            valid_time_start, valid_time_end = _forcing_version_time_window(forcing_version)
            overall = self._fetch_forcing_readiness_overall(
                cursor,
                forcing_version_id=str(forcing_version["forcing_version_id"]),
                valid_time_start=valid_time_start,
                valid_time_end=valid_time_end,
                variables=variables,
            )
            coverage_rows = self._fetch_forcing_readiness_variable_rows(
                cursor,
                forcing_version_id=str(forcing_version["forcing_version_id"]),
                valid_time_start=valid_time_start,
                valid_time_end=valid_time_end,
                variables=variables,
            )

        return _station_forcing_readiness_response(
            forcing_version=forcing_version,
            expected_station_count=expected_count,
            required_variables=variables,
            overall=overall,
            coverage_rows=coverage_rows,
        )

    def latest_qhh_display_product(self, source: str) -> dict[str, Any]:
        source_id = _qhh_latest_source_id(source)
        with self._transaction() as cursor:
            rows = self._fetch_latest_qhh_display_candidates(cursor, source_id=source_id)

        evaluations = [_qhh_latest_candidate_response(row) for row in rows]
        for evaluation in evaluations:
            if evaluation["ready"]:
                return evaluation["product"]

        reasons: list[dict[str, Any]] = []
        for evaluation in evaluations[:QHH_LATEST_CONTEXT_LIMIT]:
            reasons.extend(evaluation["unavailable_reasons"])
        if not reasons:
            reasons.append(
                {
                    "code": "NO_CANDIDATES",
                    "message": f"No QHH display-product candidates were found for source {source_id}.",
                    "source_id": source_id,
                    "basin_id": QHH_BASIN_ID,
                }
            )
        raise ForecastStoreError(
            status_code=404,
            code="QHH_LATEST_PRODUCT_UNAVAILABLE",
            message=f"No usable latest QHH display product is available for source {source_id}.",
            details={
                "source_id": source_id,
                "basin_id": QHH_BASIN_ID,
                "status": "unavailable",
                "candidate_limit": QHH_LATEST_CANDIDATE_LIMIT,
                "candidate_count": len(evaluations),
                "unavailable_reasons": reasons,
                "candidates": [_qhh_latest_candidate_summary(evaluation) for evaluation in evaluations],
            },
        )

    def _fetch_latest_qhh_display_candidates(self, cursor: Any, *, source_id: str) -> list[dict[str, Any]]:
        return self._fetch_all(
            cursor,
            """
            WITH candidate_runs AS (
                SELECT
                    h.run_id,
                    h.run_type,
                    h.scenario_id,
                    h.model_id,
                    h.basin_version_id,
                    h.forcing_version_id,
                    h.source_id,
                    h.cycle_time,
                    h.start_time AS run_start_time,
                    h.end_time AS run_end_time,
                    h.status,
                    h.created_at AS run_created_at,
                    h.updated_at AS run_updated_at,
                    mi.river_network_version_id,
                    mi.basin_version_id AS model_basin_version_id,
                    bv.basin_id,
                    rnv.basin_version_id AS river_network_basin_version_id,
                    rnv.segment_count AS expected_segment_count,
                    fv.forcing_version_id AS fv_forcing_version_id,
                    fv.model_id AS forcing_model_id,
                    fv.source_id AS forcing_source_id,
                    fv.cycle_time AS forcing_cycle_time,
                    fv.start_time AS forcing_start_time,
                    fv.end_time AS forcing_end_time,
                    fv.station_count AS expected_station_count,
                    fv.checksum AS forcing_checksum,
                    fv.lineage_json AS forcing_lineage_json,
                    GREATEST(h.cycle_time, h.start_time, fv.start_time) AS display_start_time,
                    LEAST(h.end_time, fv.end_time) AS display_end_time
                FROM hydro.hydro_run h
                JOIN core.basin_version bv
                  ON bv.basin_version_id = h.basin_version_id
                LEFT JOIN core.model_instance mi
                  ON mi.model_id = h.model_id
                LEFT JOIN core.river_network_version rnv
                  ON rnv.river_network_version_id = mi.river_network_version_id
                LEFT JOIN met.forcing_version fv
                  ON fv.forcing_version_id = h.forcing_version_id
                WHERE bv.basin_id = %s
                  AND h.run_type = 'forecast'
                  AND LOWER(h.source_id) = LOWER(%s)
                  AND h.cycle_time IS NOT NULL
                ORDER BY h.cycle_time DESC, h.run_id DESC
                LIMIT %s
            ),
            station_coverage AS (
                SELECT
                    fst.forcing_version_id,
                    fst.basin_version_id,
                    LOWER(fst.source_id) AS station_source_id,
                    COUNT(DISTINCT fst.station_id) AS station_count,
                    COUNT(*) AS station_sample_count,
                    MIN(fst.valid_time) AS station_valid_time_start,
                    MAX(fst.valid_time) AS station_valid_time_end
                FROM met.forcing_station_timeseries fst
                JOIN candidate_runs cr
                  ON cr.forcing_version_id = fst.forcing_version_id
                 AND fst.basin_version_id = cr.basin_version_id
                 AND LOWER(fst.source_id) = LOWER(cr.source_id)
                WHERE fst.variable = ANY(%s)
                  AND fst.valid_time >= cr.display_start_time
                  AND fst.valid_time <= cr.display_end_time
                  AND EXISTS (
                      SELECT 1
                      FROM met.interp_weight iw
                      WHERE iw.model_id = cr.model_id
                        AND iw.station_id = fst.station_id
                        AND iw.variable = fst.variable
                        AND LOWER(iw.source_id) = LOWER(cr.source_id)
                  )
                GROUP BY fst.forcing_version_id, fst.basin_version_id, LOWER(fst.source_id)
            ),
            station_variable_coverage AS (
                SELECT
                    variable_stats.forcing_version_id,
                    variable_stats.basin_version_id,
                    variable_stats.station_source_id,
                    jsonb_agg(
                        jsonb_build_object(
                            'variable', variable_stats.variable,
                            'station_count', variable_stats.station_count,
                            'sample_count', variable_stats.sample_count,
                            'unit_count', variable_stats.unit_count,
                            'quality_flag_count', variable_stats.quality_flag_count,
                            'missing_unit_samples', variable_stats.missing_unit_samples,
                            'missing_quality_flag_samples', variable_stats.missing_quality_flag_samples,
                            'valid_time_start', variable_stats.valid_time_start,
                            'valid_time_end', variable_stats.valid_time_end
                        )
                        ORDER BY variable_stats.variable
                    ) AS station_variable_coverage
                FROM (
                    SELECT
                        fst.forcing_version_id,
                        fst.basin_version_id,
                        LOWER(fst.source_id) AS station_source_id,
                        fst.variable,
                        COUNT(DISTINCT fst.station_id) AS station_count,
                        COUNT(*) AS sample_count,
                        COUNT(DISTINCT NULLIF(BTRIM(fst.unit), '')) AS unit_count,
                        COUNT(DISTINCT NULLIF(BTRIM(fst.quality_flag), '')) AS quality_flag_count,
                        SUM(CASE WHEN fst.unit IS NULL OR BTRIM(fst.unit) = '' THEN 1 ELSE 0 END)
                            AS missing_unit_samples,
                        SUM(CASE WHEN fst.quality_flag IS NULL OR BTRIM(fst.quality_flag) = '' THEN 1 ELSE 0 END)
                            AS missing_quality_flag_samples,
                        MIN(fst.valid_time) AS valid_time_start,
                        MAX(fst.valid_time) AS valid_time_end
                    FROM met.forcing_station_timeseries fst
                    JOIN candidate_runs cr
                      ON cr.forcing_version_id = fst.forcing_version_id
                     AND fst.basin_version_id = cr.basin_version_id
                     AND LOWER(fst.source_id) = LOWER(cr.source_id)
                    WHERE fst.variable = ANY(%s)
                      AND fst.valid_time >= cr.display_start_time
                      AND fst.valid_time <= cr.display_end_time
                      AND EXISTS (
                          SELECT 1
                          FROM met.interp_weight iw
                          WHERE iw.model_id = cr.model_id
                            AND iw.station_id = fst.station_id
                            AND iw.variable = fst.variable
                            AND LOWER(iw.source_id) = LOWER(cr.source_id)
                      )
                    GROUP BY fst.forcing_version_id, fst.basin_version_id, LOWER(fst.source_id), fst.variable
                ) variable_stats
                GROUP BY
                    variable_stats.forcing_version_id,
                    variable_stats.basin_version_id,
                    variable_stats.station_source_id
            ),
            hydro_coverage AS (
                SELECT
                    rt.run_id,
                    rt.basin_version_id,
                    rt.river_network_version_id,
                    COUNT(DISTINCT rt.river_segment_id) AS segment_count,
                    COUNT(*) AS river_sample_count,
                    MIN(rt.valid_time) AS river_valid_time_start,
                    MAX(rt.valid_time) AS river_valid_time_end,
                    MIN(rt.lead_time_hours) AS min_lead_time_hours,
                    MAX(rt.lead_time_hours) AS max_lead_time_hours
                FROM hydro.river_timeseries rt
                JOIN candidate_runs cr
                  ON cr.run_id = rt.run_id
                 AND cr.basin_version_id = rt.basin_version_id
                 AND cr.river_network_version_id = rt.river_network_version_id
                WHERE rt.variable = 'q_down'
                  AND rt.valid_time >= cr.display_start_time
                  AND rt.valid_time <= cr.display_end_time
                GROUP BY rt.run_id, rt.basin_version_id, rt.river_network_version_id
            )
            SELECT
                cr.*,
                COALESCE(sc.station_count, 0) AS station_count,
                COALESCE(sc.station_sample_count, 0) AS station_sample_count,
                sc.basin_version_id AS station_basin_version_id,
                sc.station_source_id,
                sc.station_valid_time_start,
                sc.station_valid_time_end,
                COALESCE(svc.station_variable_coverage, '[]'::jsonb) AS station_variable_coverage,
                COALESCE(hc.segment_count, 0) AS segment_count,
                COALESCE(hc.river_sample_count, 0) AS river_sample_count,
                hc.river_valid_time_start,
                hc.river_valid_time_end,
                hc.min_lead_time_hours,
                hc.max_lead_time_hours
            FROM candidate_runs cr
            LEFT JOIN station_coverage sc
              ON sc.forcing_version_id = cr.forcing_version_id
             AND sc.basin_version_id = cr.basin_version_id
             AND sc.station_source_id = LOWER(cr.source_id)
            LEFT JOIN station_variable_coverage svc
              ON svc.forcing_version_id = cr.forcing_version_id
             AND svc.basin_version_id = cr.basin_version_id
             AND svc.station_source_id = LOWER(cr.source_id)
            LEFT JOIN hydro_coverage hc
              ON hc.run_id = cr.run_id
             AND hc.basin_version_id = cr.basin_version_id
             AND hc.river_network_version_id = cr.river_network_version_id
            ORDER BY cr.cycle_time DESC, cr.run_id DESC
            """,
            (
                QHH_BASIN_ID,
                source_id,
                QHH_LATEST_CANDIDATE_LIMIT,
                list(MVP_STATION_VARIABLES),
                list(MVP_STATION_VARIABLES),
            ),
        )

    def _fetch_station_for_series(self, cursor: Any, *, station_id: str) -> dict[str, Any]:
        station = self._fetch_optional(
            cursor,
            """
            SELECT
                station_id,
                basin_version_id,
                station_name,
                ST_X(geom) AS longitude,
                ST_Y(geom) AS latitude,
                elevation_m,
                station_role,
                active_flag,
                properties_json
            FROM met.met_station
            WHERE station_id = %s
            """,
            (station_id,),
        )
        if station is None:
            raise ForecastStoreError(
                status_code=404,
                code="STATION_NOT_FOUND",
                message=f"Station not found: {station_id}",
                details={"station_id": station_id},
            )
        return station

    def _select_forcing_version(
        self,
        cursor: Any,
        *,
        forcing_version_id: str | None,
        model_id: str | None,
        source_id: str | None,
        cycle_time: datetime | str | None,
    ) -> dict[str, Any]:
        if forcing_version_id is not None:
            row = self._fetch_forcing_version_by_id(
                cursor,
                forcing_version_id=_required_text(forcing_version_id, "forcing_version_id"),
            )
            _validate_forcing_version_filter_consistency(
                row,
                model_id=model_id,
                source_id=source_id,
                cycle_time=cycle_time,
            )
            _ensure_forcing_version_finalized(row)
            return row

        model_token = str(model_id or "").strip()
        source_token = _source_lookup_token(source_id)
        if not model_token or not source_token or cycle_time is None:
            raise ForecastStoreError(
                status_code=422,
                code="MISSING_REQUIRED_FILTER",
                message=(
                    "forcing_version_id or model_id, source_id, and cycle_time are required "
                    "for station series queries."
                ),
                details={
                    "required_alternatives": [
                        ["forcing_version_id"],
                        ["model_id", "source_id", "cycle_time"],
                    ]
                },
            )

        parsed_cycle_time = _required_datetime_filter(cycle_time, "cycle_time")
        rows = self._fetch_all(
            cursor,
            """
            SELECT
                forcing_version_id,
                model_id,
                source_id,
                cycle_time,
                start_time,
                end_time,
                station_count,
                forcing_package_uri,
                checksum,
                lineage_json,
                created_at
            FROM met.forcing_version
            WHERE model_id = %s
              AND LOWER(source_id) = %s
              AND cycle_time = %s
            ORDER BY created_at DESC, forcing_version_id
            LIMIT 2
            """,
            (model_token, source_token, parsed_cycle_time),
        )
        if not rows:
            raise ForecastStoreError(
                status_code=404,
                code="FORCING_VERSION_NOT_FOUND",
                message="Forcing version not found for model_id, source_id, and cycle_time.",
                details={
                    "model_id": model_token,
                    "source_id": source_token,
                    "cycle_time": _format_time(parsed_cycle_time),
                },
            )
        if len(rows) > 1:
            raise ForecastStoreError(
                status_code=409,
                code="FORCING_VERSION_AMBIGUOUS",
                message="Multiple forcing versions match model_id, source_id, and cycle_time.",
                details={
                    "model_id": model_token,
                    "source_id": source_token,
                    "cycle_time": _format_time(parsed_cycle_time),
                    "candidates": [
                        {
                            "forcing_version_id": row.get("forcing_version_id"),
                            "created_at": _format_time_value(row.get("created_at")),
                        }
                        for row in rows[:2]
                    ],
                },
            )
        _ensure_forcing_version_finalized(rows[0])
        return rows[0]

    def _fetch_forcing_version_by_id(self, cursor: Any, *, forcing_version_id: str) -> dict[str, Any]:
        row = self._fetch_optional(
            cursor,
            """
            SELECT
                forcing_version_id,
                model_id,
                source_id,
                cycle_time,
                start_time,
                end_time,
                station_count,
                forcing_package_uri,
                checksum,
                lineage_json,
                created_at
            FROM met.forcing_version
            WHERE forcing_version_id = %s
            """,
            (forcing_version_id,),
        )
        if row is None:
            raise ForecastStoreError(
                status_code=404,
                code="FORCING_VERSION_NOT_FOUND",
                message=f"Forcing version not found: {forcing_version_id}",
                details={"forcing_version_id": forcing_version_id},
            )
        return row

    def _validate_station_forcing_membership(
        self,
        cursor: Any,
        *,
        station_id: str,
        forcing_version: Mapping[str, Any],
        valid_time_start: datetime,
        valid_time_end: datetime,
    ) -> None:
        forcing_version_id = str(forcing_version["forcing_version_id"])
        row = self._fetch_optional(
            cursor,
            """
            SELECT 1 AS present
            FROM met.forcing_station_timeseries
            WHERE forcing_version_id = %s
              AND station_id = %s
              AND valid_time >= %s
              AND valid_time <= %s
            LIMIT 1
            """,
            (forcing_version_id, station_id, valid_time_start, valid_time_end),
        )
        if row is None:
            raise ForecastStoreError(
                status_code=404,
                code="STATION_NOT_IN_FORCING_VERSION",
                message="Station has no finalized forcing samples for the selected forcing version.",
                details={
                    "station_id": station_id,
                    "forcing_version_id": forcing_version_id,
                    "valid_time_start": _format_time(valid_time_start),
                    "valid_time_end": _format_time(valid_time_end),
                },
            )

    def _fetch_station_series_rows(
        self,
        cursor: Any,
        *,
        station_id: str,
        forcing_version_id: str,
        valid_time_start: datetime,
        valid_time_end: datetime,
        variables: Sequence[str],
        from_time: datetime | None,
        to_time: datetime | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses = [
            "fst.forcing_version_id = %s",
            "fst.station_id = %s",
            "fst.variable = requested.variable",
            "fst.valid_time >= %s",
            "fst.valid_time <= %s",
        ]
        params: list[Any] = [forcing_version_id, station_id, valid_time_start, valid_time_end]
        if from_time is not None:
            clauses.append("fst.valid_time >= %s")
            params.append(from_time)
        if to_time is not None:
            clauses.append("fst.valid_time <= %s")
            params.append(to_time)
        where = " AND ".join(clauses)
        return self._fetch_all(
            cursor,
            f"""
            WITH requested(variable, ordinal) AS (
                SELECT variable, ordinal
                FROM unnest(%s::text[]) WITH ORDINALITY AS variables(variable, ordinal)
            )
            SELECT
                limited.forcing_version_id,
                limited.station_id,
                limited.variable,
                limited.valid_time,
                limited.value,
                limited.unit,
                limited.native_resolution,
                limited.quality_flag,
                limited.source_id,
                ROW_NUMBER() OVER (
                    PARTITION BY limited.variable
                    ORDER BY limited.valid_time
                ) AS row_number
            FROM requested
            CROSS JOIN LATERAL (
                SELECT
                    fst.forcing_version_id,
                    fst.station_id,
                    fst.variable,
                    fst.valid_time,
                    fst.value,
                    fst.unit,
                    fst.native_resolution,
                    fst.quality_flag,
                    fst.source_id
                FROM met.forcing_station_timeseries fst
                WHERE {where}
                ORDER BY fst.valid_time
                LIMIT %s
            ) limited
            ORDER BY requested.ordinal, limited.valid_time
            """,
            (list(variables), *params, limit + 1),
        )

    def _fetch_forcing_readiness_overall(
        self,
        cursor: Any,
        *,
        forcing_version_id: str,
        valid_time_start: datetime,
        valid_time_end: datetime,
        variables: Sequence[str],
    ) -> dict[str, Any]:
        row = self._fetch_optional(
            cursor,
            """
            SELECT
                COUNT(DISTINCT station_id) AS actual_station_count,
                COUNT(*) AS sample_count,
                MIN(valid_time) AS valid_time_start,
                MAX(valid_time) AS valid_time_end
            FROM met.forcing_station_timeseries
            WHERE forcing_version_id = %s
              AND valid_time >= %s
              AND valid_time <= %s
              AND variable = ANY(%s)
            """,
            (forcing_version_id, valid_time_start, valid_time_end, list(variables)),
        )
        return row or {
            "actual_station_count": 0,
            "sample_count": 0,
            "valid_time_start": None,
            "valid_time_end": None,
        }

    def _fetch_forcing_readiness_variable_rows(
        self,
        cursor: Any,
        *,
        forcing_version_id: str,
        valid_time_start: datetime,
        valid_time_end: datetime,
        variables: Sequence[str],
    ) -> list[dict[str, Any]]:
        return self._fetch_all(
            cursor,
            """
            SELECT
                variable,
                COUNT(DISTINCT station_id) AS station_count,
                COUNT(*) AS sample_count,
                COUNT(DISTINCT NULLIF(BTRIM(unit), '')) AS unit_count,
                SUM(CASE WHEN unit IS NULL OR BTRIM(unit) = '' THEN 1 ELSE 0 END) AS missing_unit_samples,
                COUNT(DISTINCT NULLIF(BTRIM(quality_flag), '')) AS quality_flag_count,
                SUM(CASE WHEN quality_flag IS NULL OR BTRIM(quality_flag) = '' THEN 1 ELSE 0 END)
                    AS missing_quality_flag_samples,
                MIN(valid_time) AS valid_time_start,
                MAX(valid_time) AS valid_time_end
            FROM met.forcing_station_timeseries
            WHERE forcing_version_id = %s
              AND valid_time >= %s
              AND valid_time <= %s
              AND variable = ANY(%s)
            GROUP BY variable
            ORDER BY variable
            """,
            (forcing_version_id, valid_time_start, valid_time_end, list(variables)),
        )

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
        self.connection.set_session(isolation_level="REPEATABLE READ", readonly=True, autocommit=False)
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


def _required_text(value: str | None, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"{field} is required.",
            details={"field": field},
        )
    return normalized


def _station_variable_tokens(values: Sequence[str] | str | None) -> list[str]:
    if not values:
        return list(MVP_STATION_VARIABLES)

    aliases = {variable.lower(): variable for variable in MVP_STATION_VARIABLES}
    tokens: list[str] = []
    rejected: list[str] = []
    raw_values: Sequence[str]
    if isinstance(values, str):
        raw_values = [values]
    else:
        raw_values = values
    for value in raw_values:
        for token in str(value).split(","):
            raw = token.strip()
            if not raw:
                continue
            canonical = aliases.get(raw.lower())
            if canonical is None:
                rejected.append(raw)
                continue
            if canonical not in tokens:
                tokens.append(canonical)
    if rejected:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Invalid station forcing variable.",
            details={"field": "variables", "rejected_values": rejected, "allowed_values": list(MVP_STATION_VARIABLES)},
        )
    return tokens or list(MVP_STATION_VARIABLES)


def _station_series_limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_STATION_SERIES_LIMIT
    try:
        limit = int(value)
    except (TypeError, ValueError) as error:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="limit must be an integer.",
            details={"field": "limit", "rejected_value": value},
        ) from error
    if limit < 1 or limit > MAX_STATION_SERIES_LIMIT:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"limit must be between 1 and {MAX_STATION_SERIES_LIMIT}.",
            details={"field": "limit", "rejected_value": value, "max": MAX_STATION_SERIES_LIMIT},
        )
    return limit


def _optional_non_negative_int(value: int | None, field: str) -> int | None:
    if value is None:
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError) as error:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"{field} must be a non-negative integer.",
            details={"field": field, "rejected_value": value},
        ) from error
    if normalized < 0:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"{field} must be a non-negative integer.",
            details={"field": field, "rejected_value": value},
        )
    return normalized


def _optional_datetime_filter(value: datetime | str | None, field: str) -> datetime | None:
    if value in (None, ""):
        return None
    return _required_datetime_filter(value, field)


def _required_datetime_filter(value: datetime | str, field: str) -> datetime:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    try:
        return _ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError as error:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message=f"{field} must be an ISO 8601 timestamp.",
            details={"field": field, "rejected_value": value},
        ) from error


def _source_lookup_token(value: str | None) -> str:
    return str(value or "").strip().lower()


def _forcing_version_time_window(forcing_version: Mapping[str, Any]) -> tuple[datetime, datetime]:
    forcing_version_id = str(forcing_version.get("forcing_version_id") or "")
    start_time = _datetime_value(forcing_version.get("start_time"))
    end_time = _datetime_value(forcing_version.get("end_time"))
    if start_time is None or end_time is None or start_time > end_time:
        raise ForecastStoreError(
            status_code=409,
            code="FORCING_VERSION_INVALID_WINDOW",
            message="Forcing version has an invalid valid-time window.",
            details={
                "forcing_version_id": forcing_version_id,
                "valid_time_start": _format_time(start_time),
                "valid_time_end": _format_time(end_time),
            },
        )
    return start_time, end_time


def _ensure_forcing_version_finalized(forcing_version: Mapping[str, Any]) -> None:
    checksum = str(forcing_version.get("checksum") or "").strip()
    if not checksum or checksum.lower() == "pending":
        raise ForecastStoreError(
            status_code=409,
            code="FORCING_VERSION_NOT_FINALIZED",
            message="Forcing version is not finalized and cannot be used for station forcing reads.",
            details={
                "forcing_version_id": forcing_version.get("forcing_version_id"),
                "checksum_state": checksum or None,
            },
        )


def _qhh_latest_source_id(source: str) -> str:
    normalized = str(source or "").strip().upper()
    if normalized not in QHH_LATEST_SUPPORTED_SOURCES:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="source must be GFS or IFS.",
            details={
                "field": "source",
                "rejected_value": source,
                "allowed_values": list(QHH_LATEST_SUPPORTED_SOURCES),
            },
        )
    return normalized


def _qhh_latest_candidate_response(row: Mapping[str, Any]) -> dict[str, Any]:
    reasons = _qhh_latest_unavailable_reasons(row)
    source_id = _display_source_id(str(row.get("source_id") or ""))
    cycle_time = _datetime_value(row.get("cycle_time"))
    available_start_time, available_end_time = _qhh_latest_available_window(row)
    horizon_hours = _qhh_latest_horizon_hours(
        row,
        cycle_time=cycle_time,
        available_end_time=available_end_time,
    )
    expected_horizon_hours = QHH_LATEST_EXPECTED_HORIZON_HOURS
    shorter_horizon = horizon_hours is not None and horizon_hours < expected_horizon_hours
    quality_flags: list[str] = []
    quality_notes: list[dict[str, Any]] = []
    if shorter_horizon:
        quality_flags.append("shorter_horizon")
        quality_notes.append(
            {
                "code": "SHORTER_HORIZON",
                "message": "Available horizon is shorter than the default seven-day display window.",
                "expected_horizon_hours": expected_horizon_hours,
                "available_horizon_hours": horizon_hours,
                "available_end_time": _format_time(available_end_time),
            }
        )

    product = {
        "basin_id": str(row.get("basin_id") or QHH_BASIN_ID),
        "model_id": str(row.get("model_id") or ""),
        "basin_version_id": str(row.get("basin_version_id") or ""),
        "river_network_version_id": str(row.get("river_network_version_id") or ""),
        "source_id": source_id,
        "cycle_time": _format_time(cycle_time),
        "run_id": str(row.get("run_id") or ""),
        "forcing_version_id": str(row.get("forcing_version_id") or ""),
        "station_count": _non_negative_int(row.get("station_count")),
        "expected_station_count": _optional_non_negative_response_int(row.get("expected_station_count")),
        "segment_count": _non_negative_int(row.get("segment_count")),
        "expected_segment_count": _optional_non_negative_response_int(row.get("expected_segment_count")),
        "status": "ready" if not reasons else "unavailable",
        "run_status": str(row.get("status") or ""),
        "valid_time_start": _format_time(available_start_time),
        "valid_time_end": _format_time(available_end_time),
        "river_valid_time_start": _format_time_value(row.get("river_valid_time_start")),
        "river_valid_time_end": _format_time_value(row.get("river_valid_time_end")),
        "forcing_valid_time_start": _format_time_value(row.get("forcing_start_time")),
        "forcing_valid_time_end": _format_time_value(row.get("forcing_end_time")),
        "available_horizon_hours": horizon_hours,
        "expected_horizon_hours": expected_horizon_hours,
        "shorter_horizon": shorter_horizon,
        "availability": {
            "ready": not reasons,
            "unavailable_reasons": reasons,
            "quality_flags": quality_flags,
            "quality_notes": quality_notes,
        },
        "quality": {
            "station_sample_count": _non_negative_int(row.get("station_sample_count")),
            "river_sample_count": _non_negative_int(row.get("river_sample_count")),
            "required_station_variables": list(MVP_STATION_VARIABLES),
            "station_variable_coverage": _qhh_station_variable_coverage(row.get("station_variable_coverage")),
            "candidate_limit": QHH_LATEST_CANDIDATE_LIMIT,
            "query_indexes": _qhh_latest_query_indexes(),
        },
    }
    return {"ready": not reasons, "product": product, "unavailable_reasons": reasons}


def _qhh_latest_unavailable_reasons(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    reasons: list[dict[str, Any]] = []
    run_id = str(row.get("run_id") or "")
    source_id = _display_source_id(str(row.get("source_id") or ""))

    def add(code: str, message: str, **extra: Any) -> None:
        reasons.append(
            {
                "code": code,
                "message": message,
                "run_id": run_id or None,
                "source_id": source_id,
                **{key: value for key, value in extra.items() if value is not None},
            }
        )

    run_status = str(row.get("status") or "")
    if run_status not in QHH_LATEST_READY_RUN_STATUSES:
        add(
            "RUN_STATUS_NOT_READY",
            "Hydro run is not in a display-ready terminal status.",
            run_status=run_status,
            allowed_statuses=list(QHH_LATEST_READY_RUN_STATUSES),
        )

    for field, code in (
        ("run_id", "RUN_ID_MISSING"),
        ("model_id", "MODEL_ID_MISSING"),
        ("basin_version_id", "BASIN_VERSION_ID_MISSING"),
        ("river_network_version_id", "RIVER_NETWORK_VERSION_ID_MISSING"),
        ("forcing_version_id", "FORCING_VERSION_ID_MISSING"),
        ("cycle_time", "CYCLE_TIME_MISSING"),
    ):
        if not row.get(field):
            add(code, f"{field} is required for a ready latest-product response.", field=field)

    if row.get("fv_forcing_version_id") is None and row.get("forcing_version_id"):
        add(
            "FORCING_VERSION_NOT_FOUND",
            "Hydro run references a forcing_version_id that does not exist.",
            forcing_version_id=row.get("forcing_version_id"),
        )
    if row.get("model_basin_version_id") and row.get("basin_version_id") != row.get("model_basin_version_id"):
        add(
            "MODEL_BASIN_MISMATCH",
            "Hydro run basin_version_id does not match model instance basin_version_id.",
            run_basin_version_id=row.get("basin_version_id"),
            model_basin_version_id=row.get("model_basin_version_id"),
        )
    if row.get("river_network_basin_version_id") and row.get("basin_version_id") != row.get(
        "river_network_basin_version_id"
    ):
        add(
            "RIVER_NETWORK_BASIN_MISMATCH",
            "Model river network version does not belong to the hydro run basin version.",
            run_basin_version_id=row.get("basin_version_id"),
            river_network_basin_version_id=row.get("river_network_basin_version_id"),
        )

    forcing_checksum = str(row.get("forcing_checksum") or "").strip()
    if not forcing_checksum or forcing_checksum.lower() == "pending":
        add(
            "FORCING_VERSION_NOT_FINALIZED",
            "Forcing version checksum is missing or pending.",
            forcing_version_id=row.get("forcing_version_id"),
            checksum_state=forcing_checksum or None,
        )

    if row.get("forcing_model_id") and row.get("model_id") and row["forcing_model_id"] != row["model_id"]:
        add(
            "FORCING_MODEL_MISMATCH",
            "Hydro run model_id does not match forcing version model_id.",
            run_model_id=row.get("model_id"),
            forcing_model_id=row.get("forcing_model_id"),
        )
    forcing_source = _display_source_id(str(row.get("forcing_source_id") or ""))
    if row.get("forcing_source_id") and source_id != forcing_source:
        add(
            "FORCING_SOURCE_MISMATCH",
            "Hydro run source_id does not match forcing version source_id.",
            run_source_id=source_id,
            forcing_source_id=forcing_source,
        )
    cycle_time = _datetime_value(row.get("cycle_time"))
    forcing_cycle_time = _datetime_value(row.get("forcing_cycle_time"))
    if row.get("fv_forcing_version_id") is not None and forcing_cycle_time is None:
        add(
            "FORCING_CYCLE_MISSING",
            "Forcing version cycle_time is required for a ready latest-product response.",
            forcing_version_id=row.get("forcing_version_id"),
        )
    elif cycle_time is not None and forcing_cycle_time is not None and cycle_time != forcing_cycle_time:
        add(
            "FORCING_CYCLE_MISMATCH",
            "Hydro run cycle_time does not match forcing version cycle_time.",
            cycle_time=_format_time(cycle_time),
            forcing_cycle_time=_format_time(forcing_cycle_time),
        )

    display_start_time = _datetime_value(row.get("display_start_time"))
    display_end_time = _datetime_value(row.get("display_end_time"))
    if row.get("fv_forcing_version_id") is not None and (display_start_time is None or display_end_time is None):
        add(
            "DISPLAY_WINDOW_MISSING",
            "A selected display window from hydro run and forcing windows is required.",
            run_start_time=_format_time_value(row.get("run_start_time")),
            run_end_time=_format_time_value(row.get("run_end_time")),
            forcing_start_time=_format_time_value(row.get("forcing_start_time")),
            forcing_end_time=_format_time_value(row.get("forcing_end_time")),
        )
    elif display_start_time is not None and display_end_time is not None and display_end_time <= display_start_time:
        add(
            "DISPLAY_WINDOW_NONPOSITIVE",
            "Selected display window must have positive duration.",
            display_start_time=_format_time(display_start_time),
            display_end_time=_format_time(display_end_time),
        )

    station_count = _non_negative_int(row.get("station_count"))
    expected_station_count = _optional_non_negative_response_int(row.get("expected_station_count"))
    if station_count <= 0:
        add(
            "STATION_FORCING_MISSING",
            "No station forcing samples were found for the selected basin/source/model station identity.",
        )
    if expected_station_count is not None and station_count != expected_station_count:
        add(
            "STATION_COUNT_MISMATCH",
            "Station forcing coverage does not match the expected station count.",
            expected=expected_station_count,
            actual=station_count,
        )
    station_basin_version_id = row.get("station_basin_version_id")
    if station_basin_version_id and row.get("basin_version_id") != station_basin_version_id:
        add(
            "STATION_BASIN_MISMATCH",
            "Station forcing rows do not match the selected basin_version_id.",
            run_basin_version_id=row.get("basin_version_id"),
            station_basin_version_id=station_basin_version_id,
        )
    station_source_id = _display_source_id(str(row.get("station_source_id") or ""))
    if row.get("station_source_id") and station_source_id != source_id:
        add(
            "STATION_SOURCE_MISMATCH",
            "Station forcing rows do not match the selected source_id.",
            run_source_id=source_id,
            station_source_id=station_source_id,
        )
    station_start = _datetime_value(row.get("station_valid_time_start"))
    station_end = _datetime_value(row.get("station_valid_time_end"))
    if station_count > 0:
        if station_start is None or station_end is None:
            add("STATION_VALID_TIME_MISSING", "Station forcing valid-time metadata is missing.")
        elif station_end < station_start:
            add("STATION_INVALID_VALID_TIME_RANGE", "Station forcing valid-time range is invalid.")
        if display_start_time is not None and station_start is not None and station_start < display_start_time:
            add(
                "STATION_WINDOW_UNDERFLOW",
                "Station forcing coverage includes samples before the selected display window.",
                display_start_time=_format_time(display_start_time),
                station_valid_time_start=_format_time(station_start),
            )
        if display_end_time is not None and station_end is not None and station_end > display_end_time:
            add(
                "STATION_WINDOW_OVERFLOW",
                "Station forcing coverage includes samples after the selected display window.",
                display_end_time=_format_time(display_end_time),
                station_valid_time_end=_format_time(station_end),
            )

    station_coverage = _qhh_station_variable_coverage(row.get("station_variable_coverage"))
    coverage_by_variable = {item["variable"]: item for item in station_coverage}
    for variable in MVP_STATION_VARIABLES:
        coverage = coverage_by_variable.get(variable)
        if coverage is None or int(coverage.get("sample_count") or 0) <= 0:
            add("STATION_VARIABLE_MISSING", "Required station forcing variable is missing.", variable=variable)
            continue
        variable_station_count = int(coverage.get("station_count") or 0)
        if expected_station_count is not None and variable_station_count != expected_station_count:
            add(
                "STATION_VARIABLE_COUNT_MISMATCH",
                "Station forcing variable coverage does not match the expected station count.",
                variable=variable,
                expected=expected_station_count,
                actual=variable_station_count,
            )
        if variable_station_count > 0 and int(coverage.get("sample_count") or 0) <= variable_station_count:
            add(
                "STATION_VARIABLE_SINGLE_TIMESTEP",
                "Station forcing variable coverage must include more than one timestep.",
                variable=variable,
                station_count=variable_station_count,
                sample_count=int(coverage.get("sample_count") or 0),
            )
        if int(coverage.get("unit_count") or 0) <= 0 or int(coverage.get("missing_unit_samples") or 0) > 0:
            add(
                "STATION_VARIABLE_UNIT_MISSING",
                "Station forcing variable has missing units.",
                variable=variable,
                missing_samples=int(coverage.get("missing_unit_samples") or 0),
            )
        if (
            int(coverage.get("quality_flag_count") or 0) <= 0
            or int(coverage.get("missing_quality_flag_samples") or 0) > 0
        ):
            add(
                "STATION_VARIABLE_QUALITY_FLAG_MISSING",
                "Station forcing variable has missing quality flags.",
                variable=variable,
                missing_samples=int(coverage.get("missing_quality_flag_samples") or 0),
            )
        variable_start = _datetime_value(coverage.get("valid_time_start"))
        variable_end = _datetime_value(coverage.get("valid_time_end"))
        if variable_start is None or variable_end is None:
            add(
                "STATION_VARIABLE_VALID_TIME_MISSING",
                "Station forcing variable valid-time metadata is missing.",
                variable=variable,
            )
        elif variable_end < variable_start:
            add(
                "STATION_VARIABLE_INVALID_VALID_TIME_RANGE",
                "Station forcing variable valid-time range is invalid.",
                variable=variable,
            )
        if display_start_time is not None and variable_start is not None and variable_start < display_start_time:
            add(
                "STATION_VARIABLE_WINDOW_UNDERFLOW",
                "Station forcing variable coverage includes samples before the selected display window.",
                variable=variable,
                display_start_time=_format_time(display_start_time),
                valid_time_start=_format_time(variable_start),
            )
        if display_end_time is not None and variable_end is not None and variable_end > display_end_time:
            add(
                "STATION_VARIABLE_WINDOW_OVERFLOW",
                "Station forcing variable coverage includes samples after the selected display window.",
                variable=variable,
                display_end_time=_format_time(display_end_time),
                valid_time_end=_format_time(variable_end),
            )

    segment_count = _non_negative_int(row.get("segment_count"))
    expected_segment_count = _optional_non_negative_response_int(row.get("expected_segment_count"))
    if segment_count <= 0 or _non_negative_int(row.get("river_sample_count")) <= 0:
        add("Q_DOWN_MISSING", "No river q_down samples were found inside the selected display window.")
    if expected_segment_count is not None and segment_count != expected_segment_count:
        add(
            "SEGMENT_COUNT_MISMATCH",
            "River q_down coverage does not match the expected segment count.",
            expected=expected_segment_count,
            actual=segment_count,
        )
    if segment_count > 0 and _non_negative_int(row.get("river_sample_count")) <= segment_count:
        add(
            "Q_DOWN_SINGLE_TIMESTEP",
            "River q_down coverage must include more than one timestep.",
            segment_count=segment_count,
            sample_count=_non_negative_int(row.get("river_sample_count")),
        )
    river_start = _datetime_value(row.get("river_valid_time_start"))
    river_end = _datetime_value(row.get("river_valid_time_end"))
    if river_start is None or river_end is None:
        add("Q_DOWN_VALID_TIME_MISSING", "River q_down valid-time metadata is missing.")
    elif river_end < river_start:
        add("Q_DOWN_INVALID_VALID_TIME_RANGE", "River q_down valid-time range is invalid.")
    if display_start_time is not None and river_start is not None and river_start < display_start_time:
        add(
            "Q_DOWN_WINDOW_UNDERFLOW",
            "River q_down coverage includes samples before the selected display window.",
            display_start_time=_format_time(display_start_time),
            river_valid_time_start=_format_time(river_start),
        )
    if display_end_time is not None and river_end is not None and river_end > display_end_time:
        add(
            "Q_DOWN_WINDOW_OVERFLOW",
            "River q_down coverage includes samples after the selected display window.",
            display_end_time=_format_time(display_end_time),
            river_valid_time_end=_format_time(river_end),
        )
    if cycle_time is not None and river_end is not None and _elapsed_lead_hours(cycle_time, river_end) is None:
        add(
            "Q_DOWN_HORIZON_NONPOSITIVE",
            "River q_down coverage must extend beyond the selected cycle_time.",
            cycle_time=_format_time(cycle_time),
            river_valid_time_end=_format_time(river_end),
        )
    max_lead_time_hours = _optional_int(row.get("max_lead_time_hours"))
    if max_lead_time_hours is not None and max_lead_time_hours <= 0:
        add(
            "Q_DOWN_LEAD_TIME_NONPOSITIVE",
            "River q_down max lead time must be positive for a ready latest-product response.",
            max_lead_time_hours=max_lead_time_hours,
        )

    available_start_time, available_end_time = _qhh_latest_available_window(row)
    if available_start_time is None or available_end_time is None:
        add(
            "DISPLAYABLE_WINDOW_MISSING",
            "Station forcing and river q_down coverage do not provide a common displayable window.",
            display_start_time=_format_time(display_start_time),
            display_end_time=_format_time(display_end_time),
            station_valid_time_start=_format_time(station_start),
            station_valid_time_end=_format_time(station_end),
            river_valid_time_start=_format_time(river_start),
            river_valid_time_end=_format_time(river_end),
        )
    elif available_end_time <= available_start_time:
        add(
            "DISPLAYABLE_WINDOW_NONPOSITIVE",
            "Station forcing and river q_down coverage do not overlap for a positive displayable window.",
            available_start_time=_format_time(available_start_time),
            available_end_time=_format_time(available_end_time),
        )
    if (
        cycle_time is not None
        and available_end_time is not None
        and _elapsed_lead_hours(cycle_time, available_end_time) is None
    ):
        add(
            "DISPLAYABLE_HORIZON_NONPOSITIVE",
            "The common displayable window must extend beyond the hydro cycle_time.",
            cycle_time=_format_time(cycle_time),
            available_end_time=_format_time(available_end_time),
        )

    return reasons


def _qhh_latest_horizon_hours(
    row: Mapping[str, Any],
    *,
    cycle_time: datetime | None,
    available_end_time: datetime | None,
) -> int | None:
    explicit_lead = _optional_int(row.get("max_lead_time_hours"))
    elapsed_lead = _elapsed_lead_hours(cycle_time, available_end_time)
    if explicit_lead is not None and elapsed_lead is not None:
        return min(explicit_lead, elapsed_lead)
    if explicit_lead is not None:
        return explicit_lead
    return elapsed_lead


def _qhh_latest_available_window(row: Mapping[str, Any]) -> tuple[datetime | None, datetime | None]:
    station_coverage = _qhh_station_variable_coverage(row.get("station_variable_coverage"))
    coverage_by_variable = {item["variable"]: item for item in station_coverage}
    variable_starts = [
        _datetime_value(coverage_by_variable[variable].get("valid_time_start"))
        for variable in MVP_STATION_VARIABLES
        if variable in coverage_by_variable
    ]
    variable_ends = [
        _datetime_value(coverage_by_variable[variable].get("valid_time_end"))
        for variable in MVP_STATION_VARIABLES
        if variable in coverage_by_variable
    ]
    return (
        _latest_datetime(
            _datetime_value(row.get("display_start_time")),
            _datetime_value(row.get("station_valid_time_start")),
            _datetime_value(row.get("river_valid_time_start")),
            *variable_starts,
        ),
        _earliest_datetime(
            _datetime_value(row.get("display_end_time")),
            _datetime_value(row.get("station_valid_time_end")),
            _datetime_value(row.get("river_valid_time_end")),
            *variable_ends,
        ),
    )


def _latest_datetime(*values: datetime | None) -> datetime | None:
    present = [_ensure_utc(value) for value in values if value is not None]
    return max(present) if present else None


def _earliest_datetime(*values: datetime | None) -> datetime | None:
    present = [_ensure_utc(value) for value in values if value is not None]
    return min(present) if present else None


def _qhh_station_variable_coverage(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = []
    if not isinstance(value, list):
        return []
    coverage: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        coverage.append(
            {
                "variable": str(item.get("variable") or ""),
                "station_count": _non_negative_int(item.get("station_count")),
                "sample_count": _non_negative_int(item.get("sample_count")),
                "unit_count": _non_negative_int(item.get("unit_count")),
                "quality_flag_count": _non_negative_int(item.get("quality_flag_count")),
                "missing_unit_samples": _non_negative_int(item.get("missing_unit_samples")),
                "missing_quality_flag_samples": _non_negative_int(item.get("missing_quality_flag_samples")),
                "valid_time_start": _format_time_value(item.get("valid_time_start")),
                "valid_time_end": _format_time_value(item.get("valid_time_end")),
            }
        )
    return coverage


def _qhh_latest_candidate_summary(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    product = evaluation["product"]
    return {
        "run_id": product.get("run_id"),
        "source_id": product.get("source_id"),
        "cycle_time": product.get("cycle_time"),
        "run_status": product.get("run_status"),
        "status": product.get("status"),
        "unavailable_reason_codes": [
            reason["code"] for reason in evaluation.get("unavailable_reasons", []) if reason.get("code")
        ],
    }


def _qhh_latest_query_indexes() -> list[dict[str, Any]]:
    return [
        {
            "table": "hydro.hydro_run",
            "index": "hydro_run_qhh_latest_candidate_idx",
            "status": "covered_by_latest_product_candidate_index",
            "columns": ["LOWER(source_id)", "run_type", "basin_version_id", "cycle_time DESC", "run_id DESC"],
            "predicate": "cycle_time IS NOT NULL",
        },
        {
            "table": "core.basin_version",
            "index": "basin_version_qhh_latest_lookup_idx",
            "status": "covered_by_latest_product_basin_lookup_index",
            "columns": ["basin_id", "basin_version_id"],
        },
        {
            "table": "hydro.river_timeseries",
            "index": "river_timeseries_qhh_latest_window_idx",
            "status": "covered_by_latest_product_window_index",
            "columns": ["run_id", "basin_version_id", "river_network_version_id", "variable", "valid_time DESC"],
        },
        {
            "table": "met.forcing_station_timeseries",
            "index": "forcing_station_timeseries_qhh_latest_window_idx",
            "status": "covered_by_latest_product_station_window_index",
            "columns": ["forcing_version_id", "basin_version_id", "LOWER(source_id)", "variable", "valid_time DESC"],
        },
        {
            "table": "met.interp_weight",
            "index": "interp_weight_qhh_latest_membership_idx",
            "status": "covered_by_latest_product_station_membership_index",
            "columns": ["model_id", "station_id", "variable", "LOWER(source_id)"],
        },
    ]


def _non_negative_int(value: Any) -> int:
    parsed = _optional_int(value)
    if parsed is None or parsed < 0:
        return 0
    return parsed


def _optional_non_negative_response_int(value: Any) -> int | None:
    parsed = _optional_int(value)
    if parsed is None or parsed < 0:
        return None
    return parsed


def _validate_forcing_version_filter_consistency(
    forcing_version: Mapping[str, Any],
    *,
    model_id: str | None,
    source_id: str | None,
    cycle_time: datetime | str | None,
) -> None:
    conflicts: list[dict[str, Any]] = []
    model_token = str(model_id or "").strip()
    if model_token and model_token != str(forcing_version.get("model_id") or ""):
        conflicts.append(
            {
                "field": "model_id",
                "supplied": model_token,
                "selected": forcing_version.get("model_id"),
            }
        )

    source_token = _source_lookup_token(source_id)
    selected_source_token = _source_lookup_token(str(forcing_version.get("source_id") or ""))
    if source_token and source_token != selected_source_token:
        conflicts.append(
            {
                "field": "source_id",
                "supplied": source_token,
                "selected": selected_source_token,
            }
        )

    if cycle_time is not None:
        supplied_cycle_time = _required_datetime_filter(cycle_time, "cycle_time")
        selected_cycle_time = _datetime_value(forcing_version.get("cycle_time"))
        if selected_cycle_time != supplied_cycle_time:
            conflicts.append(
                {
                    "field": "cycle_time",
                    "supplied": _format_time(supplied_cycle_time),
                    "selected": _format_time(selected_cycle_time),
                }
            )

    if conflicts:
        raise ForecastStoreError(
            status_code=409,
            code="FORCING_VERSION_FILTER_CONFLICT",
            message="forcing_version_id conflicts with supplied model_id, source_id, or cycle_time.",
            details={
                "forcing_version_id": forcing_version.get("forcing_version_id"),
                "conflicts": conflicts,
            },
        )


def _validate_time_range(from_time: datetime | None, to_time: datetime | None) -> None:
    if from_time is not None and to_time is not None and from_time > to_time:
        raise ForecastStoreError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="from must be earlier than or equal to to.",
            details={"from": _format_time(from_time), "to": _format_time(to_time)},
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


def _hydro_run_response(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    product_quality = _flood_product_quality_from_row(payload)
    for key in (
        "flood_result_rows",
        "flood_return_period_rows",
        "flood_warning_rows",
        "flood_quality_max_over_window",
    ):
        payload.pop(key, None)
    payload["product_quality"] = {"flood_return_period": product_quality}
    return _json_ready(payload)


def _flood_product_quality_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result_rows = int(row.get("flood_result_rows") or 0)
    return_period_rows = int(row.get("flood_return_period_rows") or 0)
    warning_rows = int(row.get("flood_warning_rows") or 0)
    unavailable_products: list[str] = []
    residual_blockers: list[dict[str, Any]] = []
    run_id = str(row.get("run_id") or "")

    if return_period_rows <= 0:
        unavailable_products.append("return_period_result")
        residual_blockers.append(
            {
                "code": "RETURN_PERIOD_RESULT_UNAVAILABLE",
                "state": "unavailable",
                "run_id": run_id,
                "residual_risk": "No non-null peak return-period rows are available for this run.",
            }
        )
    elif result_rows > return_period_rows:
        unavailable_products.append("frequency_curves")
        residual_blockers.append(
            {
                "code": "FREQUENCY_CURVES_UNAVAILABLE",
                "state": "unavailable",
                "run_id": run_id,
                "residual_risk": "Some peak rows have null return_period because frequency curves are unavailable.",
            }
        )
    if return_period_rows > 0 and warning_rows < return_period_rows:
        unavailable_products.append("warning_thresholds")
        residual_blockers.append(
            {
                "code": "WARNING_THRESHOLDS_UNAVAILABLE",
                "state": "unavailable",
                "run_id": run_id,
                "residual_risk": "warning_level remains null for peak return-period rows.",
            }
        )

    quality_state = "ready"
    if "warning_thresholds" in unavailable_products or "return_period_result" in unavailable_products:
        quality_state = "unavailable"
    elif unavailable_products:
        quality_state = "degraded"

    return {
        "quality_state": quality_state,
        "max_over_window": bool(row.get("flood_quality_max_over_window")) if result_rows > 0 else None,
        "result_rows": result_rows,
        "return_period_rows": return_period_rows,
        "warning_rows": warning_rows,
        "unavailable_products": unavailable_products,
        "residual_blockers": residual_blockers,
    }


def _flood_product_quality_join(alias: str) -> str:
    return f"""
                LEFT JOIN (
                    SELECT run_id,
                           CASE
                               WHEN SUM(CASE WHEN max_over_window = true THEN 1 ELSE 0 END) > 0
                               THEN true
                               WHEN COUNT(*) > 0
                               THEN false
                               ELSE NULL
                           END AS quality_max_over_window,
                           CASE
                               WHEN SUM(CASE WHEN max_over_window = true THEN 1 ELSE 0 END) > 0
                               THEN SUM(CASE WHEN max_over_window = true THEN 1 ELSE 0 END)
                               ELSE COUNT(*)
                           END AS result_rows,
                           CASE
                               WHEN SUM(CASE WHEN max_over_window = true THEN 1 ELSE 0 END) > 0
                               THEN SUM(
                                   CASE
                                       WHEN max_over_window = true AND return_period IS NOT NULL THEN 1
                                       ELSE 0
                                   END
                               )
                               ELSE SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END)
                           END AS return_period_rows,
                           CASE
                               WHEN SUM(CASE WHEN max_over_window = true THEN 1 ELSE 0 END) > 0
                               THEN SUM(
                                   CASE
                                       WHEN max_over_window = true AND warning_level IS NOT NULL THEN 1
                                       ELSE 0
                                   END
                               )
                               ELSE SUM(CASE WHEN warning_level IS NOT NULL THEN 1 ELSE 0 END)
                           END AS warning_rows
                    FROM flood.return_period_result
                    GROUP BY run_id
                ) {alias} ON {alias}.run_id = h.run_id
    """


def _flood_product_quality_select(alias: str) -> str:
    return f"""
                    COALESCE({alias}.quality_max_over_window, NULL) AS flood_quality_max_over_window,
                    COALESCE({alias}.result_rows, 0) AS flood_result_rows,
                    COALESCE({alias}.return_period_rows, 0) AS flood_return_period_rows,
                    COALESCE({alias}.warning_rows, 0) AS flood_warning_rows
    """


def _flood_product_ready_sql(alias: str) -> str:
    return f"""
            COALESCE({alias}.result_rows, 0) > 0
            AND COALESCE({alias}.return_period_rows, 0) > 0
            AND COALESCE({alias}.return_period_rows, 0) = COALESCE({alias}.result_rows, 0)
            AND COALESCE({alias}.warning_rows, 0) = COALESCE({alias}.return_period_rows, 0)
    """


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
        "frequency_thresholds": None,
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
    frequency_thresholds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not rows:
        response = _empty_forecast_response(segment_id=segment_id, issue_time=issue_time)
        response["frequency_thresholds"] = frequency_thresholds
        return response

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
        "frequency_thresholds": frequency_thresholds,
    }


def _station_series_response(
    *,
    station: Mapping[str, Any],
    forcing_version: Mapping[str, Any],
    requested_variables: Sequence[str],
    requested_from: datetime | None,
    requested_to: datetime | None,
    limit: int,
    rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {
        variable: {
            "variable": variable,
            "unit": None,
            "native_resolution": None,
            "source_id": _display_source_id(str(forcing_version.get("source_id") or "")),
            "cycle_time": _format_time_value(forcing_version.get("cycle_time")),
            "points": [],
            "truncated": False,
            "metadata": _station_truncation_metadata(
                limit=limit,
                requested_from=requested_from,
                requested_to=requested_to,
                returned_points=0,
                returned_from=None,
                returned_to=None,
            ),
        }
        for variable in requested_variables
    }

    for row in rows:
        variable = str(row.get("variable"))
        series = grouped.get(variable)
        if series is None:
            continue
        row_number = int(row.get("row_number") or 0)
        if row_number > limit:
            series["truncated"] = True
            series["metadata"]["truncated"] = True
            continue
        valid_time = _datetime_value(row.get("valid_time"))
        if valid_time is None:
            continue
        if series["unit"] is None and row.get("unit") is not None:
            series["unit"] = str(row["unit"])
        if series["native_resolution"] is None and row.get("native_resolution") is not None:
            series["native_resolution"] = str(row["native_resolution"])
        point = {
            "valid_time": _format_time(valid_time),
            "value": float(row["value"]),
            "quality_flag": row.get("quality_flag"),
            "source_id": _display_source_id(str(row["source_id"])) if row.get("source_id") else None,
        }
        series["points"].append(point)

    for series in grouped.values():
        series["points"].sort(key=lambda point: str(point["valid_time"]))
        returned_from = series["points"][0]["valid_time"] if series["points"] else None
        returned_to = series["points"][-1]["valid_time"] if series["points"] else None
        metadata = _station_truncation_metadata(
            limit=limit,
            requested_from=requested_from,
            requested_to=requested_to,
            returned_points=len(series["points"]),
            returned_from=returned_from,
            returned_to=returned_to,
        )
        metadata["truncated"] = bool(series["truncated"])
        series["metadata"] = metadata

    return {
        "station_id": str(station["station_id"]),
        "station": _station_response(dict(station)),
        "forcing_version_id": str(forcing_version["forcing_version_id"]),
        "model_id": forcing_version.get("model_id"),
        "source_id": _display_source_id(str(forcing_version.get("source_id") or "")),
        "cycle_time": _format_time_value(forcing_version.get("cycle_time")),
        "valid_time_start": _format_time_value(forcing_version.get("start_time")),
        "valid_time_end": _format_time_value(forcing_version.get("end_time")),
        "limit": limit,
        "requested_from": _format_time(requested_from),
        "requested_to": _format_time(requested_to),
        "series": list(grouped.values()),
    }


def _station_series_rows_within_window(
    rows: Sequence[dict[str, Any]],
    *,
    valid_time_start: datetime,
    valid_time_end: datetime,
) -> list[dict[str, Any]]:
    by_variable: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        valid_time = _datetime_value(row.get("valid_time"))
        if valid_time is None or valid_time < valid_time_start or valid_time > valid_time_end:
            continue
        normalized_row = dict(row)
        normalized_row["valid_time"] = valid_time
        by_variable.setdefault(str(row.get("variable")), []).append(normalized_row)

    bounded: list[dict[str, Any]] = []
    for variable_rows in by_variable.values():
        variable_rows.sort(key=lambda item: item["valid_time"])
        for row_number, row in enumerate(variable_rows, start=1):
            row["row_number"] = row_number
            bounded.append(row)
    return bounded


def _station_truncation_metadata(
    *,
    limit: int,
    requested_from: datetime | None,
    requested_to: datetime | None,
    returned_points: int,
    returned_from: str | None,
    returned_to: str | None,
) -> dict[str, Any]:
    return {
        "limit": limit,
        "returned_points": returned_points,
        "requested_from": _format_time(requested_from),
        "requested_to": _format_time(requested_to),
        "returned_from": returned_from,
        "returned_to": returned_to,
        "truncated": False,
    }


def _station_forcing_readiness_response(
    *,
    forcing_version: Mapping[str, Any],
    expected_station_count: int | None,
    required_variables: Sequence[str],
    overall: Mapping[str, Any],
    coverage_rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    coverage_by_variable = {str(row.get("variable")): row for row in coverage_rows}
    actual_station_count = int(overall.get("actual_station_count") or 0)
    declared_station_count = int(forcing_version.get("station_count") or 0)
    effective_expected_station_count = (
        expected_station_count
        if expected_station_count is not None
        else declared_station_count if declared_station_count > 0 else None
    )
    missing_reasons: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []

    if effective_expected_station_count is not None and actual_station_count != effective_expected_station_count:
        missing_reasons.append(
            {
                "code": "STATION_COUNT_MISMATCH",
                "expected": effective_expected_station_count,
                "actual": actual_station_count,
            }
        )

    for variable in required_variables:
        row = coverage_by_variable.get(variable)
        if row is None:
            coverage.append(
                {
                    "variable": variable,
                    "station_count": 0,
                    "sample_count": 0,
                    "unit_count": 0,
                    "quality_flag_count": 0,
                    "missing_unit_samples": 0,
                    "missing_quality_flag_samples": 0,
                    "valid_time_start": None,
                    "valid_time_end": None,
                    "ready": False,
                }
            )
            missing_reasons.append({"code": "VARIABLE_MISSING", "variable": variable})
            if effective_expected_station_count is not None:
                missing_reasons.append(
                    {
                        "code": "VARIABLE_STATION_COUNT_MISMATCH",
                        "variable": variable,
                        "expected": effective_expected_station_count,
                        "actual": 0,
                    }
                )
            continue

        station_count = int(row.get("station_count") or 0)
        sample_count = int(row.get("sample_count") or 0)
        unit_count = int(row.get("unit_count") or 0)
        missing_unit_samples = int(row.get("missing_unit_samples") or 0)
        quality_flag_count = int(row.get("quality_flag_count") or 0)
        missing_quality_flag_samples = int(row.get("missing_quality_flag_samples") or 0)
        station_count_matches = (
            effective_expected_station_count is None or station_count == effective_expected_station_count
        )
        variable_ready = (
            station_count > 0
            and sample_count > 0
            and station_count_matches
            and unit_count > 0
            and missing_unit_samples == 0
            and quality_flag_count > 0
            and missing_quality_flag_samples == 0
        )
        coverage.append(
            {
                "variable": variable,
                "station_count": station_count,
                "sample_count": sample_count,
                "unit_count": unit_count,
                "quality_flag_count": quality_flag_count,
                "missing_unit_samples": missing_unit_samples,
                "missing_quality_flag_samples": missing_quality_flag_samples,
                "valid_time_start": _format_time_value(row.get("valid_time_start")),
                "valid_time_end": _format_time_value(row.get("valid_time_end")),
                "ready": variable_ready,
            }
        )
        if unit_count <= 0 or missing_unit_samples > 0:
            missing_reasons.append(
                {
                    "code": "UNIT_MISSING",
                    "variable": variable,
                    "missing_samples": missing_unit_samples,
                }
            )
        if quality_flag_count <= 0 or missing_quality_flag_samples > 0:
            missing_reasons.append(
                {
                    "code": "QUALITY_FLAG_MISSING",
                    "variable": variable,
                    "missing_samples": missing_quality_flag_samples,
                }
            )
        if effective_expected_station_count is not None and station_count != effective_expected_station_count:
            missing_reasons.append(
                {
                    "code": "VARIABLE_STATION_COUNT_MISMATCH",
                    "variable": variable,
                    "expected": effective_expected_station_count,
                    "actual": station_count,
                }
            )

    query_index = {
        "status": "covered_by_primary_key",
        "table": "met.forcing_station_timeseries",
        "index": "forcing_station_timeseries_pkey",
        "columns": ["forcing_version_id", "station_id", "variable", "valid_time"],
        "reason": (
            "Station-series reads constrain forcing_version_id and station_id before variable and valid_time, "
            "matching the source-of-truth primary key prefix; no additive index is required for #204."
        ),
    }
    ready = not missing_reasons and all(item["ready"] for item in coverage)
    return {
        "forcing_version_id": str(forcing_version["forcing_version_id"]),
        "model_id": forcing_version.get("model_id"),
        "source_id": _display_source_id(str(forcing_version.get("source_id") or "")),
        "cycle_time": _format_time_value(forcing_version.get("cycle_time")),
        "expected_station_count": effective_expected_station_count,
        "actual_station_count": actual_station_count,
        "declared_station_count": declared_station_count,
        "required_variables": list(required_variables),
        "six_variable_coverage": coverage,
        "sample_count": int(overall.get("sample_count") or 0),
        "valid_time_start": _format_time_value(overall.get("valid_time_start")),
        "valid_time_end": _format_time_value(overall.get("valid_time_end")),
        "missing_data_reasons": missing_reasons,
        "query_index": query_index,
        "ready": ready,
    }


def _spliced_response_from_rows(
    *,
    river_segment_id: str,
    issue_time: datetime,
    variable: str,
    analysis_rows: Sequence[dict[str, Any]],
    forecast_rows: Sequence[dict[str, Any]],
    frequency_thresholds: dict[str, Any] | None = None,
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
        "frequency_thresholds": frequency_thresholds,
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


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
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
