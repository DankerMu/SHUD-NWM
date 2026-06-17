from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, overload

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

QUALITY_STATES = {"ready", "degraded", "unavailable"}
QUALITY_SOURCES = {"historical_backfill", "explicit"}
JSON_COLUMNS = {"unavailable_products", "residual_blockers"}
EXPLICIT_QUALITY_COLUMNS: tuple[str, ...] = (
    "quality_state",
    "quality_source",
    "unavailable_products",
    "residual_blockers",
    "expected_result_rows",
    "expected_max_result_rows",
    "expected_timestep_result_rows",
    "meaningful_result_rows",
    "meaningful_max_result_rows",
    "meaningful_timestep_result_rows",
    "no_frequency_curve_rows",
    "no_usable_frequency_curve_rows",
    "warning_threshold_unavailable_rows",
)
MAX_RESIDUAL_BLOCKERS = 20
MAX_BLOCKER_SAMPLE_SEGMENT_IDS = 10
MAX_BLOCKER_STRING_LENGTH = 512
BLOCKER_REQUIRED_FIELDS: tuple[str, ...] = (
    "code",
    "state",
    "quality_flag",
    "residual_risk",
    "run_id",
)
BLOCKER_ALLOWED_FIELDS = frozenset(
    (
        *BLOCKER_REQUIRED_FIELDS,
        "model_id",
        "river_network_version_id",
        "count",
        "sample_segment_ids",
        "omitted_count",
    )
)


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
    quality_state: str = "ready"
    quality_source: str = "historical_backfill"
    unavailable_products: tuple[str, ...] = ()
    residual_blockers: tuple[dict[str, Any], ...] = ()
    expected_result_rows: int = 0
    expected_max_result_rows: int = 0
    expected_timestep_result_rows: int = 0
    meaningful_result_rows: int = 0
    meaningful_max_result_rows: int = 0
    meaningful_timestep_result_rows: int = 0
    no_frequency_curve_rows: int = 0
    no_usable_frequency_curve_rows: int = 0
    warning_threshold_unavailable_rows: int = 0


@dataclass(frozen=True)
class ExplicitFloodRunProductQuality:
    run_id: str
    quality_state: str
    unavailable_products: Sequence[str] = ()
    residual_blockers: Sequence[Mapping[str, Any]] = ()
    result_rows: int = 0
    max_result_rows: int = 0
    return_period_rows: int = 0
    warning_rows: int = 0
    max_return_period_rows: int = 0
    max_warning_rows: int = 0
    expected_result_rows: int = 0
    expected_max_result_rows: int = 0
    expected_timestep_result_rows: int = 0
    meaningful_result_rows: int = 0
    meaningful_max_result_rows: int = 0
    meaningful_timestep_result_rows: int = 0
    no_frequency_curve_rows: int = 0
    no_usable_frequency_curve_rows: int = 0
    warning_threshold_unavailable_rows: int = 0
    refreshed_at: datetime | None = None


@dataclass(frozen=True)
class FloodRunProductQualityReadiness:
    run_id: str
    quality_state: str
    unavailable_products: tuple[str, ...]
    residual_blockers: tuple[dict[str, Any], ...]
    q_down_available: bool = True


@dataclass(frozen=True)
class FloodRunProductQualityBackfillSummary:
    refreshed_runs: int
    orphan_quality_rows_deleted: int


def refresh_run_product_quality(session: Session, run_id: str) -> FloodRunProductQuality | None:
    """Refresh one run's materialized flood return-period quality from source rows.

    The upsert is intentionally scoped to one ``run_id``. If the run has no
    return-period rows, stale historical materializations are deleted so
    readiness fails closed after reruns or failed processing. Explicit quality
    rows are preserved because they are the run-level source of truth for
    unavailable products.
    """
    quality = _quality_for_run(session, run_id)
    if quality is None:
        _delete_historical_quality_rows(session, [run_id])
        return get_run_product_quality(session, run_id)
    _upsert_quality_rows(session, [quality])
    return get_run_product_quality(session, run_id)


def refresh_run_product_quality_many(session: Session, run_ids: Sequence[str]) -> list[FloodRunProductQuality]:
    """Refresh several runs idempotently, preserving explicit empty-source rows."""
    ordered_run_ids = _dedupe_run_ids(run_ids)
    if not ordered_run_ids:
        return []
    qualities = _quality_for_runs(session, ordered_run_ids)
    quality_run_ids = {quality.run_id for quality in qualities}
    stale_run_ids = [run_id for run_id in ordered_run_ids if run_id not in quality_run_ids]
    if stale_run_ids:
        _delete_historical_quality_rows(session, stale_run_ids)
    if qualities:
        _upsert_quality_rows(session, qualities)
    return [
        quality
        for run_id in ordered_run_ids
        if (quality := get_run_product_quality(session, run_id)) is not None
    ]


@overload
def backfill_run_product_quality(
    session: Session,
    run_ids: None = None,
) -> FloodRunProductQualityBackfillSummary: ...


@overload
def backfill_run_product_quality(
    session: Session,
    run_ids: Sequence[str],
) -> list[FloodRunProductQuality]: ...


