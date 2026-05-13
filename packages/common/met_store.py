from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from packages.common.source_identity import normalize_source_id


class MetStoreError(RuntimeError):
    """Raised when a met-schema database operation fails."""


def default_database_url() -> str:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise MetStoreError("DATABASE_URL is required for met database operations.")
    return database_url


@dataclass(frozen=True)
class PsycopgMetStore:
    """Small repository for M1 met-schema writes."""

    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgMetStore:
        return cls(default_database_url())

    def ensure_data_source(
        self,
        *,
        source_id: str,
        source_name: str,
        source_type: str,
        status: str,
        native_format: str,
        adapter_name: str,
        config_json: Mapping[str, Any] | None = None,
        license_status: str | None = None,
    ) -> dict[str, Any]:
        source_id = normalize_source_id(source_id)
        try:
            from psycopg2.extras import Json
        except ImportError as error:
            raise MetStoreError("psycopg2 is required for met database operations.") from error

        return self._fetch_one(
            """
            INSERT INTO met.data_source (
                source_id,
                source_name,
                source_type,
                status,
                native_format,
                license_status,
                adapter_name,
                config_json
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_id) DO UPDATE SET
                source_name = EXCLUDED.source_name,
                source_type = EXCLUDED.source_type,
                status = EXCLUDED.status,
                native_format = EXCLUDED.native_format,
                license_status = EXCLUDED.license_status,
                adapter_name = EXCLUDED.adapter_name,
                config_json = EXCLUDED.config_json
            RETURNING *
            """,
            (
                source_id,
                source_name,
                source_type,
                status,
                native_format,
                license_status,
                adapter_name,
                Json(dict(config_json or {})),
            ),
        )

    def upsert_forecast_cycle(
        self,
        *,
        cycle_id: str,
        source_id: str,
        cycle_time: datetime,
        status: str,
        issue_time: datetime | None = None,
        manifest_uri: str | None = None,
        retry_count: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        source_id = normalize_source_id(source_id)
        return self._fetch_one(
            """
            INSERT INTO met.forecast_cycle (
                cycle_id,
                source_id,
                cycle_time,
                issue_time,
                status,
                manifest_uri,
                retry_count,
                error_code,
                error_message
            )
            VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s, 0), %s, %s)
            ON CONFLICT (source_id, cycle_time) DO UPDATE SET
                issue_time = COALESCE(EXCLUDED.issue_time, met.forecast_cycle.issue_time),
                status = CASE
                    WHEN met.forecast_cycle.status IN (
                        'raw_complete',
                        'canonical_ready',
                        'forcing_ready_partial',
                        'forcing_ready',
                        'forecast_running',
                        'parsed_partial',
                        'complete',
                        'published'
                    )
                    THEN met.forecast_cycle.status
                    ELSE EXCLUDED.status
                END,
                manifest_uri = COALESCE(EXCLUDED.manifest_uri, met.forecast_cycle.manifest_uri),
                retry_count = COALESCE(%s, met.forecast_cycle.retry_count),
                error_code = EXCLUDED.error_code,
                error_message = EXCLUDED.error_message
            RETURNING *
            """,
            (
                cycle_id,
                source_id,
                cycle_time,
                issue_time,
                status,
                manifest_uri,
                retry_count,
                error_code,
                error_message,
                retry_count,
            ),
        )

    def update_forecast_cycle(
        self,
        *,
        source_id: str,
        cycle_time: datetime,
        status: str | None = None,
        manifest_uri: str | None = None,
        retry_count: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any] | None:
        source_id = normalize_source_id(source_id)
        assignments: list[str] = []
        parameters: list[Any] = []
        for column, value in (
            ("status", status),
            ("manifest_uri", manifest_uri),
            ("retry_count", retry_count),
            ("error_code", error_code),
            ("error_message", error_message),
        ):
            if value is not None:
                assignments.append(f"{column} = %s")
                parameters.append(value)

        if not assignments:
            return self.get_forecast_cycle(source_id=source_id, cycle_time=cycle_time)

        parameters.extend([source_id, cycle_time])
        return self._fetch_one(
            f"""
            UPDATE met.forecast_cycle
            SET {", ".join(assignments)}
            WHERE source_id = %s AND cycle_time = %s
            RETURNING *
            """,
            tuple(parameters),
        )

    def get_forecast_cycle(self, *, source_id: str, cycle_time: datetime) -> dict[str, Any] | None:
        source_id = normalize_source_id(source_id)
        return self._fetch_optional(
            """
            SELECT *
            FROM met.forecast_cycle
            WHERE source_id = %s AND cycle_time = %s
            """,
            (source_id, cycle_time),
        )

    def get_canonical_product(self, *, canonical_product_id: str) -> dict[str, Any] | None:
        return self._fetch_optional(
            """
            SELECT *
            FROM met.canonical_met_product
            WHERE canonical_product_id = %s
            """,
            (canonical_product_id,),
        )

    def upsert_canonical_product(self, record: Mapping[str, Any]) -> dict[str, Any]:
        try:
            from psycopg2.extras import Json
        except ImportError as error:
            raise MetStoreError("psycopg2 is required for met database operations.") from error

        lineage_json = record.get("lineage_json")
        return self._fetch_one(
            """
            INSERT INTO met.canonical_met_product (
                canonical_product_id,
                source_id,
                source_version,
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
                quality_flag,
                lineage_json
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (canonical_product_id) DO UPDATE SET
                source_version = EXCLUDED.source_version,
                cycle_time = EXCLUDED.cycle_time,
                valid_time = EXCLUDED.valid_time,
                lead_time_hours = EXCLUDED.lead_time_hours,
                variable = EXCLUDED.variable,
                unit = EXCLUDED.unit,
                grid_id = EXCLUDED.grid_id,
                grid_definition_uri = EXCLUDED.grid_definition_uri,
                native_time_resolution = EXCLUDED.native_time_resolution,
                native_spatial_resolution = EXCLUDED.native_spatial_resolution,
                object_uri = EXCLUDED.object_uri,
                checksum = EXCLUDED.checksum,
                quality_flag = EXCLUDED.quality_flag,
                lineage_json = EXCLUDED.lineage_json
            RETURNING *
            """,
            (
                record["canonical_product_id"],
                record["source_id"],
                record.get("source_version"),
                record["cycle_time"],
                record["valid_time"],
                record["lead_time_hours"],
                record["variable"],
                record["unit"],
                record["grid_id"],
                record.get("grid_definition_uri"),
                record.get("native_time_resolution"),
                record.get("native_spatial_resolution"),
                record["object_uri"],
                record["checksum"],
                record.get("quality_flag", "ok"),
                Json(dict(lineage_json or {})),
            ),
        )

    def _fetch_one(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any]:
        row = self._fetch_optional(statement, parameters)
        if row is None:
            raise MetStoreError("Database operation did not return a row.")
        return row

    def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
        try:
            import psycopg2
        except ImportError as error:
            raise MetStoreError("psycopg2 is required for met database operations.") from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                if cursor.description is None:
                    connection.commit()
                    return None
                row = cursor.fetchone()
                connection.commit()
                if row is None:
                    return None
                columns = [description.name for description in cursor.description]
                return dict(zip(columns, row, strict=True))
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise MetStoreError(f"Met database operation failed: {error}") from error
        finally:
            if connection is not None:
                connection.close()
