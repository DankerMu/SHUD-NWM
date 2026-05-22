from __future__ import annotations

import json
import logging
import math
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from workers.data_adapters.base import cycle_id_for

RETURN_PERIODS: tuple[int, ...] = (2, 5, 10, 20, 50, 100)
QUANTILE_KEYS: tuple[str, ...] = tuple(f"Q{return_period}" for return_period in RETURN_PERIODS)
USABLE_CURVE_FLAGS: tuple[str, ...] = ("ok", "partial_sample", "monotonicity_corrected")
WARNING_LEVELS: tuple[str, ...] = (
    "normal",
    "elevated",
    "watch",
    "warning",
    "high_risk",
    "severe",
    "extreme",
)

LOGGER = logging.getLogger(__name__)


class ReturnPeriodError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class FrequencyCurve:
    curve_id: str
    thresholds: dict[str, float]
    sample_quality: dict[str, dict[str, Any]]
    quality_flag: str


@dataclass(frozen=True)
class ReturnPeriodComputationStats:
    total_segments: int
    with_curve: int
    without_curve: int
    warning_counts: dict[str, int]
    rows_written: int
    status: str = "succeeded"
    error_code: str | None = None
    error_message: str | None = None
    quality_state: str = "ready"
    unavailable_products: tuple[str, ...] = ()
    residual_blockers: tuple[dict[str, Any], ...] = ()


def extract_max_forecast_q(run_id: str, db_session: Session) -> dict[str, tuple[float, datetime]]:
    rows = db_session.execute(
        text(
            """
            WITH ranked AS (
                SELECT
                    rt.river_segment_id,
                    rt.value,
                    rt.valid_time,
                    ROW_NUMBER() OVER (
                        PARTITION BY rt.river_segment_id
                        ORDER BY rt.value DESC, rt.valid_time ASC
                    ) AS rank
                FROM hydro.river_timeseries rt
                JOIN hydro.hydro_run hr ON hr.run_id = rt.run_id
                WHERE rt.run_id = :run_id
                  AND rt.variable = 'q_down'
                  AND rt.valid_time >= hr.start_time
                  AND rt.valid_time <= hr.end_time
            )
            SELECT river_segment_id, value, valid_time
            FROM ranked
            WHERE rank = 1
            ORDER BY river_segment_id
            """
        ),
        {"run_id": run_id},
    ).mappings()
    return {
        str(row["river_segment_id"]): (float(row["value"]), row["valid_time"])
        for row in rows
        if row["value"] is not None
    }


def extract_timestep_q(run_id: str, db_session: Session) -> dict[datetime, dict[str, float]]:
    rows = db_session.execute(
        text(
            """
            SELECT rt.valid_time, rt.river_segment_id, rt.value
            FROM hydro.river_timeseries rt
            JOIN hydro.hydro_run hr ON hr.run_id = rt.run_id
            WHERE rt.run_id = :run_id
              AND rt.variable = 'q_down'
              AND rt.valid_time >= hr.start_time
              AND rt.valid_time <= hr.end_time
            ORDER BY rt.valid_time, rt.river_segment_id
            """
        ),
        {"run_id": run_id},
    ).mappings()
    timesteps: dict[datetime, dict[str, float]] = {}
    for row in rows:
        if row["value"] is None:
            continue
        valid_time = row["valid_time"]
        timesteps.setdefault(valid_time, {})[str(row["river_segment_id"])] = float(row["value"])
    return timesteps


def get_frequency_curve(
    model_id: str,
    river_network_version_id: str,
    segment_id: str,
    db_session: Session,
) -> FrequencyCurve | None:
    row = db_session.execute(
        text(
            """
            SELECT
                curve_id,
                parameters_json,
                quality_flag,
                q2,
                q5,
                q10,
                q20,
                q50,
                q100
            FROM flood.flood_frequency_curve
            WHERE model_id = :model_id
              AND river_network_version_id = :river_network_version_id
              AND river_segment_id = :river_segment_id
              AND duration = '1h'
              AND quality_flag IN ('ok', 'partial_sample', 'monotonicity_corrected', 'p3_fallback_gev')
            ORDER BY sample_period_end DESC, sample_period_start DESC, curve_id DESC
            LIMIT 1
            """
        ),
        {
            "model_id": model_id,
            "river_network_version_id": river_network_version_id,
            "river_segment_id": segment_id,
        },
    ).mappings().first()
    if row is None:
        return None

    thresholds: dict[str, float] = {}
    for key in QUANTILE_KEYS:
        value = row[key.lower()]
        if value is None:
            return None
        thresholds[key] = float(value)

    parameters = _json_loads(row["parameters_json"])
    sample_quality = dict(parameters.get("sample_quality") or {})
    return FrequencyCurve(
        curve_id=str(row["curve_id"]),
        thresholds=thresholds,
        sample_quality=sample_quality,
        quality_flag=str(row["quality_flag"]),
    )