def backfill_run_product_quality(
    session: Session,
    run_ids: Sequence[str] | None = None,
) -> list[FloodRunProductQuality] | FloodRunProductQualityBackfillSummary:
    """Backfill materialized quality for existing source result rows.

    ``run_ids=None`` refreshes all source runs with one set-based statement and
    returns only counts, so production backfills do not materialize every run id
    in Python. Supplying run ids keeps targeted repair semantics and also clears
    stale rows for runs that no longer have source rows.
    """
    if run_ids is not None:
        return refresh_run_product_quality_many(session, _dedupe_run_ids(run_ids))
    refreshed_runs = _upsert_all_quality_rows_from_source(session)
    orphan_quality_rows_deleted = _delete_orphan_quality_rows(session)
    return FloodRunProductQualityBackfillSummary(
        refreshed_runs=refreshed_runs,
        orphan_quality_rows_deleted=orphan_quality_rows_deleted,
    )


def clear_run_product_quality(session: Session, run_id: str) -> None:
    _delete_quality_rows(session, [run_id])


def clear_historical_run_product_quality(session: Session, run_id: str) -> None:
    _delete_historical_quality_rows(session, [run_id])


def write_explicit_run_product_quality(
    session: Session,
    quality: ExplicitFloodRunProductQuality,
) -> FloodRunProductQuality:
    """Persist explicit run-level flood product quality without source rows."""
    normalized = _normalize_explicit_quality(quality)
    _upsert_quality_rows(session, [normalized], preserve_explicit=False)
    return normalized


