from __future__ import annotations

import csv
import math
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from packages.common.object_store import LocalObjectStore

SECONDS_PER_DAY = 86_400.0
VARIABLE_Q_DOWN = "q_down"
UNIT_M3S = "m3/s"


class OutputParsingError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class OutputParserConfig:
    object_store_root: Path | str
    object_store_prefix: str = ""
    max_flow_m3s: float = 100_000.0
    batch_size: int = 1000

    def __post_init__(self) -> None:
        object.__setattr__(self, "object_store_root", Path(self.object_store_root).expanduser().resolve())

    @classmethod
    def from_env(cls) -> OutputParserConfig:
        workspace_root = os.getenv("WORKSPACE_ROOT", ".")
        return cls(
            object_store_root=os.getenv("OBJECT_STORE_ROOT", workspace_root),
            object_store_prefix=os.getenv("OBJECT_STORE_PREFIX", ""),
            max_flow_m3s=float(os.getenv("OUTPUT_PARSER_MAX_FLOW_M3S", "100000")),
            batch_size=int(os.getenv("OUTPUT_PARSER_BATCH_SIZE", "1000")),
        )


@dataclass(frozen=True)
class HydroRunContext:
    run_id: str
    model_id: str
    basin_version_id: str
    river_network_version_id: str
    source_id: str | None
    cycle_id: str | None
    cycle_time: datetime | None
    start_time: datetime
    output_uri: str | None = None
    run_type: str = "forecast"
    scenario_id: str | None = None


@dataclass(frozen=True)
class RiverSegmentOrder:
    river_segment_id: str
    river_network_version_id: str
    segment_order: int | None


@dataclass(frozen=True)
class RiverTimeseriesRow:
    run_id: str
    basin_version_id: str
    river_network_version_id: str
    river_segment_id: str
    valid_time: datetime
    lead_time_hours: int | None
    variable: str
    value: float
    unit: str
    quality_flag: str = "ok"


@dataclass(frozen=True)
class QCResultRecord:
    qc_checkpoint: str
    target_type: str
    target_id: str
    run_id: str
    cycle_id: str | None
    passed: bool
    severity: str
    checks_json: dict[str, Any]
    message: str


@dataclass(frozen=True)
class OutputParsingResult:
    run_id: str
    status: str
    source_file: str
    rows_written: int
    qc_passed: bool
    max_value_m3s: float | None