def interpolate_return_period(q_value: float, thresholds: dict[str, float]) -> float | None:
    q = float(q_value)
    if not math.isfinite(q):
        return None
    points = [(return_period, float(thresholds[f"Q{return_period}"])) for return_period in RETURN_PERIODS]
    if any(not math.isfinite(value) for _return_period, value in points):
        return None
    if q < points[0][1]:
        return 1.0
    if q > points[-1][1]:
        return 101.0

    for (lower_t, lower_q), (upper_t, upper_q) in zip(points, points[1:], strict=True):
        if q == lower_q:
            return float(lower_t)
        if q == upper_q:
            return float(upper_t)
        if lower_q < q < upper_q:
            fraction = (q - lower_q) / (upper_q - lower_q)
            log_t = math.log10(lower_t) + fraction * (math.log10(upper_t) - math.log10(lower_t))
            return float(10**log_t)
    return float(points[-1][0])


def map_warning_level(
    return_period: float | None,
    sample_quality: dict[str, dict[str, Any]] | None = None,
    curve_quality_flag: str | None = None,
) -> str | None:
    if curve_quality_flag in {"fit_failed", "no_valid_sample"} or return_period is None:
        return None
    rp = float(return_period)
    if not math.isfinite(rp):
        return None
    raw_level = _raw_warning_level(rp)
    max_index = _highest_reliable_warning_index(sample_quality or {})
    return WARNING_LEVELS[min(WARNING_LEVELS.index(raw_level), max_index)]


def compute_return_periods(
    run_id: str,
    db_session: Session,
    *,
    graceful_degradation: bool = True,
    quality_contract: Mapping[str, Any] | None = None,
) -> ReturnPeriodComputationStats:
    started_at = datetime.now(UTC)
    try:
        stats = _compute_return_periods(run_id, db_session, started_at=started_at, quality_contract=quality_contract)
        db_session.commit()
        return stats
    except Exception as error:
        db_session.rollback()
        error_code = getattr(error, "error_code", "RETURN_PERIOD_FAILED")
        error_message = getattr(error, "message", str(error))
        _record_frequency_failure(db_session, run_id, str(error_code), str(error_message), started_at)
        db_session.commit()
        LOGGER.warning("Return-period computation failed for run_id=%s: %s", run_id, error_message)
        if graceful_degradation:
            return ReturnPeriodComputationStats(
                total_segments=0,
                with_curve=0,
                without_curve=0,
                warning_counts={},
                rows_written=0,
                status="failed",
                error_code=str(error_code),
                error_message=str(error_message),
                quality_state="unavailable",
                unavailable_products=("return_period_result",),
                residual_blockers=(
                    {
                        "code": str(error_code),
                        "state": "unavailable",
                        "quality_flag": "frequency_failed",
                        "run_id": run_id,
                        "residual_risk": str(error_message),
                    },
                ),
            )
        raise


