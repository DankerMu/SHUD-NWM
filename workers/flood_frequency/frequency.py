from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from scipy import stats
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

RETURN_PERIODS: tuple[int, ...] = (2, 5, 10, 20, 50, 100)
QUANTILE_KEYS: tuple[str, ...] = tuple(f"Q{return_period}" for return_period in RETURN_PERIODS)
DURATION_HOURS: dict[str, int] = {
    "1h": 1,
    "3h": 3,
    "6h": 6,
    "24h": 24,
    "72h": 72,
    "7d": 168,
}
SAMPLE_THRESHOLDS: dict[str, int] = {
    "Q2": 10,
    "Q5": 10,
    "Q10": 15,
    "Q20": 20,
    "Q50": 30,
    "Q100": 40,
}


class FrequencyFitError(RuntimeError):
    pass


@dataclass(frozen=True)
class FitResult:
    method: str
    params: dict[str, float]
    quantiles: dict[str, float | None]
    quality_flag: str = "ok"
    error_message: str | None = None


@dataclass(frozen=True)
class SampleQuality:
    n_samples: int
    thresholds: dict[str, dict[str, Any]]
    quality_flag: str

    @property
    def per_threshold(self) -> dict[str, dict[str, Any]]:
        return self.thresholds


@dataclass(frozen=True)
class MonotonicityResult:
    original_quantiles: dict[str, float | None]
    corrected_quantiles: dict[str, float | None]
    corrections: list[dict[str, Any]]
    quality_flag: str

    @property
    def corrected_values(self) -> dict[str, float | None]:
        return self.corrected_quantiles


@dataclass(frozen=True)
class AnnualMaximaResult:
    samples: list[tuple[int, float]]
    excluded_years: list[int]
    observed_years: list[int]


@dataclass(frozen=True)
class FitCurvesStats:
    total_segments: int
    succeeded: int
    failed: int
    skipped: int
    items: list[dict[str, Any]]


def extract_annual_maxima(
    model_id: str,
    river_segment_id: str,
    duration: str,
    db_session: Session,
) -> list[tuple[int, float]]:
    return extract_annual_maxima_with_metadata(model_id, river_segment_id, duration, db_session).samples


def extract_annual_maxima_with_metadata(
    model_id: str,
    river_segment_id: str,
    duration: str,
    db_session: Session,
) -> AnnualMaximaResult:
    window_hours = _duration_hours(duration)
    rows = _annual_rows(model_id, river_segment_id, window_hours, db_session)
    samples: list[tuple[int, float]] = []
    excluded_years: list[int] = []
    observed_years: list[int] = []

    for row in rows:
        year = int(row["year"])
        observed_years.append(year)
        available_hours = int(row["available_hours"] or 0)
        annual_max = row["annual_max"]
        if annual_max is None:
            excluded_years.append(year)
            continue
        expected_hours = _expected_hours(year)
        missing_rate = 1.0 - min(available_hours, expected_hours) / expected_hours
        if missing_rate > 0.10:
            excluded_years.append(year)
            continue
        samples.append((year, float(annual_max)))

    samples.sort(key=lambda item: item[0])
    return AnnualMaximaResult(
        samples=samples,
        excluded_years=sorted(excluded_years),
        observed_years=sorted(set(observed_years)),
    )


def fit_pearson3(samples: list[float]) -> FitResult:
    skew, loc, scale = stats.pearson3.fit(_fit_data(samples))
    params = {"skew": float(skew), "loc": float(loc), "scale": float(scale)}
    _validate_distribution_params(params, scale_key="scale")
    quantiles = _quantiles(stats.pearson3, (skew, loc, scale))
    return FitResult(method="P-III", params=params, quantiles=quantiles)


