from __future__ import annotations

import csv
import json
import logging
import math
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from packages.common.object_store import LocalObjectStore
from packages.common.storage import validate_object_path

LOGGER = logging.getLogger(__name__)

SECONDS_PER_DAY = 86_400.0
VARIABLE_Q_DOWN = "q_down"
UNIT_M3S = "m3/s"
UNIX_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)
AUTO_TIME_BASIS_MAX_RELATIVE_DAYS = 366
AUTO_TIME_BASIS_CONTEXT_PADDING_DAYS = 1
DEFAULT_DB_CONNECT_TIMEOUT_SECONDS = 10
DEFAULT_DB_STATEMENT_TIMEOUT_MS = 60_000
PARSE_READY_RUN_STATUSES = ("succeeded", "parsed", "failed")
FAILABLE_RUN_STATUSES = ("created", "staged", "submitted", "running", "succeeded", "parsed")


class OutputParsingError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class OutputParserConfig:
    object_store_root: Path | str
    workspace_root: Path | str | None = None
    object_store_prefix: str = ""
    max_flow_m3s: float = 100_000.0
    batch_size: int = 1000

    def __post_init__(self) -> None:
        object.__setattr__(self, "object_store_root", Path(self.object_store_root).expanduser().resolve())
        if self.workspace_root is not None:
            object.__setattr__(self, "workspace_root", Path(self.workspace_root).expanduser().resolve())

    @classmethod
    def from_env(cls) -> OutputParserConfig:
        workspace_root = os.getenv("WORKSPACE_ROOT", ".")
        return cls(
            workspace_root=workspace_root,
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

    def transaction(self) -> Iterator[OutputParserRepository]:
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
        object_store = LocalObjectStore(config.object_store_root, config.object_store_prefix)
        if _db_free_output_parser_enabled():
            return cls(
                config=config,
                repository=FileOutputParserRepository(config=config, object_store=object_store),
                object_store=object_store,
            )
        return cls(config=config, repository=PsycopgOutputParserRepository.from_env(), object_store=object_store)

    def parse_run(self, run_id: str) -> OutputParsingResult:
        context = self.repository.load_run_context(run_id)
        try:
            segments = self.repository.load_river_segments(context.river_network_version_id)
            source_file = self._find_rivqdown_file(context)
            rows = parse_rivqdown_file(source_file, context, segments)
            qc_record = build_qc_result(rows, context, self.config.max_flow_m3s)
            if not qc_record.passed:
                rows = tuple(replace(row, quality_flag="qc_warning") for row in rows)

            transaction = getattr(self.repository, "transaction", None)
            if callable(transaction):
                with transaction() as repository:
                    repository.upsert_river_timeseries(rows, batch_size=self.config.batch_size)
                    repository.insert_qc_result(qc_record)
                    repository.mark_run_parsed(context.run_id)
            else:
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
            self._mark_run_failed_preserving_error(context.run_id, error.error_code, error.message)
            raise
        except OSError as error:
            self._mark_run_failed_preserving_error(
                context.run_id,
                "OUTPUT_PARSE_OS_ERROR",
                _runtime_error_message(error),
            )
            raise
        except Exception as error:
            self._mark_run_failed_preserving_error(
                context.run_id,
                "OUTPUT_PARSE_RUNTIME_ERROR",
                _runtime_error_message(error),
            )
            raise

    def _mark_run_failed_preserving_error(self, run_id: str, error_code: str, error_message: str) -> None:
        try:
            self.repository.mark_run_failed(run_id, error_code, error_message)
        except Exception:
            LOGGER.exception(
                "Failed to mark hydro_run %s failed after output parsing error %s; preserving original error.",
                run_id,
                error_code,
            )

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


@dataclass
class FileOutputParserRepository:
    config: OutputParserConfig
    object_store: LocalObjectStore
    _manifest_by_run_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    _manifest_by_river_network: dict[str, dict[str, Any]] = field(default_factory=dict)
    _last_rows_written: dict[str, int] = field(default_factory=dict)
    _last_qc_uri: dict[str, str] = field(default_factory=dict)
    _last_timeseries_uri: dict[str, str] = field(default_factory=dict)

    def load_run_context(self, run_id: str) -> HydroRunContext:
        manifest = self._load_run_manifest(run_id)
        identity = _mapping(manifest.get("identity"))
        model = _mapping(manifest.get("model"))
        outputs = _mapping(manifest.get("outputs"))
        run_type = str(manifest.get("run_type") or "forecast")
        cycle_time_value = manifest.get("cycle_time") or identity.get("cycle_time")
        cycle_time = _parse_time(cycle_time_value) if cycle_time_value not in (None, "") else None
        if cycle_time is None and run_type != "analysis":
            raise OutputParsingError("CYCLE_TIME_MISSING", f"hydro_run {run_id} has no cycle_time.")
        context = HydroRunContext(
            run_id=str(manifest.get("run_id") or identity.get("run_id") or run_id),
            model_id=str(model.get("model_id") or identity.get("model_id") or ""),
            basin_version_id=str(model.get("basin_version_id") or identity.get("basin_version_id") or ""),
            river_network_version_id=str(
                model.get("river_network_version_id") or identity.get("river_network_version_id") or ""
            ),
            source_id=manifest.get("source_id") or identity.get("source_id") or identity.get("source"),
            cycle_id=identity.get("cycle_id"),
            cycle_time=cycle_time,
            start_time=_parse_time(manifest.get("start_time") or identity.get("start_time")),
            output_uri=outputs.get("output_uri") or manifest.get("output_uri"),
            run_type=run_type,
            scenario_id=manifest.get("scenario_id") or identity.get("scenario_id"),
        )
        for field_name, value in (
            ("model_id", context.model_id),
            ("basin_version_id", context.basin_version_id),
            ("river_network_version_id", context.river_network_version_id),
        ):
            if value == "":
                raise OutputParsingError("RUN_CONTEXT_INCOMPLETE", f"DB-free run manifest is missing {field_name}.")
        self._manifest_by_run_id[context.run_id] = manifest
        self._manifest_by_river_network[context.river_network_version_id] = manifest
        return context

    def load_river_segments(self, river_network_version_id: str) -> tuple[RiverSegmentOrder, ...]:
        manifest = self._manifest_by_river_network.get(river_network_version_id)
        if manifest is None:
            raise OutputParsingError(
                "RUN_CONTEXT_MISSING",
                "DB-free output parser must load run context before river segments.",
            )
        model = _mapping(manifest.get("model"))
        identity = _mapping(manifest.get("identity"))
        model_id = str(model.get("model_id") or identity.get("model_id") or "")
        model_package_uri = str(model.get("model_package_uri") or identity.get("model_package_uri") or "")
        project_name = str(model.get("project_name") or model.get("shud_input_name") or "")
        if not model_id or not model_package_uri:
            raise OutputParsingError(
                "MODEL_PACKAGE_CONTEXT_MISSING",
                "DB-free output parser requires model_id and model_package_uri in the run manifest.",
            )
        riv_path = self._resolve_model_riv_path(model_package_uri, project_name)
        return tuple(
            RiverSegmentOrder(
                river_segment_id=f"{model_id}_shud_riv_{index:06d}",
                river_network_version_id=river_network_version_id,
                segment_order=index,
            )
            for index in _read_shud_riv_indices(riv_path)
        )

    def upsert_river_timeseries(self, rows: tuple[RiverTimeseriesRow, ...], *, batch_size: int) -> None:
        del batch_size
        if not rows:
            return
        run_id = rows[0].run_id
        payload = "\n".join(json.dumps(_timeseries_row_payload(row), sort_keys=True) for row in rows) + "\n"
        uri = self.object_store.write_bytes_atomic(f"runs/{run_id}/output/parsed/q_down.jsonl", payload.encode("utf-8"))
        self._last_rows_written[run_id] = len(rows)
        self._last_timeseries_uri[run_id] = uri

    def insert_qc_result(self, record: QCResultRecord) -> dict[str, Any]:
        payload = _qc_result_payload(record)
        uri = self.object_store.write_bytes_atomic(
            f"runs/{record.run_id}/output/parsed/qc_result.json",
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
        )
        self._last_qc_uri[record.run_id] = uri
        return {"uri": uri, **payload}

    def mark_run_parsed(self, run_id: str) -> dict[str, Any]:
        payload = {
            "run_id": run_id,
            "status": "parsed",
            "rows_written": self._last_rows_written.get(run_id, 0),
            "timeseries_uri": self._last_timeseries_uri.get(run_id),
            "qc_result_uri": self._last_qc_uri.get(run_id),
            "parsed_at": _format_time(datetime.now(UTC)),
            "repository": "file",
        }
        uri = self.object_store.write_bytes_atomic(
            f"runs/{run_id}/output/parsed/parse_result.json",
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
        )
        return {"uri": uri, **payload}

    def mark_run_failed(self, run_id: str, error_code: str, error_message: str) -> dict[str, Any]:
        payload = {
            "run_id": run_id,
            "status": "failed",
            "error_code": error_code,
            "error_message": error_message,
            "failed_at": _format_time(datetime.now(UTC)),
            "repository": "file",
        }
        uri = self.object_store.write_bytes_atomic(
            f"runs/{run_id}/output/parsed/parse_result.json",
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
        )
        return {"uri": uri, **payload}

    @contextmanager
    def transaction(self) -> Iterator[FileOutputParserRepository]:
        yield self

    def _load_run_manifest(self, run_id: str) -> dict[str, Any]:
        try:
            payload = json.loads(self.object_store.read_bytes(f"runs/{run_id}/input/manifest.json").decode("utf-8"))
        except Exception:
            payload = None
        if isinstance(payload, dict):
            return payload
        workspace_root = self.config.workspace_root
        if workspace_root is not None:
            path = Path(workspace_root) / "runs" / run_id / "input" / "manifest.json"
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
        raise OutputParsingError("RUN_MANIFEST_NOT_FOUND", f"DB-free run manifest not found for {run_id}.")

    def _resolve_model_riv_path(self, model_package_uri: str, project_name: str) -> Path:
        package_path = self.object_store.resolve_path(model_package_uri)
        if not package_path.exists() or not package_path.is_dir():
            raise OutputParsingError("MODEL_PACKAGE_NOT_FOUND", f"model package not found: {model_package_uri}")
        candidates: list[Path] = []
        if project_name:
            candidates.append(package_path / f"{project_name}.sp.riv")
        candidates.extend(sorted(package_path.glob("*.sp.riv")))
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        raise OutputParsingError("MODEL_RIVER_FILE_NOT_FOUND", f"No *.sp.riv file found under {model_package_uri}")


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

    time_basis = _rivqdown_time_basis(lines, context.start_time, context=context)
    data_lines = _rivqdown_data_lines(lines, context.start_time)
    if not data_lines:
        raise OutputParsingError("RIVQDOWN_NO_DATA", f".rivqdown file contains no data rows: {path}")

    rows: list[RiverTimeseriesRow] = []
    expected_columns: int | None = None
    for row_offset, line in enumerate(data_lines):
        line_number = row_offset + 1
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

        valid_time = _parse_valid_time(tokens[0], context.start_time, line_number, numeric_unit=time_basis)
        # "auto" is retained for legacy callers: values more than a leap-year window after
        # run start are interpreted as absolute Unix minutes from the 1970 UTC epoch.
        if time_basis == "auto" and _ensure_utc(valid_time) > _ensure_utc(context.start_time) + timedelta(
            days=AUTO_TIME_BASIS_MAX_RELATIVE_DAYS
        ):
            valid_time = _parse_valid_time(
                tokens[0],
                UNIX_EPOCH_UTC,
                line_number,
                numeric_unit="absolute_unix_minutes",
            )
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
    connect_timeout_seconds: int = DEFAULT_DB_CONNECT_TIMEOUT_SECONDS
    statement_timeout_ms: int = DEFAULT_DB_STATEMENT_TIMEOUT_MS
    _connection: Any | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_env(cls) -> PsycopgOutputParserRepository:
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise OutputParsingError("DATABASE_URL_MISSING", "DATABASE_URL is required for output parsing.")
        return cls(
            database_url,
            connect_timeout_seconds=int(
                os.getenv("OUTPUT_PARSER_DB_CONNECT_TIMEOUT_SECONDS", str(DEFAULT_DB_CONNECT_TIMEOUT_SECONDS))
            ),
            statement_timeout_ms=int(
                os.getenv("OUTPUT_PARSER_DB_STATEMENT_TIMEOUT_MS", str(DEFAULT_DB_STATEMENT_TIMEOUT_MS))
            ),
        )

    @contextmanager
    def transaction(self) -> Iterator[PsycopgOutputParserRepository]:
        if self._connection is not None:
            yield self
            return

        connection = self._connect()
        connection.autocommit = False
        try:
            yield replace(self, _connection=connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
              AND COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
            ORDER BY segment_order NULLS LAST, river_segment_id
            """,
            (river_network_version_id,),
        )
        if not rows:
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
        replacement_keys = sorted(
            {(row.run_id, row.river_network_version_id, row.variable) for row in rows}
        )
        if self._connection is None:
            with self.transaction() as repository:
                repository.upsert_river_timeseries(rows, batch_size=batch_size)
            return
        for run_id, river_network_version_id, variable in replacement_keys:
            self._fetch_all(
                """
                DELETE FROM hydro.river_timeseries
                WHERE run_id = %s
                  AND river_network_version_id = %s
                  AND variable = %s
                """,
                (run_id, river_network_version_id, variable),
            )
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

        if self._connection is None:
            with self.transaction() as repository:
                return repository.insert_qc_result(record)

        self._fetch_all(
            """
            SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))
            """,
            (_qc_result_lock_key(record),),
        )
        row = self._fetch_optional(
            """
            UPDATE ops.qc_result
            SET cycle_id = %s,
                passed = %s,
                severity = %s,
                checks_json = %s,
                message = %s,
                created_at = now()
            WHERE qc_checkpoint = %s
              AND target_type = %s
              AND target_id = %s
              AND run_id IS NOT DISTINCT FROM %s
            RETURNING *
            """,
            (
                record.cycle_id,
                record.passed,
                record.severity,
                Json(record.checks_json),
                record.message,
                record.qc_checkpoint,
                record.target_type,
                record.target_id,
                record.run_id,
            ),
        )
        if row is not None:
            return row

        row = self._fetch_optional(
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
            ON CONFLICT DO NOTHING
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
        if row is not None:
            return row
        return self._fetch_one(
            """
            SELECT *
            FROM ops.qc_result
            WHERE qc_checkpoint = %s
              AND target_type = %s
              AND target_id = %s
              AND run_id IS NOT DISTINCT FROM %s
            ORDER BY created_at DESC, qc_id DESC
            LIMIT 1
            """,
            (record.qc_checkpoint, record.target_type, record.target_id, record.run_id),
        )

    def mark_run_parsed(self, run_id: str) -> dict[str, Any]:
        row = self._fetch_optional(
            """
            UPDATE hydro.hydro_run
            SET status = 'parsed',
                error_code = NULL,
                error_message = NULL,
                updated_at = now()
            WHERE run_id = %s
              AND status IN %s
            RETURNING *
            """,
            (run_id, PARSE_READY_RUN_STATUSES),
        )
        if row is not None:
            return row
        return self._terminal_state_or_missing_row(run_id)

    def mark_run_failed(self, run_id: str, error_code: str, error_message: str) -> dict[str, Any]:
        row = self._fetch_optional(
            """
            UPDATE hydro.hydro_run
            SET status = 'failed',
                error_code = %s,
                error_message = %s,
                updated_at = now()
            WHERE run_id = %s
              AND status IN %s
            RETURNING *
            """,
            (error_code, error_message, run_id, FAILABLE_RUN_STATUSES),
        )
        if row is not None:
            return row
        return self._terminal_state_or_missing_row(run_id)

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

    def _fetch_optional(self, statement: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
        rows = self._fetch_all(statement, parameters)
        return rows[0] if rows else None

    def _terminal_state_or_missing_row(self, run_id: str) -> dict[str, Any]:
        return self._fetch_one(
            """
            SELECT *
            FROM hydro.hydro_run
            WHERE run_id = %s
            """,
            (run_id,),
            missing_code="DATABASE_ROW_MISSING",
            missing_message=f"hydro_run not found: {run_id}",
        )

    def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        connection = self._connection
        owns_connection = connection is None
        try:
            if connection is None:
                connection = self._connect()
                connection.autocommit = False
            with connection.cursor() as cursor:
                cursor.execute(statement, parameters)
                if cursor.description is None:
                    if owns_connection:
                        connection.commit()
                    return []
                rows = cursor.fetchall()
                columns = [description.name for description in cursor.description]
                if owns_connection:
                    connection.commit()
                return [dict(zip(columns, row, strict=True)) for row in rows]
        except self._psycopg_error() as error:
            if owns_connection and connection is not None:
                connection.rollback()
            raise OutputParsingError(
                "OUTPUT_PARSE_DB_ERROR",
                f"Output parser database operation failed: {error}",
            ) from error
        finally:
            if owns_connection and connection is not None:
                connection.close()

    def _execute_values(
        self,
        statement: str,
        rows: list[tuple[Any, ...]],
        *,
        page_size: int,
    ) -> None:
        try:
            from psycopg2.extras import execute_values
        except ImportError as error:
            raise OutputParsingError("PSYCOPG2_MISSING", "psycopg2 is required for output parsing.") from error

        connection = self._connection
        owns_connection = connection is None
        try:
            if connection is None:
                connection = self._connect()
                connection.autocommit = False
            with connection.cursor() as cursor:
                execute_values(cursor, statement, rows, page_size=page_size)
            if owns_connection:
                connection.commit()
        except self._psycopg_error() as error:
            if owns_connection and connection is not None:
                connection.rollback()
            raise OutputParsingError(
                "OUTPUT_PARSE_DB_ERROR",
                f"Output parser database operation failed: {error}",
            ) from error
        finally:
            if owns_connection and connection is not None:
                connection.close()

    def _connect(self) -> Any:
        try:
            import psycopg2
        except ImportError as error:
            raise OutputParsingError("PSYCOPG2_MISSING", "psycopg2 is required for output parsing.") from error

        return psycopg2.connect(
            self.database_url,
            connect_timeout=self.connect_timeout_seconds,
            options=f"-c statement_timeout={self.statement_timeout_ms}",
        )

    @staticmethod
    def _psycopg_error() -> type[Exception]:
        try:
            import psycopg2
        except ImportError as error:
            raise OutputParsingError("PSYCOPG2_MISSING", "psycopg2 is required for output parsing.") from error
        return psycopg2.Error


def _split_row(line: str) -> list[str]:
    if "," in line:
        return [token.strip() for token in next(csv.reader([line]))]
    return line.split()


def _looks_like_header(tokens: list[str], start_time: datetime) -> bool:
    if not tokens:
        return True
    try:
        _parse_time_token(tokens[0], start_time, numeric_unit="minutes")
    except OutputParsingError:
        return True
    return False


def _rivqdown_data_lines(lines: list[str], start_time: datetime) -> list[str]:
    candidate_lines = [line for line in lines if not line.lstrip().startswith("#")]
    if candidate_lines:
        first_tokens = _split_row(candidate_lines[0])
        if _looks_like_header(first_tokens, start_time):
            return candidate_lines[1:]
    data_lines: list[str] = []
    start_index = 0
    if len(candidate_lines) >= 2:
        first_tokens = _split_row(candidate_lines[0])
        second_tokens = _split_row(candidate_lines[1])
        if first_tokens and all(_is_float(token) for token in first_tokens) and any(
            not _is_float(token) for token in second_tokens
        ):
            start_index = 2
    for line in candidate_lines[start_index:]:
        tokens = _split_row(line)
        if len(tokens) < 2:
            continue
        if _looks_like_shud_metadata_row(tokens):
            continue
        if not _is_float(tokens[0]):
            continue
        if any(not _is_float(token) for token in tokens[1:]):
            continue
        data_lines.append(line)
    return data_lines


def _rivqdown_time_basis(lines: list[str], start_time: datetime, *, context: HydroRunContext) -> str:
    joined = "\n".join(lines[:10]).lower()
    if "unix minute" in joined or "unix_min" in joined:
        return "absolute_unix_minutes"
    data_lines = _rivqdown_data_lines(lines, start_time)
    if data_lines:
        first_token = _split_row(data_lines[0])[0]
        if _is_float(first_token):
            absolute_time = _parse_time_token(first_token, UNIX_EPOCH_UTC, numeric_unit="minutes")
            if _absolute_time_matches_context(absolute_time, context):
                return "absolute_unix_minutes"
    return "minutes"


def _absolute_time_matches_context(valid_time: datetime, context: HydroRunContext) -> bool:
    candidate = _ensure_utc(valid_time)
    start_time = _ensure_utc(context.start_time)
    if context.run_type == "analysis":
        return (
            start_time - timedelta(days=AUTO_TIME_BASIS_CONTEXT_PADDING_DAYS)
            <= candidate
            <= start_time + timedelta(days=AUTO_TIME_BASIS_MAX_RELATIVE_DAYS)
        )
    if context.cycle_time is not None:
        cycle_time = _ensure_utc(context.cycle_time)
        return (
            cycle_time - timedelta(days=AUTO_TIME_BASIS_CONTEXT_PADDING_DAYS)
            <= candidate
            <= cycle_time + timedelta(days=AUTO_TIME_BASIS_MAX_RELATIVE_DAYS)
        )
    return (
        start_time - timedelta(days=AUTO_TIME_BASIS_CONTEXT_PADDING_DAYS)
        <= candidate
        <= start_time + timedelta(days=AUTO_TIME_BASIS_MAX_RELATIVE_DAYS)
    )


def _looks_like_shud_metadata_row(tokens: list[str]) -> bool:
    return len(tokens) == 3 and tokens[0] == "0" and tokens[2].isdigit() and len(tokens[2]) == 8


def _parse_valid_time(
    token: str,
    start_time: datetime,
    line_number: int,
    *,
    numeric_unit: str = "minutes",
) -> datetime:
    try:
        return _parse_time_token(token, start_time, numeric_unit=numeric_unit)
    except OutputParsingError as error:
        raise OutputParsingError("INVALID_TIME_VALUE", f"Row {line_number} has invalid time value {token!r}") from error


def _parse_time_token(token: str, start_time: datetime, *, numeric_unit: str = "minutes") -> datetime:
    candidate = token.strip()
    if _is_float(candidate):
        value = float(candidate)
        if numeric_unit == "days":
            return _ensure_utc(start_time) + timedelta(days=value)
        if numeric_unit == "absolute_unix_minutes":
            return UNIX_EPOCH_UTC + timedelta(minutes=value)
        return _ensure_utc(start_time) + timedelta(minutes=value)

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


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if value in (None, ""):
        raise OutputParsingError("TIME_MISSING", "Required time value is missing from DB-free run manifest.")
    try:
        return _ensure_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError as error:
        raise OutputParsingError("INVALID_TIME_VALUE", f"Invalid manifest time value: {value!r}") from error


def _read_shud_riv_indices(path: Path) -> tuple[int, ...]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) < 3:
        raise OutputParsingError("MODEL_RIVER_FILE_EMPTY", f"SHUD river file is empty or incomplete: {path}")
    header_tokens = _split_row(lines[0])
    try:
        expected_count = int(header_tokens[0])
    except (IndexError, ValueError) as error:
        raise OutputParsingError(
            "MODEL_RIVER_FILE_MALFORMED",
            f"Invalid SHUD river file count header: {lines[0]!r}",
        ) from error
    if expected_count <= 0:
        raise OutputParsingError("MODEL_RIVER_FILE_MALFORMED", f"SHUD river count must be positive: {expected_count}")
    data_lines = lines[2 : 2 + expected_count]
    if len(data_lines) != expected_count:
        raise OutputParsingError(
            "MODEL_RIVER_FILE_MALFORMED",
            f"SHUD river file declares {expected_count} rows but only {len(data_lines)} were found.",
        )
    indices: list[int] = []
    for line_number, line in enumerate(data_lines, start=3):
        tokens = _split_row(line)
        token = tokens[0] if tokens else ""
        try:
            index = int(token)
        except ValueError as error:
            raise OutputParsingError(
                "MODEL_RIVER_FILE_MALFORMED",
                f"Invalid SHUD river index on line {line_number}: {token!r}",
            ) from error
        if index <= 0:
            raise OutputParsingError("MODEL_RIVER_FILE_MALFORMED", f"SHUD river index must be positive: {index}")
        indices.append(index)
    if not indices:
        raise OutputParsingError("MODEL_RIVER_FILE_EMPTY", f"No SHUD river rows found in {path}")
    return tuple(indices)


def _timeseries_row_payload(row: RiverTimeseriesRow) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "basin_version_id": row.basin_version_id,
        "river_network_version_id": row.river_network_version_id,
        "river_segment_id": row.river_segment_id,
        "valid_time": _format_time(row.valid_time),
        "lead_time_hours": row.lead_time_hours,
        "variable": row.variable,
        "value": row.value,
        "unit": row.unit,
        "quality_flag": row.quality_flag,
    }


def _qc_result_payload(record: QCResultRecord) -> dict[str, Any]:
    return {
        "qc_checkpoint": record.qc_checkpoint,
        "target_type": record.target_type,
        "target_id": record.target_id,
        "run_id": record.run_id,
        "cycle_id": record.cycle_id,
        "passed": record.passed,
        "severity": record.severity,
        "checks_json": record.checks_json,
        "message": record.message,
    }


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _db_free_output_parser_enabled() -> bool:
    return _env_flag("NHMS_OUTPUT_PARSER_DB_FREE") or _env_flag("NHMS_SCHEDULER_DB_FREE_REQUIRED")


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


def _runtime_error_message(error: BaseException) -> str:
    message = str(error).strip()
    return message or error.__class__.__name__


def _qc_result_lock_key(record: QCResultRecord) -> str:
    return "\x1f".join(
        (
            record.qc_checkpoint,
            record.target_type,
            record.target_id,
            record.run_id,
        )
    )


def _is_rivqdown_path(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith((".rivqdown", ".rivqdown.csv", ".rivqdown.dat")) or name in {
        "rivqdown.csv",
        "rivqdown.dat",
    }


def _resolve_object_path_allowing_directory(object_store: LocalObjectStore, key_or_uri: str) -> Path:
    try:
        key = object_store.normalize_key(key_or_uri)
    except ValueError as error:
        raise OutputParsingError("OUTPUT_URI_INVALID", str(error)) from error
    validation = validate_object_path(f"{key.rstrip('/')}/_directory_probe")
    if not validation.valid:
        raise OutputParsingError("OUTPUT_URI_INVALID", str(validation.error))
    root = Path(object_store.root)
    target = (root / key).resolve()
    try:
        target.relative_to(root)
    except ValueError as error:
        raise OutputParsingError("OUTPUT_URI_INVALID", f"Object key escapes object store root: {key_or_uri}") from error
    return target
