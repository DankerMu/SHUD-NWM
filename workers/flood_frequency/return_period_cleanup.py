from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

NO_CURVE_QUALITY_FLAGS: tuple[str, str] = ("no_frequency_curve", "no_usable_frequency_curve")
CANDIDATE_PREDICATE_SQL = (
    "return_period IS NULL AND warning_level IS NULL "
    "AND quality_flag IN ('no_frequency_curve','no_usable_frequency_curve')"
)
IDENTITY_COLUMNS: tuple[str, ...] = (
    "run_id",
    "river_network_version_id",
    "river_segment_id",
    "duration",
    "valid_time",
    "max_over_window",
)
DEFAULT_BATCH_SIZE = 1000
OPERATOR_DISK_NOTE = "DELETE does not immediately reclaim disk; #491 owns index, vacuum, and repack work."


@dataclass(frozen=True)
class NoCurveCleanupFilters:
    run_ids: tuple[str, ...] = ()
    basin_version_ids: tuple[str, ...] = ()
    source_ids: tuple[str, ...] = ()
    cycle_time_start: str | None = None
    cycle_time_end: str | None = None

    def to_manifest(self) -> dict[str, object]:
        return {
            "run_id": list(self.run_ids),
            "basin_version_id": list(self.basin_version_ids),
            "source_id": list(self.source_ids),
            "cycle_time_start": self.cycle_time_start,
            "cycle_time_end": self.cycle_time_end,
        }