def get_run_product_quality(session: Session, run_id: str) -> FloodRunProductQuality | None:
    """Read a quality row with safe defaults for legacy schemas."""
    if not _table_exists(session, "flood", "run_product_quality"):
        return None
    columns = _table_columns(session, "flood", "run_product_quality")
    if not columns:
        return None
    select_parts = [
        "run_id",
        *_select_count_columns(columns),
        _select_or_default(columns, "refreshed_at", "CURRENT_TIMESTAMP", "refreshed_at"),
        *_select_explicit_columns(columns),
    ]
    row = session.execute(
        text(
            f"""
            SELECT {', '.join(select_parts)}
            FROM flood.run_product_quality
            WHERE run_id = :run_id
            LIMIT 1
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    return _quality_from_storage_row(row) if row is not None else None


def read_run_product_quality(session: Session, run_id: str) -> FloodRunProductQualityReadiness:
    """Return flood quality readiness without affecting q_down availability."""
    if not _table_exists(session, "flood", "run_product_quality"):
        return _missing_storage_readiness(
            run_id,
            "FLOOD_QUALITY_STORAGE_MISSING",
            "flood.run_product_quality is absent.",
        )
    columns = _table_columns(session, "flood", "run_product_quality")
    if "quality_state" not in columns:
        legacy = get_run_product_quality(session, run_id)
        if legacy is not None:
            return FloodRunProductQualityReadiness(
                run_id=run_id,
                quality_state=legacy.quality_state,
                unavailable_products=legacy.unavailable_products,
                residual_blockers=legacy.residual_blockers,
                q_down_available=True,
            )
        return _missing_storage_readiness(
            run_id,
            "FLOOD_QUALITY_SCHEMA_MISSING",
            "flood.run_product_quality lacks explicit quality columns.",
        )
    quality = get_run_product_quality(session, run_id)
    if quality is None:
        return FloodRunProductQualityReadiness(
            run_id=run_id,
            quality_state="unavailable",
            unavailable_products=("return_period_result",),
            residual_blockers=(
                _blocker(
                    run_id=run_id,
                    code="RETURN_PERIOD_RESULT_UNAVAILABLE",
                    state="unavailable",
                    quality_flag="missing_run_product_quality",
                    residual_risk="No run-level flood product quality row exists for this run.",
                ),
            ),
            q_down_available=True,
        )
    return FloodRunProductQualityReadiness(
        run_id=run_id,
        quality_state=quality.quality_state,
        unavailable_products=quality.unavailable_products,
        residual_blockers=quality.residual_blockers,
        q_down_available=True,
    )


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
        _historical_quality_from_counts(
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


def _source_run_count(session: Session) -> int:
    return int(
        session.execute(
            text(
                """
                SELECT COUNT(DISTINCT run_id)
                FROM flood.return_period_result
                """
            )
        ).scalar_one()
        or 0
    )


def _upsert_all_quality_rows_from_source(session: Session) -> int:
    table_columns = _table_columns(session, "flood", "run_product_quality")
    columns = tuple(column for column in _all_quality_columns() if column in table_columns)
    update_columns = columns[1:]
    dialect_name = _dialect_name(session)
    excluded = "excluded" if dialect_name == "sqlite" else "EXCLUDED"
    select_columns = _source_select_columns(columns, dialect_name=dialect_name)
    where_clause = ""
    if "quality_source" in columns:
        target_quality_source = (
            "quality_source"
            if dialect_name == "sqlite"
            else "flood.run_product_quality.quality_source"
        )
        where_clause = f"WHERE {target_quality_source} <> 'explicit'"
    result = session.execute(
        text(
            f"""
            INSERT INTO flood.run_product_quality ({', '.join(columns)})
            SELECT
                {', '.join(select_columns)}
            FROM flood.return_period_result
            WHERE 1 = 1
            GROUP BY run_id
            ON CONFLICT (run_id) DO UPDATE SET
                {', '.join(f'{column} = {excluded}.{column}' for column in update_columns)}
                {where_clause}
            """
        ),
        {"refreshed_at": datetime.now(UTC)},
    )
    rowcount = _known_rowcount(result)
    return rowcount if rowcount is not None else _source_run_count(session)


def _upsert_quality_rows(
    session: Session,
    qualities: Sequence[FloodRunProductQuality],
    *,
    preserve_explicit: bool = True,
) -> None:
    if not qualities:
        return
    table_columns = _table_columns(session, "flood", "run_product_quality")
    columns = tuple(column for column in _all_quality_columns() if column in table_columns)
    update_columns = columns[1:]
    for quality in qualities:
        params = {
            column: _serialize_column_value(column, getattr(quality, column))
            for column in columns
        }
        if _dialect_name(session) == "sqlite":
            where_clause = (
                ""
                if not preserve_explicit or "quality_source" not in columns
                else " WHERE quality_source <> 'explicit'"
            )
            statement = f"""
                INSERT INTO flood.run_product_quality ({', '.join(columns)})
                VALUES ({', '.join(_json_placeholder(session, column) for column in columns)})
                ON CONFLICT (run_id) DO UPDATE SET
                    {', '.join(f'{column} = excluded.{column}' for column in update_columns)}
                    {where_clause}
            """
        else:
            where_clause = (
                ""
                if not preserve_explicit or "quality_source" not in columns
                else " WHERE flood.run_product_quality.quality_source <> 'explicit'"
            )
            statement = f"""
                INSERT INTO flood.run_product_quality ({', '.join(columns)})
                VALUES ({', '.join(_json_placeholder(session, column) for column in columns)})
                ON CONFLICT (run_id) DO UPDATE SET
                    {', '.join(f'{column} = EXCLUDED.{column}' for column in update_columns)}
                    {where_clause}
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


def _delete_historical_quality_rows(session: Session, run_ids: Sequence[str]) -> None:
    if not run_ids:
        return
    if not _table_exists(session, "flood", "run_product_quality"):
        return
    columns = _table_columns(session, "flood", "run_product_quality")
    placeholders, params = _run_id_placeholders(run_ids)
    if "quality_source" in columns:
        statement = f"""
            DELETE FROM flood.run_product_quality
            WHERE run_id IN ({placeholders})
              AND quality_source <> 'explicit'
        """
    else:
        statement = f"DELETE FROM flood.run_product_quality WHERE run_id IN ({placeholders})"
    session.execute(text(statement), params)


def _delete_orphan_quality_rows(session: Session) -> int:
    if not _table_exists(session, "flood", "run_product_quality"):
        return 0
    source_filter = ""
    if "quality_source" in _table_columns(session, "flood", "run_product_quality"):
        source_filter = "AND quality.quality_source <> 'explicit'"
    result = session.execute(
        text(
            f"""
            DELETE FROM flood.run_product_quality AS quality
            WHERE NOT EXISTS (
                SELECT 1
                FROM flood.return_period_result AS source
                WHERE source.run_id = quality.run_id
            )
              {source_filter}
            """
        )
    )
    return _known_rowcount(result) or 0


def _historical_quality_from_counts(
    *,
    run_id: str,
    result_rows: int,
    max_result_rows: int,
    return_period_rows: int,
    warning_rows: int,
    max_return_period_rows: int,
    max_warning_rows: int,
    refreshed_at: datetime,
) -> FloodRunProductQuality:
    meaningful_result_rows = max(return_period_rows, warning_rows)
    meaningful_max_result_rows = max(max_return_period_rows, max_warning_rows)
    no_frequency_curve_rows = max(result_rows - return_period_rows, 0)
    warning_threshold_unavailable_rows = max(return_period_rows - warning_rows, 0)
    unavailable_products: list[str] = []
    residual_blockers: list[dict[str, Any]] = []
    if return_period_rows <= 0:
        unavailable_products.append("return_period_result")
        residual_blockers.append(
            _blocker(
                run_id=run_id,
                code="RETURN_PERIOD_RESULT_UNAVAILABLE",
                state="unavailable",
                quality_flag="missing_return_period_result",
                residual_risk="No non-null return-period rows are available for this run.",
            )
        )
    elif no_frequency_curve_rows > 0:
        unavailable_products.append("frequency_curves")
        residual_blockers.append(
            _blocker(
                run_id=run_id,
                code="FREQUENCY_CURVES_UNAVAILABLE",
                state="degraded",
                quality_flag="no_frequency_curve",
                residual_risk="Some rows have null return_period because frequency curves are unavailable.",
            )
        )
    if warning_threshold_unavailable_rows > 0:
        unavailable_products.append("warning_thresholds")
        residual_blockers.append(
            _blocker(
                run_id=run_id,
                code="WARNING_THRESHOLDS_UNAVAILABLE",
                state="unavailable",
                quality_flag="warning_thresholds_unavailable",
                residual_risk="warning_level remains null for return-period rows.",
            )
        )
    quality_state = _quality_state_from_products(unavailable_products)
    return FloodRunProductQuality(
        run_id=run_id,
        result_rows=result_rows,
        max_result_rows=max_result_rows,
        return_period_rows=return_period_rows,
        warning_rows=warning_rows,
        max_return_period_rows=max_return_period_rows,
        max_warning_rows=max_warning_rows,
        refreshed_at=refreshed_at,
        quality_state=quality_state,
        quality_source="historical_backfill",
        unavailable_products=tuple(unavailable_products),
        residual_blockers=tuple(residual_blockers),
        expected_result_rows=result_rows,
        expected_max_result_rows=max_result_rows,
        expected_timestep_result_rows=max(result_rows - max_result_rows, 0),
        meaningful_result_rows=meaningful_result_rows,
        meaningful_max_result_rows=meaningful_max_result_rows,
        meaningful_timestep_result_rows=max(meaningful_result_rows - meaningful_max_result_rows, 0),
        no_frequency_curve_rows=no_frequency_curve_rows,
        no_usable_frequency_curve_rows=0,
        warning_threshold_unavailable_rows=warning_threshold_unavailable_rows,
    )


def _normalize_explicit_quality(quality: ExplicitFloodRunProductQuality) -> FloodRunProductQuality:
    run_id = str(quality.run_id).strip()
    if not run_id:
        raise ValueError("run_id is required for explicit flood product quality.")
    state = str(quality.quality_state).strip().lower()
    if state not in QUALITY_STATES:
        raise ValueError(f"Invalid flood product quality_state: {quality.quality_state!r}.")
    refreshed_at = quality.refreshed_at or datetime.now(UTC)
    unavailable_products = _normalize_unavailable_products(quality.unavailable_products)
    residual_blockers = _normalize_blockers(
        quality.residual_blockers,
        run_id=run_id,
        default_state=state,
        strict=True,
    )
    result_rows = _non_negative_int(quality.result_rows, "result_rows")
    max_result_rows = _non_negative_int(quality.max_result_rows, "max_result_rows")
    return_period_rows = _non_negative_int(quality.return_period_rows, "return_period_rows")
    warning_rows = _non_negative_int(quality.warning_rows, "warning_rows")
    max_return_period_rows = _non_negative_int(quality.max_return_period_rows, "max_return_period_rows")
    max_warning_rows = _non_negative_int(quality.max_warning_rows, "max_warning_rows")
    expected_result_rows = _non_negative_int(quality.expected_result_rows, "expected_result_rows")
    expected_max_result_rows = _non_negative_int(quality.expected_max_result_rows, "expected_max_result_rows")
    expected_timestep_result_rows = _non_negative_int(
        quality.expected_timestep_result_rows,
        "expected_timestep_result_rows",
    )
    meaningful_result_rows = _non_negative_int(quality.meaningful_result_rows, "meaningful_result_rows")
    meaningful_max_result_rows = _non_negative_int(
        quality.meaningful_max_result_rows,
        "meaningful_max_result_rows",
    )
    meaningful_timestep_result_rows = _non_negative_int(
        quality.meaningful_timestep_result_rows,
        "meaningful_timestep_result_rows",
    )
    no_frequency_curve_rows = _non_negative_int(quality.no_frequency_curve_rows, "no_frequency_curve_rows")
    no_usable_frequency_curve_rows = _non_negative_int(
        quality.no_usable_frequency_curve_rows,
        "no_usable_frequency_curve_rows",
    )
    warning_threshold_unavailable_rows = _non_negative_int(
        quality.warning_threshold_unavailable_rows,
        "warning_threshold_unavailable_rows",
    )
    if state == "ready":
        _validate_ready_explicit_quality(
            unavailable_products=unavailable_products,
            residual_blockers=residual_blockers,
            result_rows=result_rows,
            max_result_rows=max_result_rows,
            return_period_rows=return_period_rows,
            warning_rows=warning_rows,
            max_return_period_rows=max_return_period_rows,
            max_warning_rows=max_warning_rows,
            expected_result_rows=expected_result_rows,
            expected_max_result_rows=expected_max_result_rows,
            expected_timestep_result_rows=expected_timestep_result_rows,
            meaningful_result_rows=meaningful_result_rows,
            meaningful_max_result_rows=meaningful_max_result_rows,
            meaningful_timestep_result_rows=meaningful_timestep_result_rows,
            no_frequency_curve_rows=no_frequency_curve_rows,
            no_usable_frequency_curve_rows=no_usable_frequency_curve_rows,
            warning_threshold_unavailable_rows=warning_threshold_unavailable_rows,
        )
    return FloodRunProductQuality(
        run_id=run_id,
        result_rows=result_rows,
        max_result_rows=max_result_rows,
        return_period_rows=return_period_rows,
        warning_rows=warning_rows,
        max_return_period_rows=max_return_period_rows,
        max_warning_rows=max_warning_rows,
        refreshed_at=refreshed_at,
        quality_state=state,
        quality_source="explicit",
        unavailable_products=unavailable_products,
        residual_blockers=residual_blockers,
        expected_result_rows=expected_result_rows,
        expected_max_result_rows=expected_max_result_rows,
        expected_timestep_result_rows=expected_timestep_result_rows,
        meaningful_result_rows=meaningful_result_rows,
        meaningful_max_result_rows=meaningful_max_result_rows,
        meaningful_timestep_result_rows=meaningful_timestep_result_rows,
        no_frequency_curve_rows=no_frequency_curve_rows,
        no_usable_frequency_curve_rows=no_usable_frequency_curve_rows,
        warning_threshold_unavailable_rows=warning_threshold_unavailable_rows,
    )


def _quality_from_storage_row(row: Mapping[str, Any]) -> FloodRunProductQuality:
    run_id = str(row["run_id"])
    result_rows = _int_from_row(row, "result_rows")
    max_result_rows = _int_from_row(row, "max_result_rows")
    return_period_rows = _int_from_row(row, "return_period_rows")
    warning_rows = _int_from_row(row, "warning_rows")
    max_return_period_rows = _int_from_row(row, "max_return_period_rows")
    max_warning_rows = _int_from_row(row, "max_warning_rows")
    fallback = _historical_quality_from_counts(
        run_id=run_id,
        result_rows=result_rows,
        max_result_rows=max_result_rows,
        return_period_rows=return_period_rows,
        warning_rows=warning_rows,
        max_return_period_rows=max_return_period_rows,
        max_warning_rows=max_warning_rows,
        refreshed_at=row.get("refreshed_at") or datetime.now(UTC),
    )
    state = str(row.get("quality_state") or fallback.quality_state)
    if state not in QUALITY_STATES:
        state = fallback.quality_state
    source = str(row.get("quality_source") or fallback.quality_source)
    if source not in QUALITY_SOURCES:
        source = fallback.quality_source
    unavailable_products = _normalize_unavailable_products(
        _json_value(row.get("unavailable_products"), default=list(fallback.unavailable_products))
    )
    residual_blockers = _normalize_blockers(
        _json_value(row.get("residual_blockers"), default=list(fallback.residual_blockers)),
        run_id=run_id,
        default_state=state,
        strict=False,
    )
    if source == "historical_backfill" and _is_count_only_historical_quality(
        row=row,
        unavailable_products=unavailable_products,
        residual_blockers=residual_blockers,
    ):
        state = fallback.quality_state
        unavailable_products = fallback.unavailable_products
        residual_blockers = fallback.residual_blockers
    return FloodRunProductQuality(
        run_id=run_id,
        result_rows=result_rows,
        max_result_rows=max_result_rows,
        return_period_rows=return_period_rows,
        warning_rows=warning_rows,
        max_return_period_rows=max_return_period_rows,
        max_warning_rows=max_warning_rows,
        refreshed_at=row.get("refreshed_at") or datetime.now(UTC),
        quality_state=state,
        quality_source=source,
        unavailable_products=unavailable_products,
        residual_blockers=residual_blockers,
        expected_result_rows=_int_from_row(row, "expected_result_rows", fallback.expected_result_rows),
        expected_max_result_rows=_int_from_row(row, "expected_max_result_rows", fallback.expected_max_result_rows),
        expected_timestep_result_rows=_int_from_row(
            row,
            "expected_timestep_result_rows",
            fallback.expected_timestep_result_rows,
        ),
        meaningful_result_rows=_int_from_row(row, "meaningful_result_rows", fallback.meaningful_result_rows),
        meaningful_max_result_rows=_int_from_row(
            row,
            "meaningful_max_result_rows",
            fallback.meaningful_max_result_rows,
        ),
        meaningful_timestep_result_rows=_int_from_row(
            row,
            "meaningful_timestep_result_rows",
            fallback.meaningful_timestep_result_rows,
        ),
        no_frequency_curve_rows=_int_from_row(row, "no_frequency_curve_rows", fallback.no_frequency_curve_rows),
        no_usable_frequency_curve_rows=_int_from_row(
            row,
            "no_usable_frequency_curve_rows",
            fallback.no_usable_frequency_curve_rows,
        ),
        warning_threshold_unavailable_rows=_int_from_row(
            row,
            "warning_threshold_unavailable_rows",
            fallback.warning_threshold_unavailable_rows,
        ),
    )


def _all_quality_columns() -> tuple[str, ...]:
    return (
        "run_id",
        "quality_state",
        "quality_source",
        "unavailable_products",
        "residual_blockers",
        "result_rows",
        "max_result_rows",
        "return_period_rows",
        "warning_rows",
        "max_return_period_rows",
        "max_warning_rows",
        "expected_result_rows",
        "expected_max_result_rows",
        "expected_timestep_result_rows",
        "meaningful_result_rows",
        "meaningful_max_result_rows",
        "meaningful_timestep_result_rows",
        "no_frequency_curve_rows",
        "no_usable_frequency_curve_rows",
        "warning_threshold_unavailable_rows",
        "refreshed_at",
    )


def _source_select_columns(columns: Sequence[str], *, dialect_name: str) -> list[str]:
    meaningful_result_rows = _greatest_sql(
        "SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END)",
        "SUM(CASE WHEN warning_level IS NOT NULL THEN 1 ELSE 0 END)",
        dialect_name=dialect_name,
    )
    meaningful_max_result_rows = _greatest_sql(
        "SUM(CASE WHEN max_over_window = true AND return_period IS NOT NULL THEN 1 ELSE 0 END)",
        "SUM(CASE WHEN max_over_window = true AND warning_level IS NOT NULL THEN 1 ELSE 0 END)",
        dialect_name=dialect_name,
    )
    expressions = {
        "run_id": "run_id",
        "quality_state": """
            CASE
                WHEN SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) <= 0 THEN 'unavailable'
                WHEN SUM(CASE WHEN warning_level IS NOT NULL THEN 1 ELSE 0 END)
                    < SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) THEN 'unavailable'
                WHEN COUNT(*) > SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) THEN 'degraded'
                ELSE 'ready'
            END AS quality_state
        """,
        "quality_source": "'historical_backfill' AS quality_source",
        "unavailable_products": _source_unavailable_products_sql(dialect_name),
        "residual_blockers": _source_residual_blockers_sql(dialect_name),
        "result_rows": "COUNT(*) AS result_rows",
        "max_result_rows": "SUM(CASE WHEN max_over_window = true THEN 1 ELSE 0 END) AS max_result_rows",
        "return_period_rows": "SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) AS return_period_rows",
        "warning_rows": "SUM(CASE WHEN warning_level IS NOT NULL THEN 1 ELSE 0 END) AS warning_rows",
        "max_return_period_rows": """
            SUM(CASE WHEN max_over_window = true AND return_period IS NOT NULL THEN 1 ELSE 0 END)
                AS max_return_period_rows
        """,
        "max_warning_rows": """
            SUM(CASE WHEN max_over_window = true AND warning_level IS NOT NULL THEN 1 ELSE 0 END)
                AS max_warning_rows
        """,
        "expected_result_rows": "COUNT(*) AS expected_result_rows",
        "expected_max_result_rows": """
            SUM(CASE WHEN max_over_window = true THEN 1 ELSE 0 END) AS expected_max_result_rows
        """,
        "expected_timestep_result_rows": """
            SUM(CASE WHEN max_over_window = false THEN 1 ELSE 0 END) AS expected_timestep_result_rows
        """,
        "meaningful_result_rows": f"{meaningful_result_rows} AS meaningful_result_rows",
        "meaningful_max_result_rows": meaningful_max_result_rows + " AS meaningful_max_result_rows",
        "meaningful_timestep_result_rows": _greatest_sql(
            f"({meaningful_result_rows}) - ({meaningful_max_result_rows})",
            "0",
            dialect_name=dialect_name,
        ) + " AS meaningful_timestep_result_rows",
        "no_frequency_curve_rows": """
            SUM(CASE WHEN quality_flag = 'no_frequency_curve' THEN 1 ELSE 0 END) AS no_frequency_curve_rows
        """,
        "no_usable_frequency_curve_rows": """
            SUM(CASE WHEN quality_flag = 'no_usable_frequency_curve' THEN 1 ELSE 0 END)
                AS no_usable_frequency_curve_rows
        """,
        "warning_threshold_unavailable_rows": """
            SUM(CASE WHEN quality_flag = 'warning_thresholds_unavailable' THEN 1 ELSE 0 END)
                AS warning_threshold_unavailable_rows
        """,
        "refreshed_at": ":refreshed_at AS refreshed_at",
    }
    return [expressions[column] for column in columns]


def _greatest_sql(left: str, right: str, *, dialect_name: str) -> str:
    function_name = "max" if dialect_name == "sqlite" else "GREATEST"
    return f"{function_name}({left}, {right})"


def _source_unavailable_products_sql(dialect_name: str) -> str:
    if dialect_name == "sqlite":
        return "'[]' AS unavailable_products"
    return """
        to_jsonb(array_remove(ARRAY[
            CASE
                WHEN SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) <= 0
                THEN 'return_period_result'
                ELSE NULL
            END,
            CASE
                WHEN COUNT(*) > SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END)
                  AND SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) > 0
                THEN 'frequency_curves'
                ELSE NULL
            END,
            CASE
                WHEN SUM(CASE WHEN warning_level IS NOT NULL THEN 1 ELSE 0 END)
                    < SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END)
                  AND SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) > 0
                THEN 'warning_thresholds'
                ELSE NULL
            END
        ], NULL)) AS unavailable_products
    """


def _source_residual_blockers_sql(dialect_name: str) -> str:
    if dialect_name == "sqlite":
        return "'[]' AS residual_blockers"
    return """
        (
            CASE
                WHEN SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) <= 0
                THEN jsonb_build_array(jsonb_build_object(
                    'code', 'RETURN_PERIOD_RESULT_UNAVAILABLE',
                    'state', 'unavailable',
                    'quality_flag', 'missing_return_period_result',
                    'residual_risk', 'No non-null return-period rows are available for this run.',
                    'run_id', run_id
                ))
                ELSE '[]'::jsonb
            END
            || CASE
                WHEN COUNT(*) > SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END)
                  AND SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) > 0
                THEN jsonb_build_array(jsonb_build_object(
                    'code', 'FREQUENCY_CURVES_UNAVAILABLE',
                    'state', 'degraded',
                    'quality_flag', 'no_frequency_curve',
                    'residual_risk',
                    'Some rows have null return_period because frequency curves are unavailable.',
                    'run_id', run_id
                ))
                ELSE '[]'::jsonb
            END
            || CASE
                WHEN SUM(CASE WHEN warning_level IS NOT NULL THEN 1 ELSE 0 END)
                    < SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END)
                  AND SUM(CASE WHEN return_period IS NOT NULL THEN 1 ELSE 0 END) > 0
                THEN jsonb_build_array(jsonb_build_object(
                    'code', 'WARNING_THRESHOLDS_UNAVAILABLE',
                    'state', 'unavailable',
                    'quality_flag', 'warning_thresholds_unavailable',
                    'residual_risk', 'warning_level remains null for return-period rows.',
                    'run_id', run_id
                ))
                ELSE '[]'::jsonb
            END
        ) AS residual_blockers
    """


def _select_count_columns(columns: set[str]) -> list[str]:
    count_columns = (
        "result_rows",
        "max_result_rows",
        "return_period_rows",
        "warning_rows",
        "max_return_period_rows",
        "max_warning_rows",
    )
    return [_select_or_default(columns, column, "0", column) for column in count_columns]


def _select_explicit_columns(columns: set[str]) -> list[str]:
    defaults = {
        "quality_state": "NULL",
        "quality_source": "NULL",
        "unavailable_products": "NULL",
        "residual_blockers": "NULL",
        "expected_result_rows": "0",
        "expected_max_result_rows": "0",
        "expected_timestep_result_rows": "0",
        "meaningful_result_rows": "0",
        "meaningful_max_result_rows": "0",
        "meaningful_timestep_result_rows": "0",
        "no_frequency_curve_rows": "0",
        "no_usable_frequency_curve_rows": "0",
        "warning_threshold_unavailable_rows": "0",
    }
    return [_select_or_default(columns, column, default, column) for column, default in defaults.items()]


def _select_or_default(columns: set[str], column: str, default: str, alias: str) -> str:
    return column if column in columns else f"{default} AS {alias}"


def _serialize_column_value(column: str, value: Any) -> Any:
    if column in JSON_COLUMNS:
        return json.dumps(value, default=str, sort_keys=True)
    return value


def _json_placeholder(session: Session, column: str) -> str:
    if column not in JSON_COLUMNS:
        return f":{column}"
    if _dialect_name(session) == "sqlite":
        return f":{column}"
    return f"CAST(:{column} AS jsonb)"


def _normalize_unavailable_products(products: Any) -> tuple[str, ...]:
    if not isinstance(products, Sequence) or isinstance(products, str | bytes):
        return ()
    seen: set[str] = set()
    normalized: list[str] = []
    for product in products:
        token = str(product).strip()
        if token and token not in seen:
            normalized.append(token)
            seen.add(token)
    return tuple(normalized)


def _normalize_blockers(
    blockers: Any,
    *,
    run_id: str,
    default_state: str,
    strict: bool,
) -> tuple[dict[str, Any], ...]:
    if not isinstance(blockers, Sequence) or isinstance(blockers, str | bytes):
        if strict:
            raise ValueError("residual_blockers must be a sequence of objects.")
        return ()
    normalized: list[dict[str, Any]] = []
    for blocker in blockers[:MAX_RESIDUAL_BLOCKERS]:
        if not isinstance(blocker, Mapping):
            if strict:
                raise ValueError("Each residual_blocker must be an object.")
            continue
        normalized.append(_normalize_blocker(blocker, run_id=run_id, default_state=default_state, strict=strict))
    return tuple(normalized)


def _normalize_blocker(
    blocker: Mapping[str, Any],
    *,
    run_id: str,
    default_state: str,
    strict: bool = False,
) -> dict[str, Any]:
    normalized = {str(key): value for key, value in blocker.items() if str(key) in BLOCKER_ALLOWED_FIELDS}
    if strict:
        missing = [field for field in BLOCKER_REQUIRED_FIELDS if not _clean_string(normalized.get(field))]
        if missing:
            raise ValueError(f"residual_blocker missing required fields: {', '.join(missing)}.")
        if _clean_string(normalized.get("run_id")) != run_id:
            raise ValueError("residual_blocker run_id must match quality.run_id.")
    code = _clean_string(normalized.get("code")) or "FLOOD_PRODUCT_QUALITY_BLOCKER"
    state = _clean_string(normalized.get("state")) or default_state
    quality_flag = _clean_string(normalized.get("quality_flag")) or "unknown"
    residual_risk = _clean_string(normalized.get("residual_risk")) or "Flood product quality is not ready."
    if state not in QUALITY_STATES:
        if strict:
            raise ValueError(f"Invalid residual_blocker state: {state!r}.")
        state = default_state
    normalized.update(
        {
            "code": code,
            "state": state,
            "quality_flag": quality_flag,
            "residual_risk": residual_risk,
            "run_id": _clean_string(normalized.get("run_id")) or run_id,
        }
    )
    if "sample_segment_ids" in normalized:
        normalized["sample_segment_ids"] = _normalize_sample_segment_ids(normalized["sample_segment_ids"])
    if "model_id" in normalized:
        normalized["model_id"] = _clean_string(normalized["model_id"])
    if "river_network_version_id" in normalized:
        normalized["river_network_version_id"] = _clean_string(normalized["river_network_version_id"])
    if "count" in normalized:
        normalized["count"] = _normalize_blocker_count(
            normalized["count"],
            "residual_blocker.count",
            strict=strict,
        )
    if "omitted_count" in normalized:
        normalized["omitted_count"] = _normalize_blocker_count(
            normalized["omitted_count"],
            "residual_blocker.omitted_count",
            strict=strict,
        )
    return dict(sorted(normalized.items()))


def _validate_ready_explicit_quality(
    *,
    unavailable_products: tuple[str, ...],
    residual_blockers: tuple[dict[str, Any], ...],
    result_rows: int,
    max_result_rows: int,
    return_period_rows: int,
    warning_rows: int,
    max_return_period_rows: int,
    max_warning_rows: int,
    expected_result_rows: int,
    expected_max_result_rows: int,
    expected_timestep_result_rows: int,
    meaningful_result_rows: int,
    meaningful_max_result_rows: int,
    meaningful_timestep_result_rows: int,
    no_frequency_curve_rows: int,
    no_usable_frequency_curve_rows: int,
    warning_threshold_unavailable_rows: int,
) -> None:
    if unavailable_products:
        raise ValueError("ready explicit flood product quality cannot include unavailable_products.")
    if residual_blockers:
        raise ValueError("ready explicit flood product quality cannot include residual_blockers.")
    if no_frequency_curve_rows or no_usable_frequency_curve_rows or warning_threshold_unavailable_rows:
        raise ValueError("ready explicit flood product quality cannot include unavailable counters.")
    if max_result_rows > result_rows:
        raise ValueError("ready explicit flood product quality has inconsistent max/result row counts.")
    if return_period_rows > result_rows or warning_rows > result_rows:
        raise ValueError("ready explicit flood product quality has inconsistent result row counts.")
    if max_return_period_rows > max_result_rows or max_warning_rows > max_result_rows:
        raise ValueError("ready explicit flood product quality has inconsistent max-window row counts.")
    if max_return_period_rows > return_period_rows or max_warning_rows > warning_rows:
        raise ValueError("ready explicit flood product quality has inconsistent max-window meaningful row counts.")
    if expected_result_rows > meaningful_result_rows:
        raise ValueError(
            "ready explicit flood product quality requires expected_result_rows <= meaningful_result_rows."
        )
    if expected_max_result_rows > meaningful_max_result_rows:
        raise ValueError(
            "ready explicit flood product quality requires expected_max_result_rows <= meaningful_max_result_rows."
        )
    if expected_timestep_result_rows > meaningful_timestep_result_rows:
        raise ValueError(
            "ready explicit flood product quality requires "
            "expected_timestep_result_rows <= meaningful_timestep_result_rows."
        )
    if return_period_rows != result_rows or warning_rows != result_rows:
        raise ValueError("ready explicit flood product quality requires complete return-period and warning rows.")
    if max_return_period_rows != max_result_rows or max_warning_rows != max_result_rows:
        raise ValueError("ready explicit flood product quality requires complete max-window rows.")


def _is_count_only_historical_quality(
    *,
    row: Mapping[str, Any],
    unavailable_products: tuple[str, ...],
    residual_blockers: tuple[dict[str, Any], ...],
) -> bool:
    if unavailable_products or residual_blockers:
        return False
    default_or_zero_fields = (
        "expected_result_rows",
        "expected_max_result_rows",
        "expected_timestep_result_rows",
        "meaningful_result_rows",
        "meaningful_max_result_rows",
        "meaningful_timestep_result_rows",
        "no_frequency_curve_rows",
        "no_usable_frequency_curve_rows",
        "warning_threshold_unavailable_rows",
    )
    return all(_int_from_row(row, field, 0) == 0 for field in default_or_zero_fields)


def _normalize_sample_segment_ids(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    sample: list[str] = []
    seen: set[str] = set()
    for item in value:
        segment_id = _clean_string(item)
        if segment_id and segment_id not in seen:
            sample.append(segment_id)
            seen.add(segment_id)
        if len(sample) >= MAX_BLOCKER_SAMPLE_SEGMENT_IDS:
            break
    return sample


def _normalize_blocker_count(value: Any, name: str, *, strict: bool) -> int:
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        if strict:
            raise ValueError(f"{name} must be a non-negative integer.") from None
        return 0
    if count < 0:
        if strict:
            raise ValueError(f"{name} must be non-negative.")
        return 0
    return count


def _clean_string(value: Any) -> str:
    return str(value or "").strip()[:MAX_BLOCKER_STRING_LENGTH]


def _blocker(
    *,
    run_id: str,
    code: str,
    state: str,
    quality_flag: str,
    residual_risk: str,
) -> dict[str, Any]:
    return _normalize_blocker(
        {
            "code": code,
            "state": state,
            "quality_flag": quality_flag,
            "residual_risk": residual_risk,
            "run_id": run_id,
        },
        run_id=run_id,
        default_state=state,
        strict=True,
    )


def _missing_storage_readiness(run_id: str, code: str, residual_risk: str) -> FloodRunProductQualityReadiness:
    return FloodRunProductQualityReadiness(
        run_id=run_id,
        quality_state="unavailable",
        unavailable_products=("return_period_result",),
        residual_blockers=(
            _blocker(
                run_id=run_id,
                code=code,
                state="unavailable",
                quality_flag="flood_quality_storage_unavailable",
                residual_risk=residual_risk,
            ),
        ),
        q_down_available=True,
    )


def _quality_state_from_products(products: Sequence[str]) -> str:
    if "return_period_result" in products or "warning_thresholds" in products:
        return "unavailable"
    if products:
        return "degraded"
    return "ready"


def _non_negative_int(value: Any, name: str) -> int:
    integer = int(value or 0)
    if integer < 0:
        raise ValueError(f"{name} must be non-negative.")
    return integer


def _int_from_row(row: Mapping[str, Any], key: str, default: int = 0) -> int:
    return int(row.get(key) if row.get(key) is not None else default)


def _json_value(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _known_rowcount(result: object) -> int | None:
    rowcount = getattr(result, "rowcount", None)
    return rowcount if isinstance(rowcount, int) and rowcount >= 0 else None


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


def _table_columns(session: Session, schema: str, table_name: str) -> set[str]:
    if not _table_exists(session, schema, table_name):
        return set()
    if _dialect_name(session) == "sqlite":
        try:
            rows = session.execute(text(f"PRAGMA {schema}.table_info({table_name})")).mappings()
            return {str(row["name"]) for row in rows}
        except SQLAlchemyError:
            return set()
    try:
        return {column["name"] for column in inspect(session.get_bind()).get_columns(table_name, schema=schema)}
    except SQLAlchemyError:
        return set()


def _dialect_name(session: Session) -> str:
    return str(getattr(session.get_bind().dialect, "name", ""))