def fit_gev(samples: list[float]) -> FitResult:
    shape, loc, scale = stats.genextreme.fit(_fit_data(samples))
    params = {"shape": float(shape), "loc": float(loc), "scale": float(scale)}
    _validate_distribution_params(params, scale_key="scale")
    quantiles = _quantiles(stats.genextreme, (shape, loc, scale))
    return FitResult(method="GEV", params=params, quantiles=quantiles)


def fit_frequency_curve(samples: list[float], method: str = "auto") -> FitResult:
    normalized_method = _normalize_method(method)
    errors: list[str] = []
    if normalized_method in {"auto", "P-III"}:
        try:
            return fit_pearson3(samples)
        except Exception as error:
            errors.append(f"P-III: {error}")
            if normalized_method == "P-III":
                return _fit_failed("; ".join(errors), method="P-III")

    if normalized_method in {"auto", "GEV"}:
        try:
            result = fit_gev(samples)
            if normalized_method == "auto" and errors:
                return FitResult(
                    method=result.method,
                    params=result.params,
                    quantiles=result.quantiles,
                    quality_flag="p3_fallback_gev",
                    error_message="; ".join(errors),
                )
            return result
        except Exception as error:
            errors.append(f"GEV: {error}")
            if normalized_method == "GEV":
                return _fit_failed("; ".join(errors), method="GEV")

    return _fit_failed("; ".join(errors) or "Unsupported method")


def check_sample_size(n_samples: int) -> SampleQuality:
    thresholds: dict[str, dict[str, Any]] = {}
    if n_samples < 10:
        for key, min_required in SAMPLE_THRESHOLDS.items():
            thresholds[key] = {
                "min_required": min_required,
                "met": False,
                "quality_flag": "insufficient_sample",
            }
        return SampleQuality(n_samples=n_samples, thresholds=thresholds, quality_flag="insufficient_sample")

    all_met = True
    for key, min_required in SAMPLE_THRESHOLDS.items():
        met = n_samples >= min_required
        all_met = all_met and met
        thresholds[key] = {
            "min_required": min_required,
            "met": met,
            "quality_flag": "ok" if met else "insufficient_sample",
        }
    return SampleQuality(n_samples=n_samples, thresholds=thresholds, quality_flag="ok" if all_met else "partial_sample")


def check_monotonicity(quantiles: dict[str, float | None]) -> MonotonicityResult:
    original = {key: _finite_or_none(quantiles.get(key)) for key in QUANTILE_KEYS}
    corrected = dict(original)
    corrections: list[dict[str, Any]] = []

    for index in range(1, len(QUANTILE_KEYS)):
        key = QUANTILE_KEYS[index]
        previous_key = QUANTILE_KEYS[index - 1]
        current = corrected.get(key)
        previous = corrected.get(previous_key)
        if current is None or previous is None or current > previous:
            continue

        next_value = _next_greater_value(corrected, index, previous)
        replacement = (previous + next_value) / 2.0 if next_value is not None else math.nextafter(previous, math.inf)
        corrections.append(
            {
                "quantile": key,
                "original_value": current,
                "corrected_value": replacement,
                "reason": f"{previous_key} >= {key}",
            }
        )
        corrected[key] = replacement

    quality_flag = "monotonicity_corrected" if corrections else "ok"
    return MonotonicityResult(
        original_quantiles=original,
        corrected_quantiles=corrected,
        corrections=corrections,
        quality_flag=quality_flag,
    )


