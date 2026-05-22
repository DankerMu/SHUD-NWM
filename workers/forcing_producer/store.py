from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from packages.common.met_store import MetStoreError, default_database_url

from .producer import (
    CanonicalProduct,
    ForcingComponent,
    ForcingTimeseriesRow,
    InterpolationWeight,
    MetStation,
)


@dataclass(frozen=True)
class PsycopgForcingRepository:
    """Postgres repository for the SHUD forcing producer."""

    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgForcingRepository:
        return cls(default_database_url())

    def resolve_model_basin_version(self, *, model_id: str) -> str:
        row = self._fetch_optional(
            """
            SELECT basin_version_id
            FROM core.model_instance
            WHERE model_id = %s
            """,
            (model_id,),
        )
        if row is None:
            raise MetStoreError(f"Model instance {model_id!r} was not found.")
        return str(row["basin_version_id"])

    def load_met_stations(self, *, basin_version_id: str) -> tuple[MetStation, ...]:
        rows = self._fetch_all(
            """
            SELECT
                station_id,
                basin_version_id,
                station_name,
                ST_X(geom) AS longitude,
                ST_Y(geom) AS latitude,
                elevation_m,
                station_role,
                properties_json
            FROM met.met_station
            WHERE basin_version_id = %s
              AND active_flag = true
            ORDER BY station_id
            """,
            (basin_version_id,),
        )
        return tuple(
            MetStation(
                station_id=str(row["station_id"]),
                basin_version_id=str(row["basin_version_id"]),
                station_name=row.get("station_name"),
                longitude=float(row["longitude"]),
                latitude=float(row["latitude"]),
                elevation_m=float(row["elevation_m"]),
                station_role=str(row["station_role"]),
                properties_json=row.get("properties_json") or {},
            )
            for row in rows
            if row["elevation_m"] is not None
        )

    def list_canonical_products(self, *, source_id: str, cycle_time: datetime) -> tuple[CanonicalProduct, ...]:
        rows = self._fetch_all(
            """
            SELECT
                canonical_product_id,
                source_id,
                cycle_time,
                valid_time,
                lead_time_hours,
                variable,
                unit,
                grid_id,
                grid_definition_uri,
                native_time_resolution,
                native_spatial_resolution,
                object_uri,
                checksum,
                quality_flag
            FROM met.canonical_met_product
            WHERE source_id = %s
              AND cycle_time = %s
            ORDER BY variable, valid_time, canonical_product_id
            """,
            (source_id, cycle_time),
        )
        return tuple(
            CanonicalProduct(
                canonical_product_id=str(row["canonical_product_id"]),
                source_id=str(row["source_id"]),
                cycle_time=row["cycle_time"],
                valid_time=row["valid_time"],
                lead_time_hours=row.get("lead_time_hours"),
                variable=str(row["variable"]),
                unit=str(row["unit"]),
                grid_id=str(row["grid_id"]),
                grid_definition_uri=row.get("grid_definition_uri"),
                native_time_resolution=row.get("native_time_resolution"),
                native_spatial_resolution=row.get("native_spatial_resolution"),
                object_uri=str(row["object_uri"]),
                checksum=str(row["checksum"] or ""),
                quality_flag=str(row.get("quality_flag") or "ok"),
            )
            for row in rows
        )

    def list_fallback_canonical_products(
        self,
        *,
        source_id: str,
        start_time: datetime,
        end_time: datetime,
        variables: Sequence[str],
    ) -> tuple[CanonicalProduct, ...]:
        if not variables:
            return ()
        rows = self._fetch_all(
            """
            WITH ranked AS (
                SELECT
                    cmp.canonical_product_id,
                    cmp.source_id,
                    cmp.cycle_time,
                    cmp.valid_time,
                    cmp.lead_time_hours,
                    cmp.variable,
                    cmp.unit,
                    cmp.grid_id,
                    cmp.grid_definition_uri,
                    cmp.native_time_resolution,
                    cmp.native_spatial_resolution,
                    cmp.object_uri,
                    cmp.checksum,
                    cmp.quality_flag,
                    ROW_NUMBER() OVER (
                        PARTITION BY cmp.valid_time, cmp.variable
                        ORDER BY cmp.lead_time_hours ASC NULLS LAST, cmp.cycle_time DESC, cmp.canonical_product_id
                    ) AS rank
                FROM met.canonical_met_product cmp
                JOIN met.forecast_cycle fc
                  ON fc.source_id = cmp.source_id
                 AND fc.cycle_time = cmp.cycle_time
                WHERE cmp.source_id = %s
                  AND fc.status = 'canonical_ready'
                  AND cmp.valid_time >= %s
                  AND cmp.valid_time <= %s
                  AND cmp.variable = ANY(%s)
                  AND cmp.quality_flag <> 'fail'
                  AND cmp.checksum <> ''
            )
            SELECT
                canonical_product_id,
                source_id,
                cycle_time,
                valid_time,
                lead_time_hours,
                variable,
                unit,
                grid_id,
                grid_definition_uri,
                native_time_resolution,
                native_spatial_resolution,
                object_uri,
                checksum,
                quality_flag
            FROM ranked
            WHERE rank = 1
            ORDER BY variable, valid_time, canonical_product_id
            """,
            (source_id, start_time, end_time, list(variables)),
        )
        return tuple(
            CanonicalProduct(
                canonical_product_id=str(row["canonical_product_id"]),
                source_id=str(row["source_id"]),
                cycle_time=row["cycle_time"],
                valid_time=row["valid_time"],
                lead_time_hours=row.get("lead_time_hours"),
                variable=str(row["variable"]),
                unit=str(row["unit"]),
                grid_id=str(row["grid_id"]),
                grid_definition_uri=row.get("grid_definition_uri"),
                native_time_resolution=row.get("native_time_resolution"),
                native_spatial_resolution=row.get("native_spatial_resolution"),
                object_uri=str(row["object_uri"]),
                checksum=str(row["checksum"] or ""),
                quality_flag=str(row.get("quality_flag") or "ok"),
            )
            for row in rows
        )

    def load_interp_weights(
        self,
        *,
        source_id: str,
        grid_id: str,
        model_id: str,
    ) -> tuple[InterpolationWeight, ...]:
        rows = self._fetch_all(
            """
            SELECT source_id,
                   grid_id,
                   model_id,
                   station_id,
                   variable,
                   grid_cell_id,
                   weight,
                   method,
                   grid_signature
            FROM met.interp_weight
            WHERE source_id = %s
              AND grid_id = %s
              AND model_id = %s
            ORDER BY station_id, variable, grid_cell_id
            """,
            (source_id, grid_id, model_id),
        )
        return tuple(
            InterpolationWeight(
                source_id=str(row["source_id"]),
                grid_id=str(row["grid_id"]),
                model_id=str(row["model_id"]),
                station_id=str(row["station_id"]),
                variable=str(row["variable"]),
                grid_cell_id=str(row["grid_cell_id"]),
                weight=float(row["weight"]),
                method=str(row["method"]),
                grid_signature=row.get("grid_signature"),
            )
            for row in rows
        )

    def upsert_interp_weights(self, weights: Sequence[InterpolationWeight]) -> None:
        if not weights:
            return
        scopes = {(weight.source_id, weight.grid_id, weight.model_id) for weight in weights}
        if len(scopes) != 1:
            raise MetStoreError("Interpolation weights must be replaced one source/grid/model scope at a time.")
        source_id, grid_id, model_id = next(iter(scopes))
        rows = [
            (
                weight.source_id,
                weight.grid_id,
                weight.model_id,
                weight.station_id,
                weight.variable,
                weight.grid_cell_id,
                weight.weight,
                weight.method,
                weight.grid_signature,
            )
            for weight in weights
        ]
        self._replace_values(
            """
            DELETE FROM met.interp_weight
            WHERE source_id = %s
              AND grid_id = %s
              AND model_id = %s
            """,
            (source_id, grid_id, model_id),
            """
            INSERT INTO met.interp_weight (
                source_id, grid_id, model_id, station_id, variable, grid_cell_id, weight, method, grid_signature
            )
            VALUES %s
            ON CONFLICT (source_id, grid_id, model_id, station_id, variable, grid_cell_id)
            DO UPDATE SET
                weight = EXCLUDED.weight,
                method = EXCLUDED.method,
                grid_signature = EXCLUDED.grid_signature
            """,
            rows,
        )

    def get_forcing_version(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        model_id: str,
    ) -> dict[str, Any] | None:
        return self._fetch_optional(
            """
            SELECT *
            FROM met.forcing_version
            WHERE source_id = %s
              AND cycle_time = %s
              AND model_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (source_id, cycle_time, model_id),
        )

    def upsert_forcing_version(self, record: Mapping[str, Any]) -> dict[str, Any]:
        try:
            from psycopg2.extras import Json
        except ImportError as error:
            raise MetStoreError("psycopg2 is required for forcing database operations.") from error

        return self._fetch_one(
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
                model_id = EXCLUDED.model_id,
                source_id = EXCLUDED.source_id,
                cycle_time = EXCLUDED.cycle_time,
                start_time = EXCLUDED.start_time,
                end_time = EXCLUDED.end_time,
                station_count = EXCLUDED.station_count,
                forcing_package_uri = EXCLUDED.forcing_package_uri,
                checksum = EXCLUDED.checksum,
                lineage_json = EXCLUDED.lineage_json
            RETURNING *
            """,
            (
                record["forcing_version_id"],
                record["model_id"],
                record["source_id"],
                record["cycle_time"],
                record["start_time"],
                record["end_time"],
                record["station_count"],
                record["forcing_package_uri"],
                record["checksum"],
                Json(dict(record.get("lineage_json") or {})),
            ),
        )

    def finalize_forcing_version(self, forcing_version_id: str, checksum: str) -> dict[str, Any]:
        return self._fetch_one(
            """
            UPDATE met.forcing_version
            SET checksum = %s
            WHERE forcing_version_id = %s
            RETURNING *
            """,
            (checksum, forcing_version_id),
        )

    def replace_forcing_components(self, forcing_version_id: str, components: Sequence[ForcingComponent]) -> None:
        rows = [
            (
                component.forcing_version_id,
                component.canonical_product_id,
                component.variable,
                component.valid_time_start,
                component.valid_time_end,
                component.role,
            )
            for component in components
        ]
        self._replace_values(
            "DELETE FROM met.forcing_version_component WHERE forcing_version_id = %s",
            (forcing_version_id,),
            """
            INSERT INTO met.forcing_version_component (
                forcing_version_id, canonical_product_id, variable, valid_time_start, valid_time_end, role
            )
            VALUES %s
            """,
            rows,
        )

    def replace_forcing_timeseries(
        self,
        forcing_version_id: str,
        rows: Sequence[ForcingTimeseriesRow],
    ) -> None:
        value_rows = [
            (
                row.forcing_version_id,
                row.basin_version_id,
                row.station_id,
                row.valid_time,
                row.source_id,
                row.variable,
                row.value,
                row.unit,
                row.native_resolution,
                row.quality_flag,
            )
            for row in rows
        ]
        self._replace_values(
            "DELETE FROM met.forcing_station_timeseries WHERE forcing_version_id = %s",
            (forcing_version_id,),
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
            value_rows,
        )

    def update_forecast_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        assignments: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("status", status),
            ("error_code", error_code),
            ("error_message", error_message),
        ):
            if value is not None:
                assignments.append(f"{column} = %s")
                parameters.append(value)
        if not assignments:
            return None
        parameters.extend([source_id, cycle_time])
        return self._fetch_optional(
            f"""
            UPDATE met.forecast_cycle
            SET {", ".join(assignments)}
            WHERE source_id = %s
              AND cycle_time = %s
            RETURNING *
            """,
            tuple(parameters),
        )

    def _fetch_one(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any]:
        row = self._fetch_optional(statement, parameters)
        if row is None:
            raise MetStoreError("Forcing database operation did not return a row.")
        return row

    def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
        rows = self._fetch_all(statement, parameters)
        return rows[0] if rows else None

    def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            import psycopg2
        except ImportError as error:
            raise MetStoreError("psycopg2 is required for forcing database operations.") from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                if cursor.description is None:
                    connection.commit()
                    return []
                rows = cursor.fetchall()
                columns = [description.name for description in cursor.description]
                connection.commit()
                return [dict(zip(columns, row, strict=True)) for row in rows]
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise MetStoreError(f"Forcing database operation failed: {error}") from error
        finally:
            if connection is not None:
                connection.close()

    def _replace_values(
        self,
        delete_statement: str | None,
        delete_parameters: tuple[Any, ...],
        insert_statement: str,
        rows: Sequence[tuple[Any, ...]],
    ) -> None:
        try:
            import psycopg2
            from psycopg2.extras import execute_values
        except ImportError as error:
            raise MetStoreError("psycopg2 is required for forcing database operations.") from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            with connection.cursor() as cursor:
                if delete_statement is not None:
                    cursor.execute(delete_statement, delete_parameters)
                if rows:
                    execute_values(cursor, insert_statement, rows, page_size=5000)
            connection.commit()
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise MetStoreError(f"Forcing database operation failed: {error}") from error
        finally:
            if connection is not None:
                connection.close()