def _compute_return_periods(
    run_id: str,
    db_session: Session,
    *,
    started_at: datetime,
    quality_contract: Mapping[str, Any] | None = None,
) -> ReturnPeriodComputationStats:
    context = _load_run_context(run_id, db_session)
    contract = _normalize_quality_contract(quality_contract)
    warning_thresholds_available = "warning_thresholds" not in contract["unavailable_products"]
    max_values = extract_max_forecast_q(run_id, db_session)
    timestep_values = extract_timestep_q(run_id, db_session)
    segment_ids = sorted(
        set(max_values)
        | {segment_id for timestep in timestep_values.values() for segment_id in timestep}
    )
    if not segment_ids:
        raise ReturnPeriodError("NO_FORECAST_Q", f"No q_down values found for run: {run_id}")

    curves: dict[str, FrequencyCurve | None] = {}
    no_curve_quality: dict[str, str] = {}
    for segment_id in segment_ids:
        curve = get_frequency_curve(
            str(context["model_id"]),
            str(context["river_network_version_id"]),
            segment_id,
            db_session,
        )
        curves[segment_id] = curve
        if curve is None:
            no_curve_quality[segment_id] = (
                "no_usable_frequency_curve"
                if _any_frequency_curve_exists(context, segment_id, db_session)
                else "no_frequency_curve"
            )

    rows_written = 0
    warning_counts: Counter[str] = Counter()
    max_duration = _window_duration_label(context["start_time"], context["end_time"])

    for segment_id, (q_value, max_time) in max_values.items():
        result = _evaluate_q(
            q_value,
            curves[segment_id],
            no_curve_quality.get(segment_id),
            warning_thresholds_available=warning_thresholds_available,
        )
        warning_counts.update([result["warning_level"]] if result["warning_level"] else [])
        _upsert_return_period_result(
            db_session,
            context,
            segment_id,
            max_time,
            max_duration,
            q_value,
            max_over_window=True,
            result=result,
        )
        rows_written += 1

    for valid_time, segment_values in timestep_values.items():
        for segment_id, q_value in segment_values.items():
            result = _evaluate_q(
                q_value,
                curves[segment_id],
                no_curve_quality.get(segment_id),
                warning_thresholds_available=warning_thresholds_available,
            )
            _upsert_return_period_result(
                db_session,
                context,
                segment_id,
                valid_time,
                "1h",
                q_value,
                max_over_window=False,
                result=result,
            )
            rows_written += 1

    _mark_frequency_succeeded(db_session, context, started_at)
    unavailable_products = set(contract["unavailable_products"])
    if any(curve is None for curve in curves.values()):
        unavailable_products.add("frequency_curves")
    blockers = [
        *_frequency_residual_blockers(context, no_curve_quality),
        *_quality_contract_residual_blockers(context, unavailable_products),
    ]
    if not unavailable_products:
        register_flood_tile_layer(run_id, db_session)
    return ReturnPeriodComputationStats(
        total_segments=len(segment_ids),
        with_curve=sum(1 for curve in curves.values() if curve is not None),
        without_curve=sum(1 for curve in curves.values() if curve is None),
        warning_counts={level: int(warning_counts.get(level, 0)) for level in WARNING_LEVELS},
        rows_written=rows_written,
        quality_state="ready" if not unavailable_products else "unavailable",
        unavailable_products=tuple(sorted(unavailable_products)),
        residual_blockers=tuple(blockers),
    )


def register_flood_tile_layer(run_id: str, db_session: Session) -> None:
    """Register the vector tile layer metadata for a computed flood warning run."""
    if not _table_exists(db_session, "map", "tile_layer"):
        LOGGER.info("Skipping flood tile layer registration; map.tile_layer is unavailable")
        return

    columns = _table_columns(db_session, "map", "tile_layer")
    context = _load_run_context(run_id, db_session)
    layer_id = f"flood_return_period_{run_id}"
    tile_uri_template = (
        f"/api/v1/tiles/flood-return-period?run_id={run_id}&duration={{duration}}&valid_time={{valid_time}}"
    )
    style_json = {
        "type": "geojson",
        "warning_level_property": "warning_level",
        "return_period_property": "return_period",
    }
    values: dict[str, Any] = {
        "layer_id": layer_id,
        "layer_type": "flood_return_period",
        "source_run_id": run_id,
        "source_product_id": None,
        "variable": "return_period",
        "valid_time": None,
        "tile_format": "geojson",
        "tile_uri_template": tile_uri_template,
        "min_zoom": 0,
        "max_zoom": 14,
        "style_json": json.dumps(style_json),
        "published_flag": True,
        "publish_time": datetime.now(UTC),
        "created_at": datetime.now(UTC),
    }
    if "source_product_id" in columns and context.get("scenario_id"):
        values["source_product_id"] = str(context["scenario_id"])

    insert_columns = [column for column in values if column in columns]
    assignments = [
        f"{column} = EXCLUDED.{column}"
        for column in insert_columns
        if column not in {"layer_id", "created_at"}
    ]
    db_session.execute(
        text(
            f"""
            INSERT INTO map.tile_layer ({', '.join(insert_columns)})
            VALUES ({', '.join(f':{column}' for column in insert_columns)})
            ON CONFLICT (layer_id) DO UPDATE SET {', '.join(assignments)}
            """
        ),
        values,
    )


