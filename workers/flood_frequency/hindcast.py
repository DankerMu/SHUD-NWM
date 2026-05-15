from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from packages.common.source_identity import normalize_source_id
from services.orchestrator.chain import HttpSlurmGatewayClient
from services.orchestrator.persistence import PipelineStore
from workers.flood_frequency.config import HindcastConfig

HINDCAST_SCENARIO_ID = "hindcast_replay"
INSUFFICIENT_ERA5_COVERAGE = "INSUFFICIENT_ERA5_COVERAGE"
HINDCAST_FORCING_PACKAGE_UNAVAILABLE = "HINDCAST_FORCING_PACKAGE_UNAVAILABLE"
TERMINAL_SUCCESS_STATUSES = {"succeeded", "parsed", "frequency_done", "published", "complete"}
ACTIVE_HINDCAST_STATUSES = {"created", "submitted", "running", "staged"}
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
LOGGER = logging.getLogger(__name__)


class HindcastError(RuntimeError):
    def __init__(self, error_code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class HindcastSubmitResult:
    total_runs: int
    run_ids: list[str]
    skipped_years: list[int]
    active_years: list[int]


@dataclass(frozen=True)
class HindcastForcingResult:
    forcing_version_id: str
    coverage: float
    missing_rate: float
    start_time: datetime
    end_time: datetime
    forcing_package_uri: str | None = None


@dataclass(frozen=True)
class Era5CoverageResult:
    coverage: float
    expected_pairs: int
    actual_pairs: int
    expected_hours: int
    required_variables: tuple[str, ...]
    missing_variables: list[str]
    missing_by_variable: dict[str, int]


@dataclass(frozen=True)
class HindcastYearResult:
    run_id: str
    forcing_version_id: str
    status: str
    shud_result: dict[str, Any]
    parse_result: dict[str, Any]


@dataclass(frozen=True)
class HindcastSlurmResult:
    slurm_job_array_id: str | None
    job_ids: list[str]


def run_id_for_year(model_id: str, year: int) -> str:
    _validate_model_id(model_id)
    return f"hindcast_era5_{model_id}_{int(year)}"


def forcing_version_id_for_year(model_id: str, year: int) -> str:
    _validate_model_id(model_id)
    return f"forc_era5_hindcast_{model_id}_{int(year)}"


def calendar_years(start_time: str | datetime, end_time: str | datetime) -> list[int]:
    start = _parse_time(start_time)
    end = _parse_time(end_time)
    if start >= end:
        raise HindcastError(
            "INVALID_TIME_RANGE",
            "start_time must be earlier than end_time.",
            {"start_time": start.isoformat(), "end_time": end.isoformat()},
        )
    return list(range(start.year, end.year + 1))


def submit_hindcast(
    model_id: str,
    source_id: str,
    start_time: str | datetime,
    end_time: str | datetime,
    purpose: str,
    db_session: Session,
) -> HindcastSubmitResult:
    _validate_model_id(model_id)
    years = calendar_years(start_time, end_time)
    source_id = normalize_source_id(source_id)
    model = _load_model_context(db_session, model_id)
    run_ids: list[str] = []
    skipped_years: list[int] = []
    active_years: list[int] = []

    try:
        for year in years:
            run_id = run_id_for_year(model_id, year)
            existing = db_session.execute(
                text("SELECT status FROM hydro.hydro_run WHERE run_id = :run_id"),
                {"run_id": run_id},
            ).mappings().first()
            if existing is not None:
                existing_status = str(existing["status"])
                if existing_status in TERMINAL_SUCCESS_STATUSES:
                    skipped_years.append(year)
                    continue
                if existing_status in ACTIVE_HINDCAST_STATUSES:
                    active_years.append(year)
                    continue

            year_start, year_end = _year_bounds(year)
            db_session.execute(
                text(
                    """
                    INSERT INTO hydro.hydro_run (
                        run_id,
                        run_type,
                        scenario_id,
                        model_id,
                        basin_version_id,
                        source_id,
                        cycle_time,
                        start_time,
                        end_time,
                        status,
                        run_manifest_uri,
                        output_uri,
                        log_uri,
                        error_code,
                        error_message
                    )
                    VALUES (
                        :run_id,
                        'hindcast',
                        :scenario_id,
                        :model_id,
                        :basin_version_id,
                        :source_id,
                        :cycle_time,
                        :start_time,
                        :end_time,
                        'created',
                        :run_manifest_uri,
                        :output_uri,
                        :log_uri,
                        NULL,
                        NULL
                    )
                    ON CONFLICT (run_id) DO UPDATE SET
                        status = 'created',
                        source_id = EXCLUDED.source_id,
                        cycle_time = EXCLUDED.cycle_time,
                        start_time = EXCLUDED.start_time,
                        end_time = EXCLUDED.end_time,
                        run_manifest_uri = EXCLUDED.run_manifest_uri,
                        output_uri = EXCLUDED.output_uri,
                        log_uri = EXCLUDED.log_uri,
                        error_code = NULL,
                        error_message = NULL
                    """
                ),
                {
                    "run_id": run_id,
                    "scenario_id": HINDCAST_SCENARIO_ID,
                    "model_id": model_id,
                    "basin_version_id": model["basin_version_id"],
                    "source_id": source_id,
                    "cycle_time": year_start,
                    "start_time": year_start,
                    "end_time": year_end,
                    "run_manifest_uri": _run_manifest_uri(run_id),
                    "output_uri": _run_output_uri(run_id),
                    "log_uri": _run_log_uri(run_id),
                },
            )
            run_ids.append(run_id)
        db_session.commit()
    except SQLAlchemyError as error:
        db_session.rollback()
        raise HindcastError("HINDCAST_SUBMIT_DB_ERROR", f"Failed to create hindcast runs: {error}") from error

    return HindcastSubmitResult(
        total_runs=len(run_ids),
        run_ids=run_ids,
        skipped_years=skipped_years,
        active_years=active_years,
    )


def produce_hindcast_forcing(
    model_id: str,
    source_id: str,
    year: int,
    db_session: Session,
) -> HindcastForcingResult:
    _validate_model_id(model_id)
    source_id = normalize_source_id(source_id)
    if source_id != "ERA5":
        raise HindcastError("UNSUPPORTED_SOURCE", "Hindcast replay currently supports ERA5 only.")
    model = _load_model_context(db_session, model_id)
    start_time, end_time = _year_bounds(year)
    coverage_result = _era5_coverage(
        db_session,
        source_id=source_id,
        start_time=start_time,
        end_time=end_time,
        required_variables=HindcastConfig.from_env().era5_required_variables,
    )
    coverage = coverage_result.coverage
    missing_rate = 1.0 - coverage
    quality_flag = _coverage_quality_flag(coverage_result)
    forcing_version_id = forcing_version_id_for_year(model_id, year)
    forcing_package_uri = _hindcast_forcing_package_uri(forcing_version_id)
    run_id = run_id_for_year(model_id, year)

    if missing_rate > 0.10:
        _record_qc_result(
            db_session,
            target_id=forcing_version_id,
            run_id=run_id,
            passed=False,
            checks_json=_coverage_checks_json(coverage_result, quality_flag="insufficient_era5_coverage"),
            message="ERA5 canonical coverage is below the 90% hindcast threshold.",
        )
        db_session.commit()
        raise HindcastError(
            INSUFFICIENT_ERA5_COVERAGE,
            "ERA5 canonical coverage is below the 90% hindcast threshold.",
            {
                "coverage": coverage,
                "missing_rate": missing_rate,
                "year": int(year),
                "missing_variables": coverage_result.missing_variables,
                "missing_by_variable": coverage_result.missing_by_variable,
            },
        )

    production_result = _produce_forcing_package_with_producer(
        model_id=model_id,
        source_id=source_id,
        start_time=start_time,
        end_time=end_time,
        db_session=db_session,
    )

    try:
        if production_result is not None:
            forcing_version_id = str(production_result.forcing_version_id)
            forcing_package_uri = str(production_result.forcing_package_uri)
        else:
            forcing_package_uri = ""
            station_count = _station_count(db_session, model_id)
            lineage = {
                "purpose": "hindcast",
                "year": int(year),
                "quality_flag": quality_flag,
                "metadata_only_reason": "forcing_producer_unavailable",
            }
            _upsert_metadata_only_forcing_version(
                db_session,
                forcing_version_id=forcing_version_id,
                model_id=model_id,
                source_id=source_id,
                start_time=start_time,
                end_time=end_time,
                station_count=station_count,
                forcing_package_uri=forcing_package_uri,
                checksum=f"hindcast-{model['river_network_version_id']}-{year}",
                lineage=lineage,
            )
        _record_qc_result(
            db_session,
            target_id=forcing_version_id,
            run_id=run_id,
            passed=True,
            checks_json=_coverage_checks_json(coverage_result, quality_flag=quality_flag),
            message="ERA5 canonical coverage satisfies hindcast threshold.",
        )
        db_session.commit()
    except SQLAlchemyError as error:
        db_session.rollback()
        raise HindcastError("HINDCAST_FORCING_DB_ERROR", f"Failed to save hindcast forcing: {error}") from error

    return HindcastForcingResult(
        forcing_version_id=forcing_version_id,
        coverage=coverage,
        missing_rate=missing_rate,
        start_time=start_time,
        end_time=end_time,
        forcing_package_uri=forcing_package_uri,
    )


def hindcast_year(
    model_id: str,
    source_id: str,
    year: int,
    db_session: Session,
) -> HindcastYearResult:
    _validate_model_id(model_id)
    run_id = run_id_for_year(model_id, year)
    forcing_version_id = forcing_version_id_for_year(model_id, year)
    try:
        _update_hydro_run(
            db_session,
            run_id,
            status="running",
            error_code=None,
            error_message=None,
        )
        forcing = produce_hindcast_forcing(model_id, source_id, year, db_session)
        forcing_version_id = forcing.forcing_version_id
        _set_run_forcing(db_session, run_id, forcing_version_id, forcing.forcing_package_uri)
        shud_result = run_shud_hindcast(run_id, model_id, source_id, year, db_session)
        parse_result = parse_hindcast_output(run_id)
        _update_hydro_run(db_session, run_id, status="parsed", error_code=None, error_message=None)
        db_session.commit()
        return HindcastYearResult(run_id, forcing_version_id, "parsed", shud_result, parse_result)
    except HindcastError as error:
        db_session.rollback()
        _mark_run_failed(db_session, run_id, error.error_code, error.message)
        raise
    except Exception as error:
        db_session.rollback()
        _mark_run_failed(db_session, run_id, "HINDCAST_YEAR_FAILED", str(error))
        raise HindcastError("HINDCAST_YEAR_FAILED", str(error)) from error


def run_shud_hindcast(
    run_id: str,
    model_id: str,
    source_id: str,
    year: int,
    db_session: Session,
) -> dict[str, Any]:
    from workers.shud_runtime.runtime import SHUDRuntime

    manifest_path = _write_hindcast_manifest(run_id, model_id, source_id, year, db_session)
    result = SHUDRuntime.from_env().execute_manifest_path(str(manifest_path))
    return {
        "run_id": result.run_id,
        "status": result.status,
        "output_uri": result.output_uri,
        "log_uri": result.log_uri,
    }


def parse_hindcast_output(run_id: str) -> dict[str, Any]:
    from workers.output_parser.parser import OutputParser

    result = OutputParser.from_env().parse_run(run_id)
    return {
        "run_id": result.run_id,
        "status": result.status,
        "rows_written": result.rows_written,
        "qc_passed": result.qc_passed,
    }


def submit_hindcast_slurm(
    model_id: str,
    source_id: str,
    years: Sequence[int],
    config: HindcastConfig,
    basin_version_id: str | None = None,
    river_network_version_id: str | None = None,
) -> HindcastSlurmResult:
    _validate_model_id(model_id)
    years = [int(year) for year in years]
    if not years:
        return HindcastSlurmResult(slurm_job_array_id=None, job_ids=[])
    basin_version_id, river_network_version_id = _load_model_versions_for_slurm(
        config.db_session,
        model_id,
        basin_version_id=basin_version_id,
        river_network_version_id=river_network_version_id,
    )

    slurm_client = config.slurm_client or HttpSlurmGatewayClient(config.slurm_gateway_url)
    tasks = [
        {
            "array_task_id": index,
            "run_id": run_id_for_year(model_id, year),
            "model_id": model_id,
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
            "source_id": normalize_source_id(source_id),
            "year": year,
            "cycle_time": f"{year}-01-01T00:00:00Z",
            "forcing_version_id": forcing_version_id_for_year(model_id, year),
            "forcing_package_uri": "",
            "object_store_root": str(config.object_store_root),
            "object_store_prefix": config.object_store_prefix,
            "workspace_dir": str(config.workspace_root),
            "workspace_root": str(config.workspace_root),
        }
        for index, year in enumerate(years)
    ]
    payload = {
        "job_type": "hindcast",
        "cycle_id": f"hindcast_{model_id}_{years[0]}_{years[-1]}",
        "stage_name": "hindcast",
        "manifest": {
            "run_id": f"hindcast_era5_{model_id}",
            "model_id": model_id,
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
            "source_id": normalize_source_id(source_id),
            "years": years,
            "object_store_root": str(config.object_store_root),
            "object_store_prefix": config.object_store_prefix,
            "workspace_dir": str(config.workspace_root),
            "workspace_root": str(config.workspace_root),
        },
        "tasks": tasks,
    }
    submit_job_array = getattr(slurm_client, "submit_job_array", None)
    if callable(submit_job_array):
        submitted = submit_job_array(payload)
    else:
        submitted = slurm_client.submit_job(payload)
    submitted = _mapping(submitted)
    slurm_job_id = str(submitted.get("job_id") or submitted.get("slurm_job_id"))

    session = config.db_session
    job_ids: list[str] = []
    if session is not None:
        store = PipelineStore(session)
        for task in tasks:
            task_id = int(task["array_task_id"])
            job_id = f"{task['run_id']}_hindcast_{task_id}"
            job_ids.append(job_id)
            job = store.create_job(
                job_id=job_id,
                run_id=str(task["run_id"]),
                cycle_id=str(payload["cycle_id"]),
                job_type="hindcast",
                slurm_job_id=slurm_job_id,
                model_id=model_id,
                stage="hindcast",
                status=str(submitted.get("status") or "submitted"),
                commit=False,
            )
            if hasattr(job, "array_task_id"):
                job.array_task_id = task_id
        session.commit()

    return HindcastSlurmResult(slurm_job_array_id=slurm_job_id, job_ids=job_ids)


def hindcast_status(model_id: str, db_session: Session) -> list[dict[str, Any]]:
    _validate_model_id(model_id)
    rows = db_session.execute(
        text(
            """
            SELECT run_id, status, start_time, end_time, error_code, error_message, updated_at
            FROM hydro.hydro_run
            WHERE run_type = 'hindcast'
              AND scenario_id = :scenario_id
              AND model_id = :model_id
            ORDER BY start_time, run_id
            """
        ),
        {"scenario_id": HINDCAST_SCENARIO_ID, "model_id": model_id},
    ).mappings()
    return [dict(row) for row in rows]


def _parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as error:
            raise HindcastError("INVALID_TIME", f"Invalid ISO datetime: {value}") from error
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _year_bounds(year: int) -> tuple[datetime, datetime]:
    return datetime(int(year), 1, 1, tzinfo=UTC), datetime(int(year) + 1, 1, 1, tzinfo=UTC)


def _load_model_context(db_session: Session, model_id: str) -> dict[str, Any]:
    _validate_model_id(model_id)
    row = db_session.execute(
        text(
            """
            SELECT model_id, basin_version_id, river_network_version_id, model_package_uri
            FROM core.model_instance
            WHERE model_id = :model_id
            LIMIT 1
            """
        ),
        {"model_id": model_id},
    ).mappings().first()
    if row is None:
        raise HindcastError("MODEL_NOT_FOUND", f"Model not found: {model_id}", {"model_id": model_id})
    model = dict(row)
    segment_count = _model_segment_count(db_session, str(model["river_network_version_id"]))
    if segment_count is not None:
        model["segment_count"] = segment_count
    return model


def _era5_coverage(
    db_session: Session,
    *,
    source_id: str,
    start_time: datetime,
    end_time: datetime,
    required_variables: Sequence[str],
) -> Era5CoverageResult:
    expected_hours = int((end_time - start_time).total_seconds() // 3600)
    variables = tuple(dict.fromkeys(str(variable) for variable in required_variables if str(variable)))
    if expected_hours <= 0 or not variables:
        return Era5CoverageResult(0.0, 0, 0, expected_hours, variables, list(variables), {})
    statement = text(
        """
        SELECT variable, COUNT(DISTINCT valid_time) AS available_hours
        FROM met.canonical_met_product
        WHERE source_id = :source_id
          AND valid_time >= :start_time
          AND valid_time < :end_time
          AND variable IN :variables
          AND COALESCE(quality_flag, 'ok') = 'ok'
        GROUP BY variable
        """
    ).bindparams(bindparam("variables", expanding=True))
    rows = db_session.execute(
        statement,
        {"source_id": source_id, "start_time": start_time, "end_time": end_time, "variables": variables},
    ).mappings()
    counts = {str(row["variable"]): min(int(row["available_hours"] or 0), expected_hours) for row in rows}
    expected_pairs = expected_hours * len(variables)
    actual_pairs = sum(counts.get(variable, 0) for variable in variables)
    missing_by_variable = {
        variable: expected_hours - counts.get(variable, 0)
        for variable in variables
        if counts.get(variable, 0) < expected_hours
    }
    missing_variables = [variable for variable in variables if counts.get(variable, 0) == 0]
    return Era5CoverageResult(
        coverage=min(1.0, max(0.0, actual_pairs / expected_pairs)),
        expected_pairs=expected_pairs,
        actual_pairs=actual_pairs,
        expected_hours=expected_hours,
        required_variables=variables,
        missing_variables=missing_variables,
        missing_by_variable=missing_by_variable,
    )


def _coverage_quality_flag(coverage: Era5CoverageResult) -> str:
    if coverage.missing_variables or coverage.missing_by_variable:
        return "incomplete_forcing"
    return "ok"


def _coverage_checks_json(coverage: Era5CoverageResult, *, quality_flag: str | None = None) -> dict[str, Any]:
    resolved_quality_flag = quality_flag or _coverage_quality_flag(coverage)
    return {
        "coverage": coverage.coverage,
        "missing_rate": 1.0 - coverage.coverage,
        "quality_flag": resolved_quality_flag,
        "expected_pairs": coverage.expected_pairs,
        "actual_pairs": coverage.actual_pairs,
        "expected_hours": coverage.expected_hours,
        "required_variables": list(coverage.required_variables),
        "missing_variables": coverage.missing_variables,
        "missing_by_variable": coverage.missing_by_variable,
    }


def _produce_forcing_package_with_producer(
    *,
    model_id: str,
    source_id: str,
    start_time: datetime,
    end_time: datetime,
    db_session: Session,
) -> Any | None:
    bind = db_session.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect_name != "postgresql":
        LOGGER.warning(
            "ForcingProducer is unavailable for %s sessions; using metadata-only hindcast forcing.",
            dialect_name,
        )
        return None
    try:
        from packages.common.object_store import LocalObjectStore
        from workers.forcing_producer import ForcingProducer, ForcingProducerConfig
        from workers.forcing_producer.store import PsycopgForcingRepository
    except ImportError as error:
        LOGGER.warning("ForcingProducer could not be imported; using metadata-only hindcast forcing: %s", error)
        return None

    config = HindcastConfig.from_env()
    expected_hours = int((end_time - start_time).total_seconds() // 3600)
    max_lead_hours = max(expected_hours - 1, 0)
    try:
        producer_config = ForcingProducerConfig(
            source_id=source_id,
            workspace_root=config.object_store_root,
            object_store_prefix=config.object_store_prefix,
            required_canonical_variables=config.era5_required_variables,
        )
        database_url = str(bind.url)
        if database_url.startswith("postgresql+"):
            database_url = f"postgresql://{database_url.split('://', maxsplit=1)[1]}"
        producer = ForcingProducer(
            config=producer_config,
            repository=PsycopgForcingRepository(database_url),
            object_store=LocalObjectStore(config.object_store_root, object_store_prefix=config.object_store_prefix),
        )
        return producer.produce(
            source_id=source_id,
            cycle_time=start_time,
            model_id=model_id,
            max_lead_hours=max_lead_hours,
        )
    except Exception as error:
        raise HindcastError(
            "HINDCAST_FORCING_PRODUCER_FAILED",
            f"Failed to produce hindcast forcing: {error}",
        ) from error


def _upsert_metadata_only_forcing_version(
    db_session: Session,
    *,
    forcing_version_id: str,
    model_id: str,
    source_id: str,
    start_time: datetime,
    end_time: datetime,
    station_count: int,
    forcing_package_uri: str,
    checksum: str,
    lineage: dict[str, Any],
) -> None:
    db_session.execute(
        text(
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
            VALUES (
                :forcing_version_id,
                :model_id,
                :source_id,
                :cycle_time,
                :start_time,
                :end_time,
                :station_count,
                :forcing_package_uri,
                :checksum,
                :lineage_json
            )
            ON CONFLICT (forcing_version_id) DO UPDATE SET
                cycle_time = EXCLUDED.cycle_time,
                start_time = EXCLUDED.start_time,
                end_time = EXCLUDED.end_time,
                station_count = EXCLUDED.station_count,
                forcing_package_uri = EXCLUDED.forcing_package_uri,
                checksum = EXCLUDED.checksum,
                lineage_json = EXCLUDED.lineage_json
            """
        ),
        {
            "forcing_version_id": forcing_version_id,
            "model_id": model_id,
            "source_id": source_id,
            "cycle_time": start_time,
            "start_time": start_time,
            "end_time": end_time,
            "station_count": station_count,
            "forcing_package_uri": forcing_package_uri,
            "checksum": checksum,
            "lineage_json": _json_param(db_session, lineage),
        },
    )


def _model_segment_count(db_session: Session, river_network_version_id: str) -> int | None:
    bind = db_session.get_bind()
    if not inspect(bind).has_table("river_network_version", schema="core"):
        return None
    row = db_session.execute(
        text(
            """
            SELECT segment_count
            FROM core.river_network_version
            WHERE river_network_version_id = :river_network_version_id
            LIMIT 1
            """
        ),
        {"river_network_version_id": river_network_version_id},
    ).mappings().first()
    return int(row["segment_count"]) if row is not None and row["segment_count"] is not None else None


def _station_count(db_session: Session, model_id: str) -> int:
    row = db_session.execute(
        text("SELECT COUNT(DISTINCT station_id) AS station_count FROM met.interp_weight WHERE model_id = :model_id"),
        {"model_id": model_id},
    ).mappings().first()
    station_count = int(row["station_count"] or 0) if row is not None else 0
    return station_count


def _record_qc_result(
    db_session: Session,
    *,
    target_id: str,
    run_id: str,
    passed: bool,
    checks_json: dict[str, Any],
    message: str,
) -> None:
    db_session.execute(
        text(
            """
            INSERT INTO ops.qc_result (
                qc_checkpoint,
                target_type,
                target_id,
                run_id,
                passed,
                severity,
                checks_json,
                message
            )
            VALUES (
                'hindcast_era5_coverage',
                'forcing_version',
                :target_id,
                :run_id,
                :passed,
                :severity,
                :checks_json,
                :message
            )
            """
        ),
        {
            "target_id": target_id,
            "run_id": run_id,
            "passed": passed,
            "severity": "info" if passed else "error",
            "checks_json": _json_param(db_session, checks_json),
            "message": message,
        },
    )


def _update_hydro_run(
    db_session: Session,
    run_id: str,
    *,
    status: str,
    error_code: str | None,
    error_message: str | None,
) -> None:
    db_session.execute(
        text(
            """
            UPDATE hydro.hydro_run
            SET status = :status,
                error_code = :error_code,
                error_message = :error_message
            WHERE run_id = :run_id
            """
        ),
        {"run_id": run_id, "status": status, "error_code": error_code, "error_message": error_message},
    )
    db_session.commit()


def _set_run_forcing(
    db_session: Session,
    run_id: str,
    forcing_version_id: str,
    forcing_package_uri: str | None = None,
) -> None:
    if forcing_package_uri:
        db_session.execute(
            text(
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
                SELECT
                    :forcing_version_id,
                    model_id,
                    source_id,
                    start_time,
                    start_time,
                    end_time,
                    0,
                    :forcing_package_uri,
                    '',
                    :lineage_json
                FROM hydro.hydro_run
                WHERE run_id = :run_id
                ON CONFLICT (forcing_version_id) DO UPDATE SET
                    forcing_package_uri = EXCLUDED.forcing_package_uri,
                    lineage_json = EXCLUDED.lineage_json
                """
            ),
            {
                "run_id": run_id,
                "forcing_version_id": forcing_version_id,
                "forcing_package_uri": forcing_package_uri,
                "lineage_json": _json_param(db_session, {"purpose": "hindcast", "producer_result": True}),
            },
        )
    db_session.execute(
        text(
            """
            UPDATE hydro.hydro_run
            SET forcing_version_id = :forcing_version_id
            WHERE run_id = :run_id
            """
        ),
        {"run_id": run_id, "forcing_version_id": forcing_version_id},
    )
    db_session.commit()


def _mark_run_failed(db_session: Session, run_id: str, error_code: str, error_message: str) -> None:
    _update_hydro_run(db_session, run_id, status="failed", error_code=error_code, error_message=error_message)


def _write_hindcast_manifest(run_id: str, model_id: str, source_id: str, year: int, db_session: Session) -> Path:
    config = HindcastConfig.from_env()
    model = _load_model_context(db_session, model_id)
    run_dir = config.workspace_root / "runs" / run_id / "input"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    start_time, end_time = _year_bounds(year)
    forcing_version_id = _load_run_forcing_version_id(db_session, run_id) or forcing_version_id_for_year(model_id, year)
    model_section = {
        "model_id": model_id,
        "basin_version_id": model["basin_version_id"],
        "river_network_version_id": model["river_network_version_id"],
        "model_package_uri": model["model_package_uri"],
    }
    if model.get("segment_count") is not None:
        model_section["segment_count"] = model["segment_count"]
    manifest = {
        "run_id": run_id,
        "run_type": "hindcast",
        "scenario_id": HINDCAST_SCENARIO_ID,
        "source_id": normalize_source_id(source_id),
        "year": int(year),
        "cycle_time": _format_time(start_time),
        "start_time": _format_time(start_time),
        "end_time": _format_time(end_time),
        "model": model_section,
        "forcing": {
            "forcing_version_id": forcing_version_id,
            "forcing_uri": _require_real_forcing_package_uri(db_session, forcing_version_id),
        },
        "initial_state": {
            "state_id": None,
            "ic_file_uri": None,
            "valid_time": None,
            "checksum": None,
            "quality": "cold_start_no_state",
        },
        "runtime": {
            "output_interval_minutes": 60,
            "init_mode": 1,
        },
        "outputs": {
            "run_manifest_uri": _run_manifest_uri(run_id),
            "output_uri": _run_output_uri(run_id),
            "log_uri": _run_log_uri(run_id),
        },
    }
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest_path


def _load_forcing_package_uri(db_session: Session, forcing_version_id: str) -> str | None:
    row = db_session.execute(
        text(
            """
            SELECT forcing_package_uri
            FROM met.forcing_version
            WHERE forcing_version_id = :forcing_version_id
            LIMIT 1
            """
        ),
        {"forcing_version_id": forcing_version_id},
    ).mappings().first()
    if row is None:
        return None
    if row["forcing_package_uri"] in (None, ""):
        return None
    return str(row["forcing_package_uri"])


def _require_real_forcing_package_uri(db_session: Session, forcing_version_id: str) -> str:
    forcing_package_uri = _load_forcing_package_uri(db_session, forcing_version_id)
    if forcing_package_uri:
        return forcing_package_uri
    raise HindcastError(
        HINDCAST_FORCING_PACKAGE_UNAVAILABLE,
        "Hindcast SHUD runtime requires a real forcing package; metadata-only forcing is unavailable.",
        {"forcing_version_id": forcing_version_id},
    )


def _load_run_forcing_version_id(db_session: Session, run_id: str) -> str | None:
    row = db_session.execute(
        text(
            """
            SELECT forcing_version_id
            FROM hydro.hydro_run
            WHERE run_id = :run_id
            LIMIT 1
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    if row is None or row["forcing_version_id"] is None:
        return None
    return str(row["forcing_version_id"])


def _load_basin_version_for_slurm(db_session: Session | None, model_id: str) -> str:
    basin_version_id, _river_network_version_id = _load_model_versions_for_slurm(db_session, model_id)
    return basin_version_id


def _load_model_versions_for_slurm(
    db_session: Session | None,
    model_id: str,
    *,
    basin_version_id: str | None = None,
    river_network_version_id: str | None = None,
) -> tuple[str, str]:
    if basin_version_id and river_network_version_id:
        return basin_version_id, river_network_version_id
    if db_session is None:
        missing = []
        if basin_version_id is None:
            missing.append("basin_version_id")
        if river_network_version_id is None:
            missing.append("river_network_version_id")
        raise HindcastError(
            "MODEL_VERSION_REQUIRED",
            "basin_version_id and river_network_version_id are required when no database session is configured.",
            {"model_id": model_id, "missing_fields": missing},
        )
    row = db_session.execute(
        text(
            """
            SELECT basin_version_id, river_network_version_id
            FROM core.model_instance
            WHERE model_id = :model_id
            LIMIT 1
            """
        ),
        {"model_id": model_id},
    ).mappings().first()
    if row is not None and row["basin_version_id"] is not None and row["river_network_version_id"] is not None:
        return (
            basin_version_id or str(row["basin_version_id"]),
            river_network_version_id or str(row["river_network_version_id"]),
        )
    run_row = db_session.execute(
        text(
            """
            SELECT h.basin_version_id, mi.river_network_version_id
            FROM hydro.hydro_run h
            LEFT JOIN core.model_instance mi ON mi.model_id = h.model_id
            WHERE h.model_id = :model_id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"model_id": model_id},
    ).mappings().first()
    if (
        run_row is not None
        and (basin_version_id or run_row["basin_version_id"] is not None)
        and (river_network_version_id or run_row["river_network_version_id"] is not None)
    ):
        return (
            basin_version_id or str(run_row["basin_version_id"]),
            river_network_version_id or str(run_row["river_network_version_id"]),
        )
    raise HindcastError("MODEL_NOT_FOUND", f"Model not found: {model_id}", {"model_id": model_id})


def _hindcast_forcing_package_uri(forcing_version_id: str) -> str:
    return f"forcing/{forcing_version_id}/"


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _run_manifest_uri(run_id: str) -> str:
    return f"runs/{run_id}/input/manifest.json"


def _run_output_uri(run_id: str) -> str:
    return f"runs/{run_id}/output/"


def _run_log_uri(run_id: str) -> str:
    return f"runs/{run_id}/logs/hindcast.log"


def _json_param(db_session: Session, value: dict[str, Any]) -> Any:
    return json.dumps(value, sort_keys=True)


def _validate_model_id(model_id: str) -> None:
    if _SAFE_ID_RE.fullmatch(str(model_id)) is None:
        raise HindcastError("INVALID_MODEL_ID", "model_id contains invalid characters.", {"model_id": model_id})


def _table_exists(db_session: Session, schema: str, table_name: str) -> bool:
    try:
        return inspect(db_session.get_bind()).has_table(table_name, schema=schema)
    except SQLAlchemyError:
        return False


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump())
    return dict(value)