class NoCurveCleanupError(RuntimeError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        manifest: Mapping[str, Any] | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.manifest = dict(manifest) if manifest is not None else None
        self.details = dict(details or {})


def cleanup_no_curve_results(
    session: Session,
    *,
    filters: NoCurveCleanupFilters | None = None,
    apply_changes: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    sleep_interval: float = 0.0,
    manifest_path: Path | None = None,
    overwrite_manifest: bool = False,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Build an audit manifest and optionally delete historical no-curve rows."""
    filters = filters or NoCurveCleanupFilters()
    _validate_filters(filters)
    _validate_cleanup_options(
        apply_changes=apply_changes,
        batch_size=batch_size,
        sleep_interval=sleep_interval,
        manifest_path=manifest_path,
        overwrite_manifest=overwrite_manifest,
    )

    started_at = _utc_now()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "operation": "flood.return_period_result_no_curve_cleanup",
        "mode": "apply" if apply_changes else "dry-run",
        "dry_run": not apply_changes,
        "status": "running" if apply_changes else "dry_run",
        "started_at": started_at.isoformat(),
        "candidate_predicate": CANDIDATE_PREDICATE_SQL,
        "identity_columns": list(IDENTITY_COLUMNS),
        "filters": filters.to_manifest(),
        "operator_note": OPERATOR_DISK_NOTE,
        "database": {
            "dialect": _dialect_name(session),
            "url": redact_database_url(database_url) if database_url else None,
        },
        "batches": [],
        "resume": _resume_block(None, remaining_candidates=None),
    }
    try:
        manifest["target"] = _target_summary(session, filters)
    except SQLAlchemyError as exc:
        manifest["status"] = "failed"
        manifest["error"] = {
            "code": "NO_CURVE_CLEANUP_SUMMARY_FAILED",
            "message": redact_secret_text(str(exc), database_url),
        }
        manifest["completed_at"] = _utc_now().isoformat()
        _write_manifest_if_requested(manifest, manifest_path=manifest_path, overwrite_manifest=overwrite_manifest)
        raise NoCurveCleanupError(
            "NO_CURVE_CLEANUP_SUMMARY_FAILED",
            "Cleanup summary query failed.",
            manifest=manifest,
        ) from exc

    if not apply_changes:
        manifest["completed_at"] = _utc_now().isoformat()
        _write_manifest_if_requested(manifest, manifest_path=manifest_path, overwrite_manifest=overwrite_manifest)
        return manifest

    affected_run_ids = tuple(str(run_id) for run_id in manifest["target"]["affected_runs"]["run_ids"])
    missing_quality = manifest["target"]["quality_coverage"]["missing_explicit_quality_run_ids"]
    if missing_quality:
        error = {
            "code": "MISSING_EXPLICIT_RUN_PRODUCT_QUALITY",
            "message": "Apply refused because at least one affected run lacks explicit flood.run_product_quality.",
            "missing_run_ids": missing_quality,
        }
        manifest["status"] = "blocked"
        manifest["error"] = error
        manifest["completed_at"] = _utc_now().isoformat()
        _write_manifest_if_requested(manifest, manifest_path=manifest_path, overwrite_manifest=overwrite_manifest)
        raise NoCurveCleanupError(
            error["code"],
            error["message"],
            manifest=manifest,
            details={"missing_run_ids": missing_quality},
        )

    manifest["deleted_rows"] = 0
    _persist_apply_manifest(
        manifest,
        manifest_path=manifest_path,
        overwrite_manifest=overwrite_manifest,
        database_url=database_url,
        error_code="NO_CURVE_CLEANUP_MANIFEST_WRITE_FAILED",
    )

    deleted_total = 0
    cursor: dict[str, Any] | None = None
    batch_index = 0
    while True:
        identities = _select_batch_identities(session, filters, batch_size=batch_size, cursor=cursor)
        if not identities:
            break
        batch_index += 1
        batch_started_at = _utc_now()
        batch_record: dict[str, Any] = {
            "batch_index": batch_index,
            "status": "running",
            "started_at": batch_started_at.isoformat(),
            "selected_rows": len(identities),
            "first_identity": _identity_to_manifest(identities[0]),
            "last_identity": _identity_to_manifest(identities[-1]),
        }
        try:
            deleted_rows = _delete_batch_identities(session, filters, identities)
            if deleted_rows != len(identities):
                session.rollback()
                batch_record["status"] = "aborted"
                batch_record["deleted_rows"] = deleted_rows
                batch_record["error"] = (
                    "Selected candidate identities no longer satisfy the explicit "
                    "run_product_quality guard or cleanup predicate."
                )
                batch_record["duration_seconds"] = _elapsed_seconds(batch_started_at)
                manifest["batches"].append(batch_record)
                manifest["status"] = "failed"
                manifest["deleted_rows"] = deleted_total
                manifest["completed_at"] = _utc_now().isoformat()
                manifest["resume"] = _resume_block(cursor, remaining_candidates=None)
                _persist_apply_manifest(
                    manifest,
                    manifest_path=manifest_path,
                    overwrite_manifest=True,
                    database_url=database_url,
                    error_code="NO_CURVE_CLEANUP_MANIFEST_WRITE_FAILED",
                )
                raise NoCurveCleanupError(
                    "EXPLICIT_QUALITY_GUARD_CHANGED",
                    "Apply aborted because selected rows no longer satisfy the explicit quality guard.",
                    manifest=manifest,
                    details={
                        "batch_index": batch_index,
                        "selected_rows": len(identities),
                        "deleted_rows": deleted_rows,
                    },
                )
            session.commit()
        except Exception as exc:
            if isinstance(exc, NoCurveCleanupError):
                raise
            session.rollback()
            batch_record["status"] = "failed"
            batch_record["error"] = redact_secret_text(str(exc), database_url)
            batch_record["duration_seconds"] = _elapsed_seconds(batch_started_at)
            manifest["batches"].append(batch_record)
            manifest["status"] = "failed"
            manifest["deleted_rows"] = deleted_total
            manifest["completed_at"] = _utc_now().isoformat()
            manifest["resume"] = _resume_block(cursor, remaining_candidates=None)
            _persist_apply_manifest(
                manifest,
                manifest_path=manifest_path,
                overwrite_manifest=True,
                database_url=database_url,
                error_code="NO_CURVE_CLEANUP_MANIFEST_WRITE_FAILED",
            )
            raise NoCurveCleanupError(
                "NO_CURVE_CLEANUP_BATCH_FAILED",
                "Apply failed while deleting a cleanup batch.",
                manifest=manifest,
                details={"batch_index": batch_index},
            ) from exc

        deleted_total += deleted_rows
        cursor = identities[-1]
        batch_record["status"] = "committed"
        batch_record["deleted_rows"] = deleted_rows
        batch_record["committed_at"] = _utc_now().isoformat()
        batch_record["duration_seconds"] = _elapsed_seconds(batch_started_at)
        batch_record["cursor_after"] = _identity_to_manifest(cursor)
        manifest["batches"].append(batch_record)
        manifest["deleted_rows"] = deleted_total
        manifest["resume"] = _resume_block(cursor, remaining_candidates=None)
        _persist_apply_manifest(
            manifest,
            manifest_path=manifest_path,
            overwrite_manifest=True,
            database_url=database_url,
            error_code="NO_CURVE_CLEANUP_MANIFEST_WRITE_FAILED",
        )
        if sleep_interval > 0:
            time.sleep(sleep_interval)

    try:
        remaining_candidates = _candidate_total(session, filters)
        post_cleanup_quality = _quality_coverage_for_run_ids(session, affected_run_ids)
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["deleted_rows"] = deleted_total
        manifest["remaining_candidates"] = None
        manifest["completed_at"] = _utc_now().isoformat()
        manifest["resume"] = _resume_block(cursor, remaining_candidates=None)
        manifest["error"] = {
            "code": "NO_CURVE_CLEANUP_POSTCHECK_FAILED",
            "message": redact_secret_text(str(exc), database_url),
        }
        _persist_apply_manifest(
            manifest,
            manifest_path=manifest_path,
            overwrite_manifest=True,
            database_url=database_url,
            error_code="NO_CURVE_CLEANUP_MANIFEST_WRITE_FAILED",
        )
        raise NoCurveCleanupError(
            "NO_CURVE_CLEANUP_POSTCHECK_FAILED",
            "Apply committed batches but failed while collecting post-cleanup evidence.",
            manifest=manifest,
        ) from exc
    manifest["status"] = "completed"
    manifest["deleted_rows"] = deleted_total
    manifest["remaining_candidates"] = remaining_candidates
    manifest["post_cleanup"] = {
        "quality_coverage": post_cleanup_quality,
    }
    manifest["completed_at"] = _utc_now().isoformat()
    manifest["resume"] = _resume_block(cursor, remaining_candidates=remaining_candidates)
    _persist_apply_manifest(
        manifest,
        manifest_path=manifest_path,
        overwrite_manifest=True,
        database_url=database_url,
        error_code="NO_CURVE_CLEANUP_MANIFEST_WRITE_FAILED",
    )
    return manifest


def redact_database_url(database_url: str) -> str:
    if not database_url:
        return database_url
    try:
        parsed = urlsplit(database_url)
    except ValueError:
        return "<redacted>"
    if not parsed.scheme:
        return _redact_password_like_text(database_url)

    netloc = parsed.netloc
    if "@" in netloc:
        userinfo, hostinfo = netloc.rsplit("@", maxsplit=1)
        if ":" in userinfo:
            username, _password = userinfo.split(":", maxsplit=1)
            userinfo = f"{username}:***"
        netloc = f"{userinfo}@{hostinfo}"

    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        redacted = "***" if _looks_secret_key(key) else value
        query_items.append((key, redacted))
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query_items), parsed.fragment))


def redact_secret_text(value: str, database_url: str | None = None) -> str:
    redacted = value
    if database_url:
        redacted = redacted.replace(database_url, redact_database_url(database_url))
        try:
            password = urlsplit(database_url).password
        except ValueError:
            password = None
        if password:
            redacted = redacted.replace(password, "***")
    return _redact_password_like_text(redacted)


def _target_summary(session: Session, filters: NoCurveCleanupFilters) -> dict[str, Any]:
    quality_flag_counts = _count_by_quality_flag(session, filters)
    run_counts = _count_by_run(session, filters)
    return {
        "total_candidates": sum(item["rows"] for item in quality_flag_counts),
        "quality_flag_counts": quality_flag_counts,
        "run_counts": run_counts,
        "max_over_window_counts": _count_by_max_over_window(session, filters),
        "time_bucket_distribution": _time_bucket_distribution(session, filters),
        "chunk_distribution": _chunk_distribution(session, filters),
        "affected_runs": {
            "count": len(run_counts),
            "run_ids": [str(item["run_id"]) for item in run_counts],
        },
        "quality_coverage": _quality_coverage(session, filters),
        "size_stats": _size_stats(session),
    }


def _candidate_total(session: Session, filters: NoCurveCleanupFilters) -> int:
    where_sql, params = _candidate_where(filters, alias="r")
    return int(
        session.execute(
            text(f"SELECT COUNT(*) FROM flood.return_period_result AS r WHERE {where_sql}"),
            params,
        ).scalar_one()
        or 0
    )


def _count_by_quality_flag(session: Session, filters: NoCurveCleanupFilters) -> list[dict[str, Any]]:
    where_sql, params = _candidate_where(filters, alias="r")
    rows = session.execute(
        text(
            f"""
            SELECT r.quality_flag, COUNT(*) AS rows
            FROM flood.return_period_result AS r
            WHERE {where_sql}
            GROUP BY r.quality_flag
            ORDER BY r.quality_flag
            """
        ),
        params,
    ).mappings()
    return [{"quality_flag": str(row["quality_flag"]), "rows": int(row["rows"] or 0)} for row in rows]


def _count_by_run(session: Session, filters: NoCurveCleanupFilters) -> list[dict[str, Any]]:
    where_sql, params = _candidate_where(filters, alias="r")
    rows = session.execute(
        text(
            f"""
            SELECT r.run_id, COUNT(*) AS rows
            FROM flood.return_period_result AS r
            WHERE {where_sql}
            GROUP BY r.run_id
            ORDER BY r.run_id
            """
        ),
        params,
    ).mappings()
    return [{"run_id": str(row["run_id"]), "rows": int(row["rows"] or 0)} for row in rows]


def _count_by_max_over_window(session: Session, filters: NoCurveCleanupFilters) -> list[dict[str, Any]]:
    where_sql, params = _candidate_where(filters, alias="r")
    rows = session.execute(
        text(
            f"""
            SELECT r.max_over_window, COUNT(*) AS rows
            FROM flood.return_period_result AS r
            WHERE {where_sql}
            GROUP BY r.max_over_window
            ORDER BY r.max_over_window
            """
        ),
        params,
    ).mappings()
    return [
        {"max_over_window": bool(row["max_over_window"]), "rows": int(row["rows"] or 0)}
        for row in rows
    ]


def _time_bucket_distribution(session: Session, filters: NoCurveCleanupFilters) -> dict[str, Any]:
    where_sql, params = _candidate_where(filters, alias="r")
    rows = session.execute(
        text(
            f"""
            SELECT date(r.valid_time) AS bucket_start, COUNT(*) AS rows
            FROM flood.return_period_result AS r
            WHERE {where_sql}
            GROUP BY date(r.valid_time)
            ORDER BY bucket_start
            """
        ),
        params,
    ).mappings()
    return {
        "status": "available",
        "bucket": "day",
        "items": [
            {"bucket_start": str(row["bucket_start"]), "rows": int(row["rows"] or 0)}
            for row in rows
        ],
    }


def _chunk_distribution(session: Session, filters: NoCurveCleanupFilters) -> dict[str, Any]:
    if _dialect_name(session) != "postgresql":
        return {
            "status": "unavailable",
            "reason": "timescale_metadata_unavailable_for_dialect",
            "items": [],
        }
    metadata_available, unavailable_reason = _timescale_chunks_available(session)
    if not metadata_available:
        return {"status": "unavailable", "reason": unavailable_reason, "items": []}
    where_sql, params = _candidate_where(filters, alias="r")
    try:
        with session.begin_nested():
            rows = list(
                session.execute(
                    text(
                        f"""
                        SELECT chunks.chunk_name, COUNT(*) AS rows
                        FROM flood.return_period_result AS r
                        JOIN timescaledb_information.chunks AS chunks
                          ON chunks.hypertable_schema = 'flood'
                         AND chunks.hypertable_name = 'return_period_result'
                         AND format('%I.%I', chunks.chunk_schema, chunks.chunk_name)::regclass = r.tableoid::regclass
                        WHERE {where_sql}
                        GROUP BY chunks.chunk_name
                        ORDER BY chunks.chunk_name
                        """
                    ),
                    params,
                ).mappings()
            )
    except SQLAlchemyError as exc:
        return {"status": "unavailable", "reason": redact_secret_text(str(exc)), "items": []}
    return {
        "status": "available",
        "metadata_relation": "timescaledb_information.chunks",
        "items": [{"chunk_name": str(row["chunk_name"]), "rows": int(row["rows"] or 0)} for row in rows],
    }


def _timescale_chunks_available(session: Session) -> tuple[bool, str]:
    try:
        with session.begin_nested():
            available = session.execute(
                text(
                    """
                    SELECT to_regclass('timescaledb_information.chunks') IS NOT NULL AS available
                    """
                )
            ).scalar_one()
    except SQLAlchemyError as exc:
        return False, redact_secret_text(str(exc))
    return bool(available), "timescale_metadata_relation_missing"


def _quality_coverage(session: Session, filters: NoCurveCleanupFilters) -> dict[str, Any]:
    run_counts = _count_by_run(session, filters)
    affected_run_ids = [str(row["run_id"]) for row in run_counts]
    return _quality_coverage_for_run_ids(session, affected_run_ids)


def _quality_coverage_for_run_ids(session: Session, affected_run_ids: Sequence[str]) -> dict[str, Any]:
    if not affected_run_ids:
        return {
            "status": "complete",
            "affected_run_count": 0,
            "explicit_quality_run_count": 0,
            "missing_explicit_quality_run_ids": [],
            "quality_source_counts": {},
        }
    if not _table_exists(session, "flood", "run_product_quality"):
        return {
            "status": "missing_quality_storage",
            "affected_run_count": len(affected_run_ids),
            "explicit_quality_run_count": 0,
            "missing_explicit_quality_run_ids": affected_run_ids,
            "quality_source_counts": {},
        }
    if "quality_source" not in _table_columns(session, "flood", "run_product_quality"):
        return {
            "status": "missing_quality_source_column",
            "affected_run_count": len(affected_run_ids),
            "explicit_quality_run_count": 0,
            "missing_explicit_quality_run_ids": affected_run_ids,
            "quality_source_counts": {},
        }

    run_id_select_sql, params = _run_id_select(affected_run_ids)
    rows = session.execute(
        text(
            f"""
            SELECT candidate_runs.run_id, quality.quality_source
            FROM ({run_id_select_sql}) AS candidate_runs
            LEFT JOIN flood.run_product_quality AS quality
              ON quality.run_id = candidate_runs.run_id
            ORDER BY candidate_runs.run_id
            """
        ),
        params,
    ).mappings()
    source_counts: dict[str, int] = {}
    missing: list[str] = []
    explicit_count = 0
    for row in rows:
        source = row["quality_source"]
        source_label = str(source) if source is not None else "missing"
        source_counts[source_label] = source_counts.get(source_label, 0) + 1
        if source == "explicit":
            explicit_count += 1
        else:
            missing.append(str(row["run_id"]))

    return {
        "status": "complete" if not missing else "missing_explicit_quality",
        "affected_run_count": len(affected_run_ids),
        "explicit_quality_run_count": explicit_count,
        "missing_explicit_quality_run_ids": missing,
        "quality_source_counts": dict(sorted(source_counts.items())),
    }


def _run_id_select(run_ids: Sequence[str]) -> tuple[str, dict[str, str]]:
    params: dict[str, str] = {}
    selects: list[str] = []
    for index, run_id in enumerate(run_ids):
        param_name = f"quality_run_id_{index}"
        selects.append(f"SELECT :{param_name} AS run_id")
        params[param_name] = run_id
    return " UNION ALL ".join(selects), params


def _size_stats(session: Session) -> dict[str, Any]:
    if _dialect_name(session) != "postgresql":
        return {"status": "unavailable", "reason": "pg_relation_size_unavailable_for_dialect"}
    try:
        row = session.execute(
            text(
                """
                SELECT
                    pg_total_relation_size('flood.return_period_result'::regclass) AS total_bytes,
                    pg_table_size('flood.return_period_result'::regclass) AS table_bytes,
                    pg_indexes_size('flood.return_period_result'::regclass) AS index_bytes
                """
            )
        ).mappings().one()
    except SQLAlchemyError as exc:
        return {"status": "unavailable", "reason": redact_secret_text(str(exc))}
    return {
        "status": "available",
        "total_bytes": int(row["total_bytes"] or 0),
        "table_bytes": int(row["table_bytes"] or 0),
        "index_bytes": int(row["index_bytes"] or 0),
    }


def _select_batch_identities(
    session: Session,
    filters: NoCurveCleanupFilters,
    *,
    batch_size: int,
    cursor: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    where_sql, params = _candidate_where(filters, alias="r")
    explicit_quality_sql = _explicit_quality_exists_sql("r")
    cursor_sql = _cursor_where(cursor, params)
    params["batch_size"] = batch_size
    column_sql = ", ".join(f"r.{column}" for column in IDENTITY_COLUMNS)
    order_sql = ", ".join(f"r.{column}" for column in IDENTITY_COLUMNS)
    rows = session.execute(
        text(
            f"""
            SELECT {column_sql}
            FROM flood.return_period_result AS r
            WHERE {where_sql}
              AND {explicit_quality_sql}
            {cursor_sql}
            ORDER BY {order_sql}
            LIMIT :batch_size
            """
        ),
        params,
    ).mappings()
    return [_identity_from_row(row) for row in rows]


def _delete_batch_identities(
    session: Session,
    filters: NoCurveCleanupFilters,
    identities: Sequence[Mapping[str, Any]],
) -> int:
    if not identities:
        return 0
    target_alias = "r"
    where_sql, params = _candidate_where(filters, alias=target_alias)
    explicit_quality_sql = _explicit_quality_exists_sql(target_alias)
    identity_clauses: list[str] = []
    for index, identity in enumerate(identities):
        terms: list[str] = []
        for column in IDENTITY_COLUMNS:
            param_name = f"identity_{index}_{column}"
            terms.append(f"{target_alias}.{column} = :{param_name}")
            params[param_name] = identity[column]
        identity_clauses.append("(" + " AND ".join(terms) + ")")
    if _dialect_name(session) == "sqlite":
        statement = f"""
            DELETE FROM flood.return_period_result
            WHERE rowid IN (
                SELECT {target_alias}.rowid
                FROM flood.return_period_result AS {target_alias}
                WHERE {where_sql}
                  AND {explicit_quality_sql}
                  AND ({' OR '.join(identity_clauses)})
            )
        """
    else:
        statement = f"""
            DELETE FROM flood.return_period_result AS {target_alias}
            WHERE {where_sql}
              AND {explicit_quality_sql}
              AND ({' OR '.join(identity_clauses)})
        """
    result = session.execute(
        text(statement),
        params,
    )
    return _known_rowcount(result)


def _explicit_quality_exists_sql(alias: str | None) -> str:
    prefix = f"{alias}." if alias else ""
    return (
        "EXISTS ("
        "SELECT 1 FROM flood.run_product_quality AS quality "
        f"WHERE quality.run_id = {prefix}run_id "
        "AND quality.quality_source = 'explicit'"
        ")"
    )


def _candidate_where(filters: NoCurveCleanupFilters, *, alias: str | None) -> tuple[str, dict[str, Any]]:
    prefix = f"{alias}." if alias else ""
    params: dict[str, Any] = {
        "no_curve_quality_flag_0": NO_CURVE_QUALITY_FLAGS[0],
        "no_curve_quality_flag_1": NO_CURVE_QUALITY_FLAGS[1],
    }
    clauses = [
        f"{prefix}return_period IS NULL",
        f"{prefix}warning_level IS NULL",
        f"{prefix}quality_flag IN (:no_curve_quality_flag_0, :no_curve_quality_flag_1)",
    ]
    _add_in_filter(clauses, params, f"{prefix}run_id", filters.run_ids, "run_id")
    _add_in_filter(clauses, params, f"{prefix}basin_version_id", filters.basin_version_ids, "basin_version_id")
    _add_in_filter(clauses, params, f"{prefix}source_id", filters.source_ids, "source_id")
    if filters.cycle_time_start is not None:
        clauses.append(f"{prefix}cycle_time >= :cycle_time_start")
        params["cycle_time_start"] = filters.cycle_time_start
    if filters.cycle_time_end is not None:
        clauses.append(f"{prefix}cycle_time <= :cycle_time_end")
        params["cycle_time_end"] = filters.cycle_time_end
    return " AND ".join(clauses), params


def _add_in_filter(
    clauses: list[str],
    params: dict[str, Any],
    column_sql: str,
    values: Sequence[str],
    param_prefix: str,
) -> None:
    normalized = tuple(value for value in values if value)
    if not normalized:
        return
    placeholders: list[str] = []
    for index, value in enumerate(normalized):
        param_name = f"{param_prefix}_{index}"
        placeholders.append(f":{param_name}")
        params[param_name] = value
    clauses.append(f"{column_sql} IN ({', '.join(placeholders)})")


def _cursor_where(cursor: Mapping[str, Any] | None, params: dict[str, Any]) -> str:
    if not cursor:
        return ""
    lexicographic_terms: list[str] = []
    for index, column in enumerate(IDENTITY_COLUMNS):
        equality_terms: list[str] = []
        for previous in IDENTITY_COLUMNS[:index]:
            param_name = f"cursor_{previous}"
            equality_terms.append(f"r.{previous} = :{param_name}")
            params[param_name] = cursor[previous]
        param_name = f"cursor_{column}"
        params[param_name] = cursor[column]
        comparison = f"r.{column} > :{param_name}"
        lexicographic_terms.append("(" + " AND ".join([*equality_terms, comparison]) + ")")
    return "AND (" + " OR ".join(lexicographic_terms) + ")"


def _identity_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    identity = {column: row[column] for column in IDENTITY_COLUMNS}
    identity["max_over_window"] = bool(identity["max_over_window"])
    return identity


def _identity_to_manifest(identity: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if identity is None:
        return None
    return {column: _json_ready(identity[column]) for column in IDENTITY_COLUMNS}


def _resume_block(
    cursor: Mapping[str, Any] | None,
    *,
    remaining_candidates: int | None,
) -> dict[str, Any]:
    return {
        "pagination": "keyset",
        "offset_pagination": False,
        "identity_columns": list(IDENTITY_COLUMNS),
        "last_committed_cursor": _identity_to_manifest(cursor),
        "remaining_candidates": remaining_candidates,
        "continuation_hint": {
            "rerun_with_same_filters": True,
            "resume_after_identity": _identity_to_manifest(cursor),
        },
    }


def _write_manifest_if_requested(
    manifest: Mapping[str, Any],
    *,
    manifest_path: Path | None,
    overwrite_manifest: bool,
) -> None:
    if manifest_path is None:
        return
    _write_json_atomic(manifest_path, manifest, overwrite=overwrite_manifest)


def _persist_apply_manifest(
    manifest: Mapping[str, Any],
    *,
    manifest_path: Path | None,
    overwrite_manifest: bool,
    database_url: str | None,
    error_code: str,
) -> None:
    if manifest_path is None:
        return
    try:
        _write_json_atomic(manifest_path, manifest, overwrite=overwrite_manifest)
    except (NoCurveCleanupError, OSError) as exc:
        failed_manifest = dict(manifest)
        failed_manifest["status"] = "failed"
        failed_manifest["error"] = {
            "code": error_code,
            "message": redact_secret_text(str(exc), database_url),
        }
        raise NoCurveCleanupError(
            error_code,
            "Failed to persist cleanup manifest.",
            manifest=failed_manifest,
        ) from exc


def _write_json_atomic(path: Path, payload: Mapping[str, Any], *, overwrite: bool) -> None:
    _validate_manifest_path(path, overwrite_manifest=overwrite)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp_path.open("x", encoding="utf-8") as handle:
            json.dump(_json_ready(payload), handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(tmp_path, path)
        else:
            os.link(tmp_path, path)
            tmp_path.unlink()
        _fsync_directory(path.parent)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _validate_cleanup_options(
    *,
    apply_changes: bool,
    batch_size: int,
    sleep_interval: float,
    manifest_path: Path | None,
    overwrite_manifest: bool,
) -> None:
    if apply_changes and manifest_path is None:
        raise NoCurveCleanupError(
            "MANIFEST_PATH_REQUIRED",
            "Apply mode requires --manifest-path so committed batches are auditable.",
        )
    if batch_size < 1:
        raise NoCurveCleanupError("INVALID_BATCH_SIZE", "batch_size must be at least 1.")
    if sleep_interval < 0:
        raise NoCurveCleanupError("INVALID_SLEEP_INTERVAL", "sleep_interval must be non-negative.")
    if manifest_path is not None:
        _validate_manifest_path(manifest_path, overwrite_manifest=overwrite_manifest)


def _validate_filters(filters: NoCurveCleanupFilters) -> None:
    for field_name, values in (
        ("run_id", filters.run_ids),
        ("basin_version_id", filters.basin_version_ids),
        ("source_id", filters.source_ids),
    ):
        for value in values:
            if not str(value).strip():
                raise NoCurveCleanupError(
                    "INVALID_FILTER_VALUE",
                    f"{field_name} filter values must be non-empty.",
                    details={"filter": field_name},
                )
    for field_name, value in (
        ("cycle_time_start", filters.cycle_time_start),
        ("cycle_time_end", filters.cycle_time_end),
    ):
        if value is not None and not str(value).strip():
            raise NoCurveCleanupError(
                "INVALID_FILTER_VALUE",
                f"{field_name} filter value must be non-empty.",
                details={"filter": field_name},
            )


def _validate_manifest_path(path: Path, *, overwrite_manifest: bool) -> None:
    if path.exists() and path.is_dir():
        raise NoCurveCleanupError("INVALID_MANIFEST_PATH", f"Manifest path is a directory: {path}")
    if path.exists() and not overwrite_manifest:
        raise NoCurveCleanupError(
            "MANIFEST_PATH_EXISTS",
            f"Manifest path already exists; pass the explicit overwrite flag to replace it: {path}",
        )
    if not path.parent.exists():
        raise NoCurveCleanupError("INVALID_MANIFEST_PATH", f"Manifest parent directory does not exist: {path.parent}")


def _known_rowcount(result: object) -> int:
    rowcount = getattr(result, "rowcount", None)
    return rowcount if isinstance(rowcount, int) and rowcount >= 0 else 0


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
    if _dialect_name(session) == "sqlite":
        rows = session.execute(text(f"PRAGMA {schema}.table_info({table_name})")).mappings()
        return {str(row["name"]) for row in rows}
    try:
        return {str(column["name"]) for column in inspect(session.get_bind()).get_columns(table_name, schema=schema)}
    except SQLAlchemyError:
        return set()


def _dialect_name(session: Session) -> str:
    return str(getattr(session.get_bind().dialect, "name", ""))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _elapsed_seconds(started_at: datetime) -> float:
    return round((_utc_now() - started_at).total_seconds(), 6)


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _looks_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in ("password", "passwd", "secret", "token", "key"))


def _redact_password_like_text(value: str) -> str:
    redacted_parts: list[str] = []
    for part in value.split():
        if "=" in part:
            key, raw = part.split("=", maxsplit=1)
            if _looks_secret_key(key):
                redacted_parts.append(f"{key}=***")
                continue
            redacted_parts.append(f"{key}={raw}")
            continue
        redacted_parts.append(part)
    return " ".join(redacted_parts)