def save_frequency_curve(curve_data: Mapping[str, Any], db_session: Session) -> str:
    quantiles = _normalize_quantile_map(curve_data)
    parameters_json = dict(curve_data.get("parameters_json") or {})
    sample_period_start = _coerce_date(curve_data["sample_period_start"])
    sample_period_end = _coerce_date(curve_data["sample_period_end"])
    method = str(curve_data["method"])
    curve_id = str(
        curve_data.get("curve_id")
        or _curve_id(
            str(curve_data["model_id"]),
            str(curve_data["river_network_version_id"]),
            str(curve_data["river_segment_id"]),
            str(curve_data["duration"]),
            method,
            sample_period_start,
            sample_period_end,
        )
    )
    db_session.execute(
        text(
            """
            INSERT INTO flood.flood_frequency_curve (
                curve_id,
                model_id,
                river_network_version_id,
                basin_version_id,
                river_segment_id,
                duration,
                method,
                sample_period_start,
                sample_period_end,
                sample_size,
                parameters_json,
                q2,
                q5,
                q10,
                q20,
                q50,
                q100,
                unit,
                quality_flag
            )
            VALUES (
                :curve_id,
                :model_id,
                :river_network_version_id,
                :basin_version_id,
                :river_segment_id,
                :duration,
                :method,
                :sample_period_start,
                :sample_period_end,
                :sample_size,
                :parameters_json,
                :q2,
                :q5,
                :q10,
                :q20,
                :q50,
                :q100,
                :unit,
                :quality_flag
            )
            ON CONFLICT (
                model_id,
                river_network_version_id,
                river_segment_id,
                duration,
                method,
                sample_period_start,
                sample_period_end
            ) DO UPDATE SET
                curve_id = EXCLUDED.curve_id,
                basin_version_id = EXCLUDED.basin_version_id,
                sample_size = EXCLUDED.sample_size,
                parameters_json = EXCLUDED.parameters_json,
                q2 = EXCLUDED.q2,
                q5 = EXCLUDED.q5,
                q10 = EXCLUDED.q10,
                q20 = EXCLUDED.q20,
                q50 = EXCLUDED.q50,
                q100 = EXCLUDED.q100,
                unit = EXCLUDED.unit,
                quality_flag = EXCLUDED.quality_flag
            """
        ),
        {
            "curve_id": curve_id,
            "model_id": curve_data["model_id"],
            "river_network_version_id": curve_data["river_network_version_id"],
            "basin_version_id": curve_data["basin_version_id"],
            "river_segment_id": curve_data["river_segment_id"],
            "duration": curve_data["duration"],
            "method": method,
            "sample_period_start": sample_period_start,
            "sample_period_end": sample_period_end,
            "sample_size": int(curve_data["sample_size"]),
            "parameters_json": json.dumps(parameters_json, sort_keys=True),
            "q2": quantiles.get("Q2"),
            "q5": quantiles.get("Q5"),
            "q10": quantiles.get("Q10"),
            "q20": quantiles.get("Q20"),
            "q50": quantiles.get("Q50"),
            "q100": quantiles.get("Q100"),
            "unit": str(curve_data.get("unit") or "m3/s"),
            "quality_flag": curve_data["quality_flag"],
        },
    )
    if curve_data.get("write_qc", True):
        write_frequency_qc_result(curve_id, curve_data, db_session)
    return curve_id