def _evaluate_q(
    q_value: float,
    curve: FrequencyCurve | None,
    no_curve_quality_flag: str | None,
    *,
    warning_thresholds_available: bool = True,
) -> dict[str, Any]:
    if curve is None:
        return {
            "return_period": None,
            "warning_level": None,
            "quality_flag": no_curve_quality_flag or "no_frequency_curve",
        }
    return_period = interpolate_return_period(q_value, curve.thresholds)
    if warning_thresholds_available:
        warning_level = map_warning_level(return_period, curve.sample_quality, curve.quality_flag)
        raw_level = _raw_warning_level(return_period) if return_period is not None else None
        quality_flag = "unreliable_threshold" if raw_level is not None and raw_level != warning_level else "ok"
    else:
        warning_level = None
        quality_flag = "warning_thresholds_unavailable"
    return {
        "return_period": return_period,
        "warning_level": warning_level,
        "quality_flag": quality_flag,
    }


def _upsert_return_period_result(
    db_session: Session,
    context: dict[str, Any],
    segment_id: str,
    valid_time: Any,
    duration: str,
    q_value: float,
    *,
    max_over_window: bool,
    result: dict[str, Any],
) -> None:
    if max_over_window:
        _delete_prior_peak_result(db_session, context, segment_id, duration)
    db_session.execute(
        text(
            """
            INSERT INTO flood.return_period_result (
                run_id,
                scenario_id,
                basin_version_id,
                river_network_version_id,
                model_id,
                river_segment_id,
                valid_time,
                duration,
                q_value,
                q_unit,
                return_period,
                warning_level,
                source_id,
                cycle_time,
                max_over_window,
                quality_flag
            )
            VALUES (
                :run_id,
                :scenario_id,
                :basin_version_id,
                :river_network_version_id,
                :model_id,
                :river_segment_id,
                :valid_time,
                :duration,
                :q_value,
                'm3/s',
                :return_period,
                :warning_level,
                :source_id,
                :cycle_time,
                :max_over_window,
                :quality_flag
            )
            ON CONFLICT (
                run_id,
                river_network_version_id,
                river_segment_id,
                duration,
                valid_time,
                max_over_window
            ) DO UPDATE SET
                scenario_id = EXCLUDED.scenario_id,
                basin_version_id = EXCLUDED.basin_version_id,
                model_id = EXCLUDED.model_id,
                q_value = EXCLUDED.q_value,
                q_unit = EXCLUDED.q_unit,
                return_period = EXCLUDED.return_period,
                warning_level = EXCLUDED.warning_level,
                source_id = EXCLUDED.source_id,
                cycle_time = EXCLUDED.cycle_time,
                quality_flag = EXCLUDED.quality_flag
            """
        ),
        {
            "run_id": context["run_id"],
            "scenario_id": context["scenario_id"],
            "basin_version_id": context["basin_version_id"],
            "river_network_version_id": context["river_network_version_id"],
            "model_id": context["model_id"],
            "river_segment_id": segment_id,
            "valid_time": valid_time,
            "duration": duration,
            "q_value": float(q_value),
            "return_period": result["return_period"],
            "warning_level": result["warning_level"],
            "source_id": context.get("source_id"),
            "cycle_time": context.get("cycle_time"),
            "max_over_window": bool(max_over_window),
            "quality_flag": result["quality_flag"],
        },
    )


def _delete_prior_peak_result(
    db_session: Session,
    context: dict[str, Any],
    segment_id: str,
    duration: str,
) -> None:
    db_session.execute(
        text(
            """
            DELETE FROM flood.return_period_result
            WHERE run_id = :run_id
              AND river_network_version_id = :river_network_version_id
              AND river_segment_id = :river_segment_id
              AND duration = :duration
              AND max_over_window = true
            """
        ),
        {
            "run_id": context["run_id"],
            "river_network_version_id": context["river_network_version_id"],
            "river_segment_id": segment_id,
            "duration": duration,
        },
    )


def _load_run_context(run_id: str, db_session: Session) -> dict[str, Any]:
    hydro_columns = _table_columns(db_session, "hydro", "hydro_run")
    select_parts = []
    for column in (
        "run_id",
        "scenario_id",
        "model_id",
        "basin_version_id",
        "source_id",
        "cycle_time",
        "start_time",
        "end_time",
        "status",
    ):
        select_parts.append(column if column in hydro_columns else f"NULL AS {column}")
    row = db_session.execute(
        text(f"SELECT {', '.join(select_parts)} FROM hydro.hydro_run WHERE run_id = :run_id LIMIT 1"),
        {"run_id": run_id},
    ).mappings().first()
    if row is None:
        raise ReturnPeriodError("RUN_NOT_FOUND", f"Run not found: {run_id}")

    context = dict(row)
    model_row = db_session.execute(
        text(
            """
            SELECT river_network_version_id
            FROM core.model_instance
            WHERE model_id = :model_id
            LIMIT 1
            """
        ),
        {"model_id": context["model_id"]},
    ).mappings().first()
    if model_row is None:
        raise ReturnPeriodError("MODEL_NOT_FOUND", f"Model not found: {context['model_id']}")
    context["river_network_version_id"] = str(model_row["river_network_version_id"])
    return context


