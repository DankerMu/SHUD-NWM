from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class FloodRunProductQuality:
    run_id: str
    result_rows: int
    max_result_rows: int
    return_period_rows: int
    warning_rows: int
    max_return_period_rows: int
    max_warning_rows: int
    refreshed_at: datetime


def refresh_run_product_quality(session: Session, run_id: str) -> FloodRunProductQuality | None:
    """Refresh one run's materialized flood return-period quality from source rows.

    The upsert is intentionally scoped to one ``run_id``. If the run has no
    return-period rows, any stale materialized row is deleted so readiness fails
    closed after reruns or failed processing.
    """
    quality = _quality_for_run(session, run_id)
    if quality is None:
        _delete_quality_rows(session, [run_id])
        return None
    _upsert_quality_rows(session, [quality])
    return quality


def refresh_run_product_quality_many(session: Session, run_ids: Sequence[str]) -> list[FloodRunProductQuality]:
    """Refresh several runs idempotently, deleting stale quality for empty runs."""
    ordered_run_ids = _dedupe_run_ids(run_ids)
    if not ordered_run_ids:
        return []
    qualities = _quality_for_runs(session, ordered_run_ids)
    quality_run_ids = {quality.run_id for quality in qualities}
    stale_run_ids = [run_id for run_id in ordered_run_ids if run_id not in quality_run_ids]
    if stale_run_ids:
        _delete_quality_rows(session, stale_run_ids)
    if qualities:
        _upsert_quality_rows(session, qualities)
    return qualities


def backfill_run_product_quality(
    session: Session,
    run_ids: Sequence[str] | None = None,
) -> list[FloodRunProductQuality]:
    """Backfill materialized quality for existing source result rows.

    ``run_ids=None`` discovers runs from ``flood.return_period_result`` and
    refreshes exactly those runs. Supplying run ids also clears stale rows for
    runs that no longer have source rows.
    """
    if run_ids is not None:
        return refresh_run_product_quality_many(session, _dedupe_run_ids(run_ids))
    target_run_ids = _source_run_ids(session)
    qualities = refresh_run_product_quality_many(session, target_run_ids)
    _delete_orphan_quality_rows(session)
    return qualities


def clear_run_product_quality(session: Session, run_id: str) -> None:
    _delete_quality_rows(session, [run_id])


def _quality_for_run(session: Session, run_id: str) -> FloodRunProductQuality | None:
    qualities = _quality_for_runs(session, [run_id])
    return qualities[0] if qualities else None


def _quality_for_runs(session: Session, run_ids: Sequence[str]) -> list[FloodRunProductQuality]:
    if not run_ids:
        return []
    placeholders, params = _run_id_placeholders(run_ids)
    rows = session.execute(
        text(
            f"""
            SELECT
                run_id,
                COUNT(*) AS result_rows,
                SUM(CASE WHEN max_over_window = true THEN 1 ELSE 0 END) AS max_result_rows,
                SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) AS return_period_rows,
                SUM(CASE WHEN warning_level IS NOT NULL THEN 1 ELSE 0 END) AS warning_rows,
                SUM(CASE WHEN max_over_window = true AND return_period IS NOT NULL THEN 1 ELSE 0 END)
                    AS max_return_period_rows,
                SUM(CASE WHEN max_over_window = true AND warning_level IS NOT NULL THEN 1 ELSE 0 END)
                    AS max_warning_rows
            FROM flood.return_period_result
            WHERE run_id IN ({placeholders})
            GROUP BY run_id
            """
        ),
        params,
    ).mappings()
    refreshed_at = datetime.now(UTC)
    return [
        FloodRunProductQuality(
            run_id=str(row["run_id"]),
            result_rows=int(row["result_rows"] or 0),
            max_result_rows=int(row["max_result_rows"] or 0),
            return_period_rows=int(row["return_period_rows"] or 0),
            warning_rows=int(row["warning_rows"] or 0),
            max_return_period_rows=int(row["max_return_period_rows"] or 0),
            max_warning_rows=int(row["max_warning_rows"] or 0),
            refreshed_at=refreshed_at,
        )
        for row in rows
    ]


def _source_run_ids(session: Session) -> list[str]:
    rows = session.execute(
        text(
            """
            SELECT DISTINCT run_id
            FROM flood.return_period_result
            ORDER BY run_id
            """
        )
    ).mappings()
    return [str(row["run_id"]) for row in rows]


def _upsert_quality_rows(session: Session, qualities: Sequence[FloodRunProductQuality]) -> None:
    if not qualities:
        return
    columns = (
        "run_id",
        "result_rows",
        "max_result_rows",
        "return_period_rows",
        "warning_rows",
        "max_return_period_rows",
        "max_warning_rows",
        "refreshed_at",
    )
    update_columns = columns[1:]
    for quality in qualities:
        params = {column: getattr(quality, column) for column in columns}
        if _dialect_name(session) == "sqlite":
            statement = f"""
                INSERT INTO flood.run_product_quality ({', '.join(columns)})
                VALUES ({', '.join(f':{column}' for column in columns)})
                ON CONFLICT (run_id) DO UPDATE SET
                    {', '.join(f'{column} = excluded.{column}' for column in update_columns)}
            """
        else:
            statement = f"""
                INSERT INTO flood.run_product_quality ({', '.join(columns)})
                VALUES ({', '.join(f':{column}' for column in columns)})
                ON CONFLICT (run_id) DO UPDATE SET
                    {', '.join(f'{column} = EXCLUDED.{column}' for column in update_columns)}
            """
        session.execute(text(statement), params)


def _delete_quality_rows(session: Session, run_ids: Sequence[str]) -> None:
    if not run_ids:
        return
    if not _table_exists(session, "flood", "run_product_quality"):
        return
    placeholders, params = _run_id_placeholders(run_ids)
    session.execute(
        text(f"DELETE FROM flood.run_product_quality WHERE run_id IN ({placeholders})"),
        params,
    )


def _delete_orphan_quality_rows(session: Session) -> None:
    if not _table_exists(session, "flood", "run_product_quality"):
        return
    session.execute(
        text(
            """
            DELETE FROM flood.run_product_quality
            WHERE run_id NOT IN (
                SELECT DISTINCT run_id
                FROM flood.return_period_result
            )
            """
        )
    )


def _run_id_placeholders(run_ids: Sequence[str]) -> tuple[str, dict[str, str]]:
    params = {f"run_id_{index}": str(run_id) for index, run_id in enumerate(run_ids)}
    return ", ".join(f":run_id_{index}" for index in range(len(run_ids))), params


def _dedupe_run_ids(run_ids: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for run_id in run_ids:
        token = str(run_id).strip()
        if token and token not in seen:
            ordered.append(token)
            seen.add(token)
    return ordered


def _table_exists(session: Session, schema: str, table_name: str) -> bool:
    if _dialect_name(session) == "sqlite":
        try:
            row = session.execute(
                text(f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = :table_name LIMIT 1"),
                {"table_name": table_name},
            ).first()
            return row is not None
        except SQLAlchemyError:
            return False
    try:
        return inspect(session.get_bind()).has_table(table_name, schema=schema)
    except SQLAlchemyError:
        return False


def _dialect_name(session: Session) -> str:
    return str(getattr(session.get_bind().dialect, "name", ""))