def write_frequency_qc_result(curve_id: str, curve_data: Mapping[str, Any], db_session: Session) -> None:
    checks_json = dict(curve_data.get("qc_checks") or {})
    quality_flag = str(curve_data.get("quality_flag") or "fit_failed")
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
                'flood_frequency',
                'flood_frequency_curve',
                :target_id,
                NULL,
                :passed,
                :severity,
                :checks_json,
                :message
            )
            """
        ),
        {
            "target_id": curve_id,
            "passed": quality_flag not in {"fit_failed", "no_valid_sample"},
            "severity": _qc_severity(quality_flag),
            "checks_json": json.dumps(checks_json, sort_keys=True),
            "message": f"Flood frequency fitting completed with quality_flag={quality_flag}.",
        },
    )


def supersede_old_curves(model_id: str, db_session: Session) -> int:
    result = db_session.execute(
        text(
            """
            UPDATE flood.flood_frequency_curve
            SET quality_flag = 'superseded_by_model_upgrade'
            WHERE model_id = :model_id
              AND quality_flag NOT IN ('superseded_by_model_upgrade', 'fit_failed')
            """
        ),
        {"model_id": model_id},
    )
    return int(result.rowcount or 0)


def fit_segment_duration(
    model_id: str,
    river_segment_id: str,
    duration: str,
    db_session: Session,
    method: str = "auto",
) -> dict[str, Any]:
    model = _load_model_context(db_session, model_id)
    annual = extract_annual_maxima_with_metadata(model_id, river_segment_id, duration, db_session)
    sample_years = [year for year, _value in annual.samples]
    period_start, period_end = _sample_period(annual)
    normalized_method = _normalize_method(method)
    saved_method = "P-III" if normalized_method == "auto" else normalized_method
    sample_quality = check_sample_size(len(annual.samples))

    if not annual.samples:
        curve_data = _curve_data(
            model=model,
            river_segment_id=river_segment_id,
            duration=duration,
            method=saved_method,
            sample_period_start=period_start,
            sample_period_end=period_end,
            sample_size=0,
            quantiles=_null_quantiles(),
            parameters_json={
                "n_samples": 0,
                "sample_years": [],
                "excluded_years": annual.excluded_years,
                "sample_quality": sample_quality.thresholds,
            },
            quality_flag="no_valid_sample",
            qc_checks=_qc_checks(sample_quality, None, "no_valid_sample"),
        )
        curve_id = save_frequency_curve(curve_data, db_session)
        return {"curve_id": curve_id, "quality_flag": "no_valid_sample", "method": saved_method}

    samples = [value for _year, value in annual.samples]
    fit = fit_frequency_curve(samples, normalized_method)
    if fit.quality_flag == "fit_failed":
        curve_data = _curve_data(
            model=model,
            river_segment_id=river_segment_id,
            duration=duration,
            method=saved_method,
            sample_period_start=period_start,
            sample_period_end=period_end,
            sample_size=len(samples),
            quantiles=_null_quantiles(),
            parameters_json={
                "n_samples": len(samples),
                "sample_years": sample_years,
                "excluded_years": annual.excluded_years,
                "sample_quality": sample_quality.thresholds,
                "fit_error": fit.error_message,
            },
            quality_flag="fit_failed",
            qc_checks=_qc_checks(sample_quality, None, "fit_failed", fit.error_message),
        )
        curve_id = save_frequency_curve(curve_data, db_session)
        return {"curve_id": curve_id, "quality_flag": "fit_failed", "method": saved_method}

    monotonicity = check_monotonicity(fit.quantiles)
    if normalized_method == "auto" and fit.method == "P-III" and monotonicity.quality_flag != "ok":
        gev = fit_frequency_curve(samples, "GEV")
        gev_monotonicity = check_monotonicity(gev.quantiles) if gev.quality_flag != "fit_failed" else monotonicity
        if gev.quality_flag != "fit_failed" and gev_monotonicity.quality_flag == "ok":
            fit = FitResult(
                method=gev.method,
                params=gev.params,
                quantiles=gev.quantiles,
                quality_flag="p3_fallback_gev",
                error_message="P-III monotonicity check failed.",
            )
            monotonicity = gev_monotonicity

    quality_flag = _curve_quality_flag(fit, sample_quality, monotonicity)
    parameters_json = {
        **fit.params,
        "n_samples": len(samples),
        "sample_years": sample_years,
        "excluded_years": annual.excluded_years,
        "sample_quality": sample_quality.thresholds,
        "monotonicity_corrections": monotonicity.corrections,
    }
    if fit.error_message:
        parameters_json["fit_warning"] = fit.error_message
    curve_data = _curve_data(
        model=model,
        river_segment_id=river_segment_id,
        duration=duration,
        method=fit.method,
        sample_period_start=period_start,
        sample_period_end=period_end,
        sample_size=len(samples),
        quantiles=monotonicity.corrected_quantiles,
        parameters_json=parameters_json,
        quality_flag=quality_flag,
        qc_checks=_qc_checks(sample_quality, monotonicity, quality_flag, fit.error_message),
    )
    curve_id = save_frequency_curve(curve_data, db_session)
    return {"curve_id": curve_id, "quality_flag": quality_flag, "method": fit.method}


def fit_curves(
    model_id: str,
    db_session: Session,
    segment_id: str | None = None,
    duration: str | None = None,
    method: str = "auto",
    dry_run: bool = False,
) -> FitCurvesStats:
    durations = [_validate_duration(duration)] if duration else list(DURATION_HOURS)
    segments = [segment_id] if segment_id else _segments_for_model(model_id, db_session)
    items: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    skipped = 0

    for segment in segments:
        for item_duration in durations:
            if dry_run:
                skipped += 1
                items.append({"river_segment_id": segment, "duration": item_duration, "status": "dry_run"})
                continue
            try:
                result = fit_segment_duration(model_id, str(segment), item_duration, db_session, method)
                items.append({"river_segment_id": segment, "duration": item_duration, **result})
                if result["quality_flag"] in {"fit_failed", "no_valid_sample"}:
                    failed += 1
                else:
                    succeeded += 1
            except Exception as error:
                failed += 1
                items.append({"river_segment_id": segment, "duration": item_duration, "error": str(error)})
    if not dry_run:
        db_session.commit()
    return FitCurvesStats(
        total_segments=len(segments),
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        items=items,
    )


def _annual_rows(
    model_id: str,
    river_segment_id: str,
    window_hours: int,
    db_session: Session,
) -> list[Mapping[str, Any]]:
    year_expr = _year_expr(db_session)
    if window_hours == 1:
        statement = text(
            f"""
            SELECT
                {year_expr} AS year,
                COUNT(DISTINCT rt.valid_time) AS available_hours,
                MAX(rt.value) AS annual_max
            FROM hydro.river_timeseries rt
            JOIN hydro.hydro_run hr ON hr.run_id = rt.run_id
            WHERE hr.run_type = 'hindcast'
              AND hr.model_id = :model_id
              AND rt.river_segment_id = :river_segment_id
              AND rt.variable = 'q_down'
              AND COALESCE(rt.quality_flag, 'ok') = 'ok'
            GROUP BY year
            ORDER BY year
            """
        )
    else:
        min_window_count = math.ceil(window_hours * 0.9)
        statement = text(
            f"""
            WITH hourly AS (
                SELECT
                    {year_expr} AS year,
                    rt.valid_time,
                    rt.value,
                    AVG(rt.value) OVER (
                        PARTITION BY rt.river_segment_id, {year_expr}
                        ORDER BY rt.valid_time
                        ROWS BETWEEN {window_hours - 1} PRECEDING AND CURRENT ROW
                    ) AS window_avg,
                    COUNT(rt.value) OVER (
                        PARTITION BY rt.river_segment_id, {year_expr}
                        ORDER BY rt.valid_time
                        ROWS BETWEEN {window_hours - 1} PRECEDING AND CURRENT ROW
                    ) AS window_count
                FROM hydro.river_timeseries rt
                JOIN hydro.hydro_run hr ON hr.run_id = rt.run_id
                WHERE hr.run_type = 'hindcast'
                  AND hr.model_id = :model_id
                  AND rt.river_segment_id = :river_segment_id
                  AND rt.variable = 'q_down'
                  AND COALESCE(rt.quality_flag, 'ok') = 'ok'
            )
            SELECT
                year,
                COUNT(DISTINCT valid_time) AS available_hours,
                MAX(CASE WHEN window_count >= {min_window_count} THEN window_avg END) AS annual_max
            FROM hourly
            GROUP BY year
            ORDER BY year
            """
        )
    return list(
        db_session.execute(
            statement,
            {"model_id": model_id, "river_segment_id": river_segment_id},
        ).mappings()
    )


def _year_expr(db_session: Session) -> str:
    dialect = _dialect_name(db_session)
    if dialect == "sqlite":
        return "CAST(strftime('%Y', rt.valid_time) AS INTEGER)"
    return "CAST(EXTRACT(YEAR FROM rt.valid_time) AS INTEGER)"


def _load_model_context(db_session: Session, model_id: str) -> dict[str, Any]:
    row = db_session.execute(
        text(
            """
            SELECT model_id, basin_version_id, river_network_version_id
            FROM core.model_instance
            WHERE model_id = :model_id
            LIMIT 1
            """
        ),
        {"model_id": model_id},
    ).mappings().first()
    if row is None:
        raise FrequencyFitError(f"Model not found: {model_id}")
    return dict(row)


def _segments_for_model(model_id: str, db_session: Session) -> list[str]:
    model = _load_model_context(db_session, model_id)
    if _table_exists(db_session, "core", "river_segment"):
        rows = db_session.execute(
            text(
                """
                SELECT river_segment_id
                FROM core.river_segment
                WHERE river_network_version_id = :river_network_version_id
                ORDER BY river_segment_id
                """
            ),
            {"river_network_version_id": model["river_network_version_id"]},
        ).mappings()
        segments = [str(row["river_segment_id"]) for row in rows]
        if segments:
            return segments

    rows = db_session.execute(
        text(
            """
            SELECT DISTINCT rt.river_segment_id
            FROM hydro.river_timeseries rt
            JOIN hydro.hydro_run hr ON hr.run_id = rt.run_id
            WHERE hr.run_type = 'hindcast'
              AND hr.model_id = :model_id
              AND rt.variable = 'q_down'
            ORDER BY rt.river_segment_id
            """
        ),
        {"model_id": model_id},
    ).mappings()
    return [str(row["river_segment_id"]) for row in rows]


def _table_exists(db_session: Session, schema: str, table_name: str) -> bool:
    try:
        return inspect(db_session.get_bind()).has_table(table_name, schema=schema)
    except SQLAlchemyError:
        return False


def _duration_hours(duration: str) -> int:
    return DURATION_HOURS[_validate_duration(duration)]


def _validate_duration(duration: str | None) -> str:
    if duration not in DURATION_HOURS:
        raise FrequencyFitError(f"Unsupported duration: {duration}")
    return str(duration)


def _normalize_method(method: str) -> str:
    normalized = str(method).strip()
    if normalized.lower() == "auto":
        return "auto"
    if normalized.upper() in {"P-III", "PIII", "P3"}:
        return "P-III"
    if normalized.upper() == "GEV":
        return "GEV"
    raise FrequencyFitError(f"Unsupported fitting method: {method}")


def _fit_data(samples: Sequence[float]) -> list[float]:
    data = [float(sample) for sample in samples if math.isfinite(float(sample))]
    if not data:
        raise FrequencyFitError("No finite samples available for fitting.")
    if len(set(data)) < 2:
        raise FrequencyFitError("At least two distinct samples are required for fitting.")
    return data


def _validate_distribution_params(params: Mapping[str, float], scale_key: str) -> None:
    if any(not math.isfinite(value) for value in params.values()):
        raise FrequencyFitError("Distribution fit returned non-finite parameters.")
    if params[scale_key] <= 0:
        raise FrequencyFitError("Distribution fit returned non-positive scale.")


def _quantiles(distribution: Any, params: tuple[float, float, float]) -> dict[str, float]:
    quantiles: dict[str, float] = {}
    for return_period in RETURN_PERIODS:
        value = float(distribution.ppf(1.0 - 1.0 / return_period, *params))
        if not math.isfinite(value):
            raise FrequencyFitError("Distribution fit returned non-finite quantiles.")
        quantiles[f"Q{return_period}"] = value
    return quantiles


def _fit_failed(message: str, method: str = "fit_failed") -> FitResult:
    return FitResult(
        method=method,
        params={},
        quantiles=_null_quantiles(),
        quality_flag="fit_failed",
        error_message=message,
    )


def _null_quantiles() -> dict[str, None]:
    return {key: None for key in QUANTILE_KEYS}


def _normalize_quantile_map(curve_data: Mapping[str, Any]) -> dict[str, float | None]:
    source = dict(curve_data.get("quantiles") or {})
    return {key: source.get(key, source.get(key.lower(), curve_data.get(key.lower()))) for key in QUANTILE_KEYS}


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _next_greater_value(quantiles: Mapping[str, float | None], index: int, previous: float) -> float | None:
    for next_key in QUANTILE_KEYS[index + 1 :]:
        next_value = quantiles.get(next_key)
        if next_value is not None and next_value > previous:
            return next_value
    return None


def _expected_hours(year: int) -> int:
    return 8784 if _is_leap_year(year) else 8760


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _sample_period(annual: AnnualMaximaResult) -> tuple[date, date]:
    years = annual.observed_years or [year for year, _value in annual.samples]
    if not years:
        current_year = datetime.now(tz=UTC).year
        return date(current_year, 1, 1), date(current_year, 12, 31)
    return date(min(years), 1, 1), date(max(years), 12, 31)


def _curve_data(
    *,
    model: Mapping[str, Any],
    river_segment_id: str,
    duration: str,
    method: str,
    sample_period_start: date,
    sample_period_end: date,
    sample_size: int,
    quantiles: Mapping[str, float | None],
    parameters_json: Mapping[str, Any],
    quality_flag: str,
    qc_checks: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "model_id": model["model_id"],
        "river_network_version_id": model["river_network_version_id"],
        "basin_version_id": model["basin_version_id"],
        "river_segment_id": river_segment_id,
        "duration": duration,
        "method": method,
        "sample_period_start": sample_period_start,
        "sample_period_end": sample_period_end,
        "sample_size": sample_size,
        "quantiles": dict(quantiles),
        "parameters_json": dict(parameters_json),
        "quality_flag": quality_flag,
        "qc_checks": dict(qc_checks),
    }


def _curve_quality_flag(
    fit: FitResult,
    sample_quality: SampleQuality,
    monotonicity: MonotonicityResult,
) -> str:
    if monotonicity.quality_flag != "ok":
        return monotonicity.quality_flag
    if fit.quality_flag != "ok":
        return fit.quality_flag
    if sample_quality.quality_flag != "ok":
        return sample_quality.quality_flag
    return "ok"


def _qc_checks(
    sample_quality: SampleQuality,
    monotonicity: MonotonicityResult | None,
    quality_flag: str,
    fit_error: str | None = None,
) -> dict[str, Any]:
    return {
        "sample_size_check": {
            "n_samples": sample_quality.n_samples,
            "quality_flag": sample_quality.quality_flag,
            "thresholds": sample_quality.thresholds,
        },
        "monotonicity_check": {
            "quality_flag": monotonicity.quality_flag if monotonicity is not None else "not_run",
            "corrections": monotonicity.corrections if monotonicity is not None else [],
        },
        "fit_validity_check": {
            "quality_flag": quality_flag,
            "fit_error": fit_error,
        },
    }


def _qc_severity(quality_flag: str) -> str:
    if quality_flag == "ok":
        return "info"
    if quality_flag == "fit_failed":
        return "error"
    return "warning"


def _curve_id(
    model_id: str,
    river_network_version_id: str,
    river_segment_id: str,
    duration: str,
    method: str,
    sample_period_start: date,
    sample_period_end: date,
) -> str:
    return (
        f"ffc_{model_id}_{river_network_version_id}_{river_segment_id}_{duration}_{method}_"
        f"{sample_period_start.isoformat()}_{sample_period_end.isoformat()}"
    )


def _coerce_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value)[:10])


def _dialect_name(db_session: Session) -> str:
    return str(getattr(getattr(db_session.get_bind(), "dialect", None), "name", ""))
