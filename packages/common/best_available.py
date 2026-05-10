from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol


class BestAvailableError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int = 500,
        code: str = "BEST_AVAILABLE_ERROR",
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
        raise BestAvailableError(
            code="DATABASE_URL_MISSING",
            message="DATABASE_URL is required for best-available selection operations.",
        )
    return database_url


@dataclass(frozen=True)
class ForcingInputSelection:
    valid_time: datetime
    variable: str
    selected_source: str
    source_cycle_time: datetime


@dataclass(frozen=True)
class BestAvailableSelection:
    valid_time: datetime
    variable: str
    selected_source: str
    source_cycle_time: datetime
    fallback_order: tuple[str, ...]
    quality_flag: str


class BestAvailableRepository(Protocol):
    def list_enabled_sources(self) -> tuple[str, ...]: ...

    def list_forcing_inputs(self, forcing_version_id: str) -> list[ForcingInputSelection]: ...

    def upsert_selection(self, selection: BestAvailableSelection) -> dict[str, Any]: ...

    def list_selections(
        self,
        *,
        from_time: datetime,
        to_time: datetime,
        variable: str | None,
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class BestAvailableManager:
    repository: BestAvailableRepository

    @classmethod
    def from_env(cls) -> BestAvailableManager:
        return cls(repository=PsycopgBestAvailableRepository.from_env())

    def write_forcing_version(
        self,
        forcing_version_id: str,
        *,
        now: datetime | None = None,
    ) -> list[BestAvailableSelection]:
        enabled_sources = self.repository.list_enabled_sources()
        forcing_inputs = self.repository.list_forcing_inputs(forcing_version_id)
        selections = [
            selection_from_forcing_input(row, enabled_sources=enabled_sources, now=now) for row in forcing_inputs
        ]
        for selection in selections:
            self.repository.upsert_selection(selection)
        return selections

    def list_selections(
        self,
        *,
        from_time: datetime,
        to_time: datetime,
        variable: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.repository.list_selections(
            from_time=_ensure_utc(from_time),
            to_time=_ensure_utc(to_time),
            variable=variable,
        )


@dataclass(frozen=True)
class PsycopgBestAvailableRepository:
    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgBestAvailableRepository:
        return cls(default_database_url())

    def list_enabled_sources(self) -> tuple[str, ...]:
        rows = self._fetch_all(
            """
            SELECT source_id
            FROM met.data_source
            WHERE status = 'enabled'
            ORDER BY source_id
            """,
            (),
        )
        return tuple(str(row["source_id"]).upper() for row in rows)

    def list_forcing_inputs(self, forcing_version_id: str) -> list[ForcingInputSelection]:
        rows = self._fetch_all(
            """
            SELECT DISTINCT ON (fst.valid_time, fst.variable)
                fst.valid_time,
                fst.variable,
                fst.source_id AS selected_source,
                COALESCE(cmp.cycle_time, fv.cycle_time, fst.valid_time) AS source_cycle_time
            FROM met.forcing_station_timeseries fst
            JOIN met.forcing_version fv
              ON fv.forcing_version_id = fst.forcing_version_id
            LEFT JOIN met.forcing_version_component fvc
              ON fvc.forcing_version_id = fst.forcing_version_id
             AND fvc.variable = fst.variable
             AND (fvc.valid_time_start IS NULL OR fst.valid_time >= fvc.valid_time_start)
             AND (fvc.valid_time_end IS NULL OR fst.valid_time <= fvc.valid_time_end)
            LEFT JOIN met.canonical_met_product cmp
              ON cmp.canonical_product_id = fvc.canonical_product_id
            WHERE fst.forcing_version_id = %s
            ORDER BY
                fst.valid_time,
                fst.variable,
                CASE UPPER(fst.source_id)
                    WHEN 'ERA5' THEN 100
                    WHEN 'CLDAS' THEN 90
                    WHEN 'GFS' THEN 10
                    ELSE 0
                END DESC
            """,
            (forcing_version_id,),
        )
        return [
            ForcingInputSelection(
                valid_time=_ensure_utc(row["valid_time"]),
                variable=str(row["variable"]),
                selected_source=str(row["selected_source"]).upper(),
                source_cycle_time=_ensure_utc(row["source_cycle_time"]),
            )
            for row in rows
        ]

    def upsert_selection(self, selection: BestAvailableSelection) -> dict[str, Any]:
        valid_time = _ensure_utc(selection.valid_time)
        rows = self._fetch_all(
            """
            INSERT INTO met.best_available_selection (
                valid_time,
                variable,
                selected_source,
                source_cycle_time,
                fallback_order,
                quality_flag
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (valid_time, variable) DO UPDATE SET
                selected_source = EXCLUDED.selected_source,
                source_cycle_time = EXCLUDED.source_cycle_time,
                fallback_order = EXCLUDED.fallback_order,
                quality_flag = EXCLUDED.quality_flag
            WHERE
                CASE UPPER(EXCLUDED.selected_source)
                    WHEN 'ERA5' THEN 100
                    WHEN 'CLDAS' THEN 90
                    WHEN 'GFS' THEN 10
                    ELSE 0
                END >= CASE UPPER(met.best_available_selection.selected_source)
                    WHEN 'ERA5' THEN 100
                    WHEN 'CLDAS' THEN 90
                    WHEN 'GFS' THEN 10
                    ELSE 0
                END
            RETURNING *
            """,
            (
                valid_time,
                selection.variable,
                selection.selected_source,
                _ensure_utc(selection.source_cycle_time),
                list(selection.fallback_order),
                selection.quality_flag,
            ),
        )
        if rows:
            return rows[0]
        return self._fetch_one(
            """
            SELECT *
            FROM met.best_available_selection
            WHERE valid_time = %s
              AND variable = %s
            """,
            (valid_time, selection.variable),
        )

    def list_selections(
        self,
        *,
        from_time: datetime,
        to_time: datetime,
        variable: str | None,
    ) -> list[dict[str, Any]]:
        filters = ["valid_time >= %s", "valid_time <= %s"]
        parameters: list[Any] = [_ensure_utc(from_time), _ensure_utc(to_time)]
        if variable is not None:
            filters.append("variable = %s")
            parameters.append(variable)
        rows = self._fetch_all(
            f"""
            SELECT
                valid_time,
                variable,
                selected_source,
                source_cycle_time,
                fallback_order,
                quality_flag
            FROM met.best_available_selection
            WHERE {" AND ".join(filters)}
            ORDER BY valid_time, variable
            """,
            tuple(parameters),
        )
        return [_selection_row_response(row) for row in rows]

    def _fetch_one(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any]:
        rows = self._fetch_all(statement, parameters)
        if not rows:
            raise BestAvailableError(message="Best-available database operation did not return a row.")
        return rows[0]

    def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            import psycopg2
        except ImportError as error:
            raise BestAvailableError(code="PSYCOPG2_MISSING", message="psycopg2 is required.") from error

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
            raise BestAvailableError(
                code="DATABASE_ERROR",
                message=f"Best-available DB operation failed: {error}",
            ) from error
        finally:
            if connection is not None:
                connection.close()


def selection_from_forcing_input(
    row: ForcingInputSelection,
    *,
    enabled_sources: Sequence[str],
    now: datetime | None = None,
) -> BestAvailableSelection:
    selected_source = row.selected_source.upper()
    fallback_order = (
        ["ERA5"]
        if selected_source == "ERA5"
        else fallback_order_for_valid_time(
            row.valid_time,
            now=now,
            enabled_sources=enabled_sources,
            selected_source=selected_source,
        )
    )
    return BestAvailableSelection(
        valid_time=_ensure_utc(row.valid_time),
        variable=row.variable,
        selected_source=selected_source,
        source_cycle_time=_ensure_utc(row.source_cycle_time),
        fallback_order=tuple(fallback_order),
        quality_flag=quality_flag_for_source(selected_source),
    )


def fallback_order_for_valid_time(
    valid_time: datetime,
    *,
    now: datetime | None = None,
    enabled_sources: Sequence[str] | None = None,
    selected_source: str | None = None,
) -> list[str]:
    reference_time = _ensure_utc(now or datetime.now(tz=UTC))
    valid = _ensure_utc(valid_time)
    base_order = ["CLDAS", "ERA5", "GFS"] if valid >= reference_time - timedelta(days=5) else ["ERA5"]

    if enabled_sources is None:
        order = list(base_order)
    else:
        enabled = {source.upper() for source in enabled_sources}
        order = [source for source in base_order if source in enabled]

    if selected_source is not None:
        selected = selected_source.upper()
        if selected not in order:
            order.append(selected)
    return order


def quality_flag_for_source(source_id: str) -> str:
    return "best_available_realtime" if source_id.upper() == "ERA5" else "best_available_degraded"


def source_priority(source_id: str) -> int:
    priorities = {"ERA5": 100, "CLDAS": 90, "GFS": 10}
    return priorities.get(source_id.upper(), 0)


def _selection_row_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "valid_time": _format_time(row["valid_time"]),
        "variable": row["variable"],
        "selected_source": row["selected_source"],
        "source_cycle_time": _format_time(row["source_cycle_time"]),
        "fallback_order": list(row["fallback_order"]),
        "quality_flag": row["quality_flag"],
    }


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")