class OutputParserRepository(Protocol):
    def load_run_context(self, run_id: str) -> HydroRunContext:
        raise NotImplementedError

    def load_river_segments(self, river_network_version_id: str) -> tuple[RiverSegmentOrder, ...]:
        raise NotImplementedError

    def upsert_river_timeseries(self, rows: tuple[RiverTimeseriesRow, ...], *, batch_size: int) -> None:
        raise NotImplementedError

    def insert_qc_result(self, record: QCResultRecord) -> dict[str, Any]:
        raise NotImplementedError

    def mark_run_parsed(self, run_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def mark_run_failed(self, run_id: str, error_code: str, error_message: str) -> dict[str, Any]:
        raise NotImplementedError


class OutputParser:
    def __init__(
        self,
        *,
        config: OutputParserConfig,
        repository: OutputParserRepository,
        object_store: LocalObjectStore | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.object_store = object_store or LocalObjectStore(config.object_store_root, config.object_store_prefix)

    @classmethod
    def from_env(cls) -> OutputParser:
        config = OutputParserConfig.from_env()
        return cls(config=config, repository=PsycopgOutputParserRepository.from_env())

    def parse_run(self, run_id: str) -> OutputParsingResult:
        context = self.repository.load_run_context(run_id)
        try:
            segments = self.repository.load_river_segments(context.river_network_version_id)
            source_file = self._find_rivqdown_file(context)
            rows = parse_rivqdown_file(source_file, context, segments)
            qc_record = build_qc_result(rows, context, self.config.max_flow_m3s)
            if not qc_record.passed:
                rows = tuple(replace(row, quality_flag="qc_warning") for row in rows)

            self.repository.upsert_river_timeseries(rows, batch_size=self.config.batch_size)
            self.repository.insert_qc_result(qc_record)
            self.repository.mark_run_parsed(context.run_id)
            return OutputParsingResult(
                run_id=context.run_id,
                status="parsed",
                source_file=str(source_file),
                rows_written=len(rows),
                qc_passed=qc_record.passed,
                max_value_m3s=qc_record.checks_json["range_check"].get("max_value"),
            )
        except OutputParsingError as error:
            self.repository.mark_run_failed(context.run_id, error.error_code, error.message)
            raise

    def _find_rivqdown_file(self, context: HydroRunContext) -> Path:
        output_uri = context.output_uri or f"runs/{context.run_id}/output/"
        path = _resolve_object_path_allowing_directory(self.object_store, output_uri)
        if path.is_file():
            if _is_rivqdown_path(path):
                return path
            raise OutputParsingError("RIVQDOWN_NOT_FOUND", f"Output URI is not a .rivqdown file: {output_uri}")

        if not path.exists():
            raise OutputParsingError("OUTPUT_URI_NOT_FOUND", f"hydro_run output path does not exist: {output_uri}")
        if not path.is_dir():
            raise OutputParsingError(
                "OUTPUT_URI_INVALID",
                f"hydro_run output path is not a file or directory: {output_uri}",
            )

        candidates = sorted(file for file in path.iterdir() if file.is_file() and _is_rivqdown_path(file))
        if not candidates:
            raise OutputParsingError("RIVQDOWN_NOT_FOUND", f"No .rivqdown file found under {output_uri}")
        return candidates[0]


def parse_rivqdown_file(
    path: Path,
    context: HydroRunContext,
    segments: tuple[RiverSegmentOrder, ...],
) -> tuple[RiverTimeseriesRow, ...]:
    if not segments:
        raise OutputParsingError(
            "RIVER_SEGMENTS_MISSING",
            f"No river segments found for river_network_version_id {context.river_network_version_id}",
        )

    lines = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not lines:
        raise OutputParsingError("RIVQDOWN_EMPTY", f".rivqdown file is empty: {path}")

    first_tokens = _split_row(lines[0])
    data_lines = lines[1:] if _looks_like_header(first_tokens, context.start_time) else lines
    if not data_lines:
        raise OutputParsingError("RIVQDOWN_NO_DATA", f".rivqdown file contains no data rows: {path}")

    rows: list[RiverTimeseriesRow] = []
    expected_columns: int | None = None
    data_start_line = 2 if len(data_lines) != len(lines) else 1
    for row_offset, line in enumerate(data_lines):
        line_number = data_start_line + row_offset
        tokens = _split_row(line)
        if expected_columns is None:
            expected_columns = len(tokens)
            data_column_count = expected_columns - 1
            if data_column_count != len(segments):
                raise OutputParsingError(
                    "COLUMN_COUNT_MISMATCH",
                    "Column count mismatch: "
                    f"file has {data_column_count} columns, river_network_version has {len(segments)} segments",
                )
        elif len(tokens) != expected_columns:
            raise OutputParsingError(
                "INCONSISTENT_COLUMN_COUNT",
                f"Row {line_number} has {len(tokens)} columns; expected {expected_columns}",
            )

        if len(tokens) < 2:
            raise OutputParsingError("MALFORMED_ROW", f"Row {line_number} must contain time plus data columns")

        valid_time = _parse_valid_time(tokens[0], context.start_time, line_number)
        lead_time_hours = None if context.run_type == "analysis" else _lead_time_hours(valid_time, context.cycle_time)
        for segment, value_token in zip(segments, tokens[1:], strict=True):
            try:
                value_m3d = float(value_token)
            except ValueError as error:
                raise OutputParsingError(
                    "NON_NUMERIC_FLOW",
                    f"Row {line_number} contains non-numeric flow value {value_token!r}",
                ) from error
            if not math.isfinite(value_m3d):
                raise OutputParsingError(
                    "NON_FINITE_FLOW",
                    f"Row {line_number} contains non-finite flow value {value_token!r}",
                )

            rows.append(
                RiverTimeseriesRow(
                    run_id=context.run_id,
                    basin_version_id=context.basin_version_id,
                    river_network_version_id=context.river_network_version_id,
                    river_segment_id=segment.river_segment_id,
                    valid_time=valid_time,
                    lead_time_hours=lead_time_hours,
                    variable=VARIABLE_Q_DOWN,
                    value=value_m3d / SECONDS_PER_DAY,
                    unit=UNIT_M3S,
                )
            )
    return tuple(rows)


def build_qc_result(
    rows: tuple[RiverTimeseriesRow, ...],
    context: HydroRunContext,
    max_flow_m3s: float,
) -> QCResultRecord:
    negative_rows = [row for row in rows if row.value < 0.0]
    outlier_rows = [row for row in rows if row.value > max_flow_m3s]
    max_value = max((row.value for row in rows), default=None)
    checks_json = {
        "non_negative": {
            "passed": not negative_rows,
            "count": len(rows),
            "failed_count": len(negative_rows),
            "failures": [_qc_row(row) for row in negative_rows[:50]],
        },
        "range_check": {
            "passed": not outlier_rows,
            "max_value": max_value,
            "upper_bound": max_flow_m3s,
            "outlier_count": len(outlier_rows),
            "outliers": [_qc_row(row) for row in outlier_rows[:50]],
        },
    }
    passed = checks_json["non_negative"]["passed"] and checks_json["range_check"]["passed"]
    return QCResultRecord(
        qc_checkpoint="output_parsing",
        target_type="river_timeseries",
        target_id=context.run_id,
        run_id=context.run_id,
        cycle_id=context.cycle_id,
        passed=passed,
        severity="info" if passed else "warning",
        checks_json=checks_json,
        message="output parsing QC passed" if passed else "output parsing QC warning",
    )


@dataclass(frozen=True)
class PsycopgOutputParserRepository:
    database_url: str

    @classmethod
    def from_env(cls) -> PsycopgOutputParserRepository:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise OutputParsingError("DATABASE_URL_MISSING", "DATABASE_URL is required for output parsing.")
        return cls(database_url)

    def load_run_context(self, run_id: str) -> HydroRunContext:
        row = self._fetch_one(
            """
            SELECT
                h.run_id,
                h.model_id,
                h.basin_version_id,
                h.source_id,
                h.cycle_time,
                h.start_time,
                h.output_uri,
                h.run_type,
                h.scenario_id,
                mi.river_network_version_id,
                fc.cycle_id
            FROM hydro.hydro_run h
            JOIN core.model_instance mi ON mi.model_id = h.model_id
            LEFT JOIN met.forecast_cycle fc
              ON fc.source_id = h.source_id
             AND fc.cycle_time = h.cycle_time
            WHERE h.run_id = %s
            """,
            (run_id,),
            missing_code="HYDRO_RUN_NOT_FOUND",
            missing_message=f"hydro_run not found: {run_id}",
        )
        cycle_time = row["cycle_time"]
        run_type = str(row.get("run_type") or "forecast")
        if cycle_time is None and run_type != "analysis":
            raise OutputParsingError("CYCLE_TIME_MISSING", f"hydro_run {run_id} has no cycle_time.")
        return HydroRunContext(
            run_id=str(row["run_id"]),
            model_id=str(row["model_id"]),
            basin_version_id=str(row["basin_version_id"]),
            river_network_version_id=str(row["river_network_version_id"]),
            source_id=row.get("source_id"),
            cycle_id=row.get("cycle_id"),
            cycle_time=_ensure_utc(cycle_time) if cycle_time is not None else None,
            start_time=_ensure_utc(row["start_time"]),
            output_uri=row.get("output_uri"),
            run_type=run_type,
            scenario_id=row.get("scenario_id"),
        )

    def load_river_segments(self, river_network_version_id: str) -> tuple[RiverSegmentOrder, ...]:
        rows = self._fetch_all(
            """
            SELECT river_segment_id, river_network_version_id, segment_order
            FROM core.river_segment
            WHERE river_network_version_id = %s
            ORDER BY segment_order NULLS LAST, river_segment_id
            """,
            (river_network_version_id,),
        )
        return tuple(
            RiverSegmentOrder(
                river_segment_id=str(row["river_segment_id"]),
                river_network_version_id=str(row["river_network_version_id"]),
                segment_order=row["segment_order"],
            )
            for row in rows
        )

    def upsert_river_timeseries(self, rows: tuple[RiverTimeseriesRow, ...], *, batch_size: int) -> None:
        if not rows:
            return
        value_rows = [
            (
                row.run_id,
                row.basin_version_id,
                row.river_network_version_id,
                row.river_segment_id,
                row.valid_time,
                row.lead_time_hours,
                row.variable,
                row.value,
                row.unit,
                row.quality_flag,
            )
            for row in rows
        ]
        self._execute_values(
            """
            INSERT INTO hydro.river_timeseries (
                run_id,
                basin_version_id,
                river_network_version_id,
                river_segment_id,
                valid_time,
                lead_time_hours,
                variable,
                value,
                unit,
                quality_flag
            )
            VALUES %s
            ON CONFLICT (run_id, river_network_version_id, river_segment_id, variable, valid_time)
            DO UPDATE SET
                basin_version_id = EXCLUDED.basin_version_id,
                lead_time_hours = EXCLUDED.lead_time_hours,
                value = EXCLUDED.value,
                unit = EXCLUDED.unit,
                quality_flag = EXCLUDED.quality_flag
            """,
            value_rows,
            page_size=batch_size,
        )

    def insert_qc_result(self, record: QCResultRecord) -> dict[str, Any]:
        try:
            from psycopg2.extras import Json
        except ImportError as error:
            raise OutputParsingError("PSYCOPG2_MISSING", "psycopg2 is required for QC writes.") from error

        return self._fetch_one(
            """
            INSERT INTO ops.qc_result (
                qc_checkpoint,
                target_type,
                target_id,
                run_id,
                cycle_id,
                passed,
                severity,
                checks_json,
                message
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                record.qc_checkpoint,
                record.target_type,
                record.target_id,
                record.run_id,
                record.cycle_id,
                record.passed,
                record.severity,
                Json(record.checks_json),
                record.message,
            ),
        )

    def mark_run_parsed(self, run_id: str) -> dict[str, Any]:
        return self._fetch_one(
            """
            UPDATE hydro.hydro_run
            SET status = 'parsed',
                error_code = NULL,
                error_message = NULL,
                updated_at = now()
            WHERE run_id = %s
            RETURNING *
            """,
            (run_id,),
        )

    def mark_run_failed(self, run_id: str, error_code: str, error_message: str) -> dict[str, Any]:
        return self._fetch_one(
            """
            UPDATE hydro.hydro_run
            SET status = 'failed',
                error_code = %s,
                error_message = %s,
                updated_at = now()
            WHERE run_id = %s
            RETURNING *
            """,
            (error_code, error_message, run_id),
        )

    def _fetch_one(
        self,
        statement: str,
        parameters: tuple[Any, ...],
        *,
        missing_code: str = "DATABASE_ROW_MISSING",
        missing_message: str = "Database operation did not return a row.",
    ) -> dict[str, Any]:
        rows = self._fetch_all(statement, parameters)
        if not rows:
            raise OutputParsingError(missing_code, missing_message)
        return rows[0]

    def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        try:
            import psycopg2
        except ImportError as error:
            raise OutputParsingError("PSYCOPG2_MISSING", "psycopg2 is required for output parsing.") from error

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
            raise OutputParsingError(
                "OUTPUT_PARSE_DB_ERROR",
                f"Output parser database operation failed: {error}",
            ) from error
        finally:
            if connection is not None:
                connection.close()

    def _execute_values(
        self,
        statement: str,
        rows: list[tuple[Any, ...]],
        *,
        page_size: int,
    ) -> None:
        try:
            import psycopg2
            from psycopg2.extras import execute_values
        except ImportError as error:
            raise OutputParsingError("PSYCOPG2_MISSING", "psycopg2 is required for output parsing.") from error

        connection = None
        try:
            connection = psycopg2.connect(self.database_url)
            connection.autocommit = False
            with connection.cursor() as cursor:
                execute_values(cursor, statement, rows, page_size=page_size)
            connection.commit()
        except psycopg2.Error as error:
            if connection is not None:
                connection.rollback()
            raise OutputParsingError(
                "OUTPUT_PARSE_DB_ERROR",
                f"Output parser database operation failed: {error}",
            ) from error
        finally:
            if connection is not None:
                connection.close()


def _split_row(line: str) -> list[str]:
    if "," in line:
        return [token.strip() for token in next(csv.reader([line]))]
    return line.split()


def _looks_like_header(tokens: list[str], start_time: datetime) -> bool:
    if not tokens:
        return True
    try:
        _parse_time_token(tokens[0], start_time)
    except OutputParsingError:
        return True
    return False


def _parse_valid_time(token: str, start_time: datetime, line_number: int) -> datetime:
    try:
        return _parse_time_token(token, start_time)
    except OutputParsingError as error:
        raise OutputParsingError("INVALID_TIME_VALUE", f"Row {line_number} has invalid time value {token!r}") from error


def _parse_time_token(token: str, start_time: datetime) -> datetime:
    candidate = token.strip()
    if _is_float(candidate):
        return _ensure_utc(start_time) + timedelta(minutes=float(candidate))

    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as error:
        raise OutputParsingError("INVALID_TIME_VALUE", f"Invalid time value: {token!r}") from error
    return _ensure_utc(parsed)


def _lead_time_hours(valid_time: datetime, cycle_time: datetime | None) -> int:
    if cycle_time is None:
        raise OutputParsingError("CYCLE_TIME_MISSING", "cycle_time is required for forecast lead_time_hours.")
    hours = (_ensure_utc(valid_time) - _ensure_utc(cycle_time)).total_seconds() / 3600.0
    rounded = round(hours)
    if abs(hours - rounded) < 1e-9:
        return int(rounded)
    return int(hours)


def _qc_row(row: RiverTimeseriesRow) -> dict[str, Any]:
    return {
        "segment_id": row.river_segment_id,
        "valid_time": _format_time(row.valid_time),
        "value": row.value,
    }


def _is_float(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_time(value: datetime) -> str:
    return _ensure_utc(value).isoformat().replace("+00:00", "Z")


def _is_rivqdown_path(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith((".rivqdown", ".rivqdown.csv", ".rivqdown.dat")) or name in {
        "rivqdown.csv",
        "rivqdown.dat",
    }


def _resolve_object_path_allowing_directory(object_store: LocalObjectStore, key_or_uri: str) -> Path:
    key = _object_key(key_or_uri, object_store.object_store_prefix)
    root = Path(object_store.root)
    target = (root / key).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError(f"Object key escapes object store root: {key_or_uri}") from error
    return target


def _object_key(uri_or_key: str, object_store_prefix: str) -> str:
    candidate = uri_or_key.strip()
    prefix = object_store_prefix.rstrip("/")
    if prefix and candidate.startswith(prefix + "/"):
        candidate = candidate[len(prefix) + 1 :]
    elif candidate.startswith("s3://"):
        candidate = urlparse(candidate).path.strip("/")
    return candidate.strip("/")