def _any_frequency_curve_exists(context: dict[str, Any], segment_id: str, db_session: Session) -> bool:
    row = db_session.execute(
        text(
            """
            SELECT 1
            FROM flood.flood_frequency_curve
            WHERE model_id = :model_id
              AND river_network_version_id = :river_network_version_id
              AND river_segment_id = :river_segment_id
              AND duration = '1h'
            LIMIT 1
            """
        ),
        {
            "model_id": context["model_id"],
            "river_network_version_id": context["river_network_version_id"],
            "river_segment_id": segment_id,
        },
    ).first()
    return row is not None


def _frequency_residual_blockers(
    context: Mapping[str, Any],
    no_curve_quality: Mapping[str, str],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    for segment_id, quality_flag in sorted(no_curve_quality.items()):
        blockers.append(
            {
                "code": "FREQUENCY_CURVE_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": quality_flag,
                "run_id": context.get("run_id"),
                "model_id": context.get("model_id"),
                "river_network_version_id": context.get("river_network_version_id"),
                "river_segment_id": segment_id,
                "residual_risk": (
                    "Return period and warning level are null for this segment; no values were fabricated."
                ),
            }
        )
    return blockers


def _normalize_quality_contract(quality_contract: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(quality_contract, Mapping):
        return {"unavailable_products": set()}
    raw_products = quality_contract.get("unavailable_products")
    products = {
        str(item)
        for item in raw_products or []
        if item not in (None, "")
    } if isinstance(raw_products, (list, tuple, set)) else set()
    state = str(quality_contract.get("state") or "")
    warning_thresholds = str(quality_contract.get("warning_thresholds") or "")
    if state == "unavailable" and warning_thresholds == "unavailable":
        products.add("warning_thresholds")
    return {"unavailable_products": products}


def _quality_contract_residual_blockers(
    context: Mapping[str, Any],
    unavailable_products: set[str],
) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if "warning_thresholds" in unavailable_products:
        blockers.append(
            {
                "code": "WARNING_THRESHOLDS_UNAVAILABLE",
                "state": "unavailable",
                "quality_flag": "warning_thresholds_unavailable",
                "run_id": context.get("run_id"),
                "model_id": context.get("model_id"),
                "river_network_version_id": context.get("river_network_version_id"),
                "residual_risk": "warning_level remains null because warning thresholds are unavailable.",
            }
        )
    return blockers


def _mark_frequency_succeeded(
    db_session: Session,
    context: dict[str, Any],
    started_at: datetime,
) -> None:
    hydro_columns = _table_columns(db_session, "hydro", "hydro_run")
    assignments = ["status = 'frequency_done'"]
    if "error_code" in hydro_columns:
        assignments.append("error_code = NULL")
    if "error_message" in hydro_columns:
        assignments.append("error_message = NULL")
    if "updated_at" in hydro_columns:
        assignments.append("updated_at = CURRENT_TIMESTAMP")
    db_session.execute(
        text(
            f"""
            UPDATE hydro.hydro_run
            SET {', '.join(assignments)}
            WHERE run_id = :run_id
              AND status IN ('parsed', 'frequency_done')
            """
        ),
        {"run_id": context["run_id"]},
    )
    _upsert_pipeline_job(
        db_session,
        context,
        status="succeeded",
        started_at=started_at,
        finished_at=datetime.now(UTC),
        error_code=None,
        error_message=None,
    )


def _record_frequency_failure(
    db_session: Session,
    run_id: str,
    error_code: str,
    error_message: str,
    started_at: datetime,
) -> None:
    try:
        context = _load_run_context(run_id, db_session)
    except Exception:
        context = {
            "run_id": run_id,
            "cycle_id": None,
            "model_id": None,
            "source_id": None,
            "cycle_time": None,
        }
    hydro_columns = _table_columns(db_session, "hydro", "hydro_run")
    assignments = []
    if "error_code" in hydro_columns:
        assignments.append("error_code = :error_code")
    if "error_message" in hydro_columns:
        assignments.append("error_message = :error_message")
    if "updated_at" in hydro_columns:
        assignments.append("updated_at = CURRENT_TIMESTAMP")
    if assignments:
        db_session.execute(
            text(f"UPDATE hydro.hydro_run SET {', '.join(assignments)} WHERE run_id = :run_id"),
            {"run_id": run_id, "error_code": error_code, "error_message": error_message},
        )
    _upsert_pipeline_job(
        db_session,
        context,
        status="failed",
        started_at=started_at,
        finished_at=datetime.now(UTC),
        error_code=error_code,
        error_message=error_message,
    )


def _upsert_pipeline_job(
    db_session: Session,
    context: dict[str, Any],
    *,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    error_code: str | None,
    error_message: str | None,
) -> None:
    if not _table_exists(db_session, "ops", "pipeline_job"):
        return
    columns = _table_columns(db_session, "ops", "pipeline_job")
    values: dict[str, Any] = {
        "job_id": f"{context['run_id']}_frequency",
        "run_id": context["run_id"],
        "cycle_id": _cycle_id(context),
        "job_type": "frequency",
        "status": status,
        "stage": "frequency",
        "started_at": started_at,
        "finished_at": finished_at,
        "error_code": error_code,
        "error_message": error_message,
    }
    if "model_id" in columns:
        values["model_id"] = context.get("model_id")
    if "submitted_at" in columns:
        values["submitted_at"] = started_at
    if "updated_at" in columns:
        values["updated_at"] = finished_at
    if "created_at" in columns:
        values["created_at"] = started_at

    insert_columns = [column for column in values if column in columns]
    assignments = [
        f"{column} = EXCLUDED.{column}"
        for column in insert_columns
        if column not in {"job_id", "created_at"}
    ]
    db_session.execute(
        text(
            f"""
            INSERT INTO ops.pipeline_job ({', '.join(insert_columns)})
            VALUES ({', '.join(f':{column}' for column in insert_columns)})
            ON CONFLICT (job_id) DO UPDATE SET {', '.join(assignments)}
            """
        ),
        values,
    )


def _cycle_id(context: dict[str, Any]) -> str | None:
    source_id = context.get("source_id")
    cycle_time = context.get("cycle_time")
    if source_id is None or cycle_time is None:
        return None
    return cycle_id_for(str(source_id), cycle_time)


def _raw_warning_level(return_period: float) -> str:
    if return_period < 2:
        return "normal"
    if return_period < 5:
        return "elevated"
    if return_period < 10:
        return "watch"
    if return_period < 20:
        return "warning"
    if return_period < 50:
        return "high_risk"
    if return_period < 100:
        return "severe"
    return "extreme"


def _highest_reliable_warning_index(sample_quality: dict[str, dict[str, Any]]) -> int:
    if not sample_quality:
        return len(WARNING_LEVELS) - 1
    reliable_count = 0
    for key in QUANTILE_KEYS:
        threshold_quality = sample_quality.get(key) or sample_quality.get(key.lower()) or {}
        if threshold_quality.get("met") is False or threshold_quality.get("quality_flag") == "insufficient_sample":
            break
        reliable_count += 1
    if reliable_count == len(QUANTILE_KEYS):
        return len(WARNING_LEVELS) - 1
    return max(0, reliable_count - 1)


def _window_duration_label(start_time: Any, end_time: Any) -> str:
    start = _parse_time(start_time)
    end = _parse_time(end_time)
    hours = max(1, round((end - start).total_seconds() / 3600))
    if hours % 24 == 0:
        return f"{hours // 24}d"
    return f"{hours}h"


def _parse_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _json_loads(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return dict(json.loads(str(value)))


def _table_exists(db_session: Session, schema: str, table_name: str) -> bool:
    if _dialect_name(db_session) == "sqlite":
        try:
            row = db_session.execute(
                text(f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = :table_name LIMIT 1"),
                {"table_name": table_name},
            ).first()
            return row is not None
        except SQLAlchemyError:
            return False
    try:
        return inspect(db_session.get_bind()).has_table(table_name, schema=schema)
    except SQLAlchemyError:
        return False


def _table_columns(db_session: Session, schema: str, table_name: str) -> set[str]:
    if _dialect_name(db_session) == "sqlite":
        rows = db_session.execute(text(f"PRAGMA {schema}.table_info({table_name})")).mappings()
        return {str(row["name"]) for row in rows}
    try:
        return {str(column["name"]) for column in inspect(db_session.get_bind()).get_columns(table_name, schema=schema)}
    except SQLAlchemyError:
        return set()


def _dialect_name(db_session: Session) -> str:
    return str(getattr(db_session.get_bind().dialect, "name", ""))
