from __future__ import annotations

import json
import os
from collections.abc import Generator
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from apps.api.errors import ApiError
from apps.api.routes.pipeline import _ok

router = APIRouter(tags=["flood-alerts"])

WARNING_COLORS = {
    "normal": "#808080",
    "elevated": "#4A90D9",
    "watch": "#FFD700",
    "warning": "#FF8C00",
    "high_risk": "#FF4500",
    "severe": "#DC143C",
    "extreme": "#800080",
}
WARNING_LEVELS = tuple(WARNING_COLORS)
USABLE_CURVE_FLAGS = {"ok", "partial_sample", "monotonicity_corrected"}
NO_USABLE_CURVE_NOTE = "No usable frequency curves available"
NO_SEGMENT_CURVE_NOTE = "No frequency curve available for this segment"


class AlertLevelCount(BaseModel):
    level: str
    count: int
    color: str


class AlertSummaryResponse(BaseModel):
    run_id: str
    levels: list[AlertLevelCount]
    total_segments: int
    usable_curves: int
    unavailable_count: int = 0
    quality_note: str | None = None


class RankingItem(BaseModel):
    rank: int
    river_segment_id: str
    segment_id: str
    segment_name: str | None = None
    basin_version_id: str
    q_value: float
    q_unit: str
    return_period: float | None = None
    warning_level: str | None = None
    duration: str
    valid_time: str


class RankingResponse(BaseModel):
    items: list[RankingItem]
    total: int
    limit: int
    offset: int


class GeoPoint(BaseModel):
    type: str = "Point"
    coordinates: list[float]


class SegmentAlert(BaseModel):
    river_segment_id: str
    segment_id: str
    segment_name: str | None = None
    basin_version_id: str
    q_value: float
    return_period: float | None = None
    warning_level: str | None = None
    valid_time: str
    geom_centroid: GeoPoint | None = None


class SegmentListResponse(BaseModel):
    segments: list[SegmentAlert]
    total: int


class FrequencyThresholds(BaseModel):
    Q2: float | None = None
    Q5: float | None = None
    Q10: float | None = None
    Q20: float | None = None
    Q50: float | None = None
    Q100: float | None = None
    sample_quality: dict[str, Any] | None = None


class TimelinePoint(BaseModel):
    valid_time: str
    return_period: float | None = None
    warning_level: str | None = None
    q_value: float


class TimelineResponse(BaseModel):
    run_id: str
    segment_id: str
    river_segment_id: str
    timesteps: list[TimelinePoint]
    timeline: list[TimelinePoint]
    peak: TimelinePoint | None = None
    frequency_thresholds: FrequencyThresholds | None = None
    quality_note: str | None = None


class TileFeature(BaseModel):
    type: str = "Feature"
    properties: dict[str, Any]
    geometry: dict[str, Any] | None = None


class TileFeatureCollection(BaseModel):
    type: str = "FeatureCollection"
    features: list[TileFeature] = Field(default_factory=list)


@lru_cache
def _engine(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def get_flood_alert_session() -> Generator[Session, None, None]:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ApiError(
            status_code=500,
            code="DATABASE_URL_MISSING",
            message="DATABASE_URL is required for flood alert API operations.",
        )
    with Session(_engine(database_url)) as session:
        yield session


@router.get("/api/v1/flood-alerts/summary", response_model=dict[str, Any])
def flood_alert_summary(
    request: Request,
    run_id: str = Query(...),
    threshold: str | float | None = Query(default=None),
    valid_time: datetime | None = Query(default=None),
    session: Session = Depends(get_flood_alert_session),
) -> dict[str, Any]:
    run = _require_frequency_ready(session, run_id)
    min_return_period = _parse_threshold(threshold)
    rows = session.execute(
        text(
            f"""
            SELECT warning_level, COUNT(*) AS count
            FROM flood.return_period_result
            WHERE run_id = :run_id
              AND {_time_filter_sql(valid_time)}
              AND (:min_return_period IS NULL OR return_period >= :min_return_period)
              AND quality_flag IN :usable_flags
              AND warning_level IS NOT NULL
            GROUP BY warning_level
            """
        ).bindparams(bindparam("usable_flags", expanding=True)),
        {
            "run_id": run_id,
            "valid_time": valid_time,
            "max_over_window": True,
            "min_return_period": min_return_period,
            "usable_flags": tuple(USABLE_CURVE_FLAGS),
        },
    ).mappings()
    counts = {str(row["warning_level"]): int(row["count"]) for row in rows}
    total_segments = _result_segment_count(session, run_id, run, valid_time=valid_time)
    usable_curves = _usable_curve_count(session, run_id, valid_time=valid_time)
    unavailable_count = max(total_segments - usable_curves, 0)
    data = AlertSummaryResponse(
        run_id=run_id,
        levels=[
            AlertLevelCount(level=level, count=counts.get(level, 0), color=WARNING_COLORS[level])
            for level in WARNING_LEVELS
        ],
        total_segments=total_segments,
        usable_curves=usable_curves,
        unavailable_count=unavailable_count,
        quality_note=NO_USABLE_CURVE_NOTE if total_segments > 0 and usable_curves == 0 else None,
    )
    return _ok(request, data.model_dump())


@router.get("/api/v1/flood-alerts/ranking", response_model=dict[str, Any])
def flood_alert_ranking(
    request: Request,
    run_id: str = Query(...),
    limit: int = Query(default=10, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    basin_id: str | None = Query(default=None),
    valid_time: datetime | None = Query(default=None),
    session: Session = Depends(get_flood_alert_session),
) -> dict[str, Any]:
    _require_frequency_ready(session, run_id)
    where_sql, params = _ranking_filters(run_id=run_id, basin_id=basin_id, valid_time=valid_time)
    count_statement = text(f"SELECT COUNT(*) AS count FROM flood.return_period_result r {where_sql}").bindparams(
        bindparam("usable_flags", expanding=True)
    )
    total = int(
        session.execute(
            count_statement,
            params,
        )
        .mappings()
        .one()["count"]
    )
    rows = session.execute(
        text(
            f"""
            SELECT r.river_segment_id, r.basin_version_id, r.q_value, r.q_unit, r.return_period,
                   r.warning_level, r.duration, r.valid_time, rs.properties_json
            FROM flood.return_period_result r
            LEFT JOIN core.river_segment rs
              ON rs.river_segment_id = r.river_segment_id
             AND rs.river_network_version_id = r.river_network_version_id
            {where_sql}
            ORDER BY r.return_period DESC NULLS LAST, r.q_value DESC, r.river_segment_id
            LIMIT :limit OFFSET :offset
            """
        ).bindparams(bindparam("usable_flags", expanding=True)),
        {**params, "limit": limit, "offset": offset},
    ).mappings()
    items = [
        RankingItem(
            rank=offset + index,
            river_segment_id=str(row["river_segment_id"]),
            segment_id=str(row["river_segment_id"]),
            segment_name=_segment_name(row.get("properties_json")),
            basin_version_id=str(row["basin_version_id"]),
            q_value=float(row["q_value"]),
            q_unit=str(row["q_unit"] or "m3/s"),
            return_period=_optional_float(row["return_period"]),
            warning_level=_optional_str(row["warning_level"]),
            duration=str(row["duration"]),
            valid_time=_format_time(row["valid_time"]),
        )
        for index, row in enumerate(rows, start=1)
    ]
    return _ok(request, RankingResponse(items=items, total=total, limit=limit, offset=offset).model_dump())


@router.get("/api/v1/flood-alerts/segments", response_model=dict[str, Any])
def flood_alert_segments(
    request: Request,
    run_id: str = Query(...),
    min_return_period: float | None = Query(default=None, ge=0),
    warning_level: str | None = Query(default=None),
    valid_time: datetime | None = Query(default=None),
    session: Session = Depends(get_flood_alert_session),
) -> dict[str, Any]:
    _require_frequency_ready(session, run_id)
    geom_sql, centroid_sql = _geometry_select_sql(session)
    levels = _split_csv(warning_level)
    params: dict[str, Any] = {
        "run_id": run_id,
        "valid_time": valid_time,
        "min_return_period": min_return_period,
        "levels": tuple(levels),
    }
    level_filter = "AND r.warning_level IN :levels" if levels else ""
    statement = text(
        f"""
            SELECT r.river_segment_id, r.basin_version_id, r.q_value, r.return_period,
                   r.warning_level, r.valid_time, rs.properties_json, {centroid_sql} AS geom_centroid,
                   {geom_sql} AS geom_json
            FROM flood.return_period_result r
            LEFT JOIN core.river_segment rs
              ON rs.river_segment_id = r.river_segment_id
             AND rs.river_network_version_id = r.river_network_version_id
            WHERE r.run_id = :run_id
              AND {_time_filter_sql(valid_time)}
              AND (:min_return_period IS NULL OR r.return_period >= :min_return_period)
              {level_filter}
              AND r.quality_flag IN :usable_flags
            ORDER BY r.return_period DESC NULLS LAST, r.river_segment_id
            """
    ).bindparams(bindparam("usable_flags", expanding=True))
    if levels:
        statement = statement.bindparams(bindparam("levels", expanding=True))
    rows = session.execute(
        statement,
        {**params, "max_over_window": True, "usable_flags": tuple(USABLE_CURVE_FLAGS)},
    ).mappings()
    segments = [
        SegmentAlert(
            river_segment_id=str(row["river_segment_id"]),
            segment_id=str(row["river_segment_id"]),
            segment_name=_segment_name(row.get("properties_json")),
            basin_version_id=str(row["basin_version_id"]),
            q_value=float(row["q_value"]),
            return_period=_optional_float(row["return_period"]),
            warning_level=_optional_str(row["warning_level"]),
            valid_time=_format_time(row["valid_time"]),
            geom_centroid=_centroid_payload(row.get("geom_centroid") or row.get("geom_json")),
        )
        for row in rows
    ]
    return _ok(request, SegmentListResponse(segments=segments, total=len(segments)).model_dump())


@router.get("/api/v1/flood-alerts/timeline", response_model=dict[str, Any])
def flood_alert_timeline(
    request: Request,
    run_id: str = Query(...),
    segment_id: str = Query(...),
    session: Session = Depends(get_flood_alert_session),
) -> dict[str, Any]:
    _require_frequency_ready(session, run_id)
    rows = list(
        session.execute(
            text(
                """
                SELECT river_segment_id, valid_time, q_value, return_period, warning_level, model_id,
                       river_network_version_id, basin_version_id, duration
                FROM flood.return_period_result
                WHERE run_id = :run_id
                  AND river_segment_id = :segment_id
                  AND max_over_window = :max_over_window
                ORDER BY valid_time
                """
            ),
            {"run_id": run_id, "segment_id": segment_id, "max_over_window": False},
        ).mappings()
    )
    if not rows:
        rows = list(
            session.execute(
                text(
                    """
                    SELECT river_segment_id, valid_time, q_value, return_period, warning_level, model_id,
                           river_network_version_id, basin_version_id, duration
                    FROM flood.return_period_result
                    WHERE run_id = :run_id
                      AND river_segment_id = :segment_id
                    ORDER BY max_over_window, valid_time
                    """
                ),
                {"run_id": run_id, "segment_id": segment_id},
            ).mappings()
        )
    timesteps = [
        TimelinePoint(
            valid_time=_format_time(row["valid_time"]),
            q_value=float(row["q_value"]),
            return_period=_optional_float(row["return_period"]),
            warning_level=_optional_str(row["warning_level"]),
        )
        for row in rows
    ]
    peak = max(timesteps, key=lambda point: point.return_period or -1, default=None)
    thresholds = _frequency_thresholds_for_result(session, rows[0]) if rows else None
    data = TimelineResponse(
        run_id=run_id,
        segment_id=segment_id,
        river_segment_id=segment_id,
        timesteps=timesteps,
        timeline=timesteps,
        peak=peak,
        frequency_thresholds=thresholds,
        quality_note=NO_SEGMENT_CURVE_NOTE if thresholds is None else None,
    )
    return _ok(request, data.model_dump())


@router.get(
    "/api/v1/tiles/flood-return-period/{run_id}/{duration}/{valid_time}/{z}/{x}/{y}.pbf",
    response_model=TileFeatureCollection,
)
def flood_return_period_tile(
    run_id: str,
    duration: str,
    valid_time: datetime,
    z: int,
    x: int,
    y: int,
    session: Session = Depends(get_flood_alert_session),
) -> JSONResponse:
    del z, x, y
    _require_frequency_ready(session, run_id)
    geom_sql, _centroid_sql = _geometry_select_sql(session)
    rows = session.execute(
        text(
            f"""
            SELECT r.river_segment_id, r.return_period, r.warning_level, r.q_value, {geom_sql} AS geom_json
            FROM flood.return_period_result r
            LEFT JOIN core.river_segment rs
              ON rs.river_segment_id = r.river_segment_id
             AND rs.river_network_version_id = r.river_network_version_id
            WHERE r.run_id = :run_id
              AND r.duration = :duration
              AND r.valid_time = :valid_time
            ORDER BY r.river_segment_id
            """
        ),
        {"run_id": run_id, "duration": duration, "valid_time": valid_time},
    ).mappings()
    payload = TileFeatureCollection(
        features=[
            TileFeature(
                properties={
                    "river_segment_id": str(row["river_segment_id"]),
                    "return_period": _optional_float(row["return_period"]),
                    "warning_level": _optional_str(row["warning_level"]),
                    "q_value": float(row["q_value"]),
                },
                geometry=_geojson_geometry(row.get("geom_json")),
            )
            for row in rows
        ]
    )
    return JSONResponse(content=payload.model_dump(), media_type="application/json")


def _require_frequency_ready(session: Session, run_id: str) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            SELECT h.run_id, h.status, h.model_id, h.basin_version_id, mi.river_network_version_id
            FROM hydro.hydro_run h
            LEFT JOIN core.model_instance mi ON mi.model_id = h.model_id
            WHERE h.run_id = :run_id
            LIMIT 1
            """
        ),
        {"run_id": run_id},
    ).mappings().first()
    if row is None:
        raise ApiError(
            status_code=404,
            code="RUN_NOT_FOUND",
            message=f"Run not found: {run_id}",
            details={"run_id": run_id},
        )
    if str(row["status"]) != "frequency_done":
        raise ApiError(
            status_code=409,
            code="FREQUENCY_NOT_COMPUTED",
            message="Return period results not yet available for this run",
            details={"run_id": run_id, "status": row["status"]},
        )
    return dict(row)


def _time_filter_sql(valid_time: datetime | None, *, alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    if valid_time is not None:
        return f"{prefix}valid_time = :valid_time"
    return f"{prefix}max_over_window = :max_over_window"


def _ranking_filters(*, run_id: str, basin_id: str | None, valid_time: datetime | None) -> tuple[str, dict[str, Any]]:
    clauses = [
        "r.run_id = :run_id",
        _time_filter_sql(valid_time, alias="r"),
        "r.quality_flag IN :usable_flags",
    ]
    params: dict[str, Any] = {
        "run_id": run_id,
        "valid_time": valid_time,
        "max_over_window": True,
        "usable_flags": tuple(USABLE_CURVE_FLAGS),
    }
    if basin_id is not None:
        clauses.append(
            """
            EXISTS (
                SELECT 1 FROM core.basin_version bv
                WHERE bv.basin_version_id = r.basin_version_id
                  AND bv.basin_id = :basin_id
            )
            """
        )
        params["basin_id"] = basin_id
    return f"WHERE {' AND '.join(clauses)}", params


def _result_segment_count(
    session: Session,
    run_id: str,
    run: dict[str, Any],
    *,
    valid_time: datetime | None,
) -> int:
    result_count = int(
        session.execute(
            text(
                f"""
                SELECT COUNT(DISTINCT river_segment_id) AS count
                FROM flood.return_period_result
                WHERE run_id = :run_id
                  AND {_time_filter_sql(valid_time)}
                """
            ),
            {"run_id": run_id, "valid_time": valid_time, "max_over_window": True},
        )
        .mappings()
        .one()["count"]
    )
    if result_count:
        return result_count
    river_network_version_id = run.get("river_network_version_id")
    if river_network_version_id is None:
        return 0
    return int(
        session.execute(
            text(
                """
                SELECT COUNT(*) AS count
                FROM core.river_segment
                WHERE river_network_version_id = :river_network_version_id
                """
            ),
            {"river_network_version_id": river_network_version_id},
        )
        .mappings()
        .one()["count"]
    )


def _usable_curve_count(session: Session, run_id: str, *, valid_time: datetime | None) -> int:
    return int(
        session.execute(
            text(
                f"""
                SELECT COUNT(DISTINCT river_segment_id) AS count
                FROM flood.return_period_result
                WHERE run_id = :run_id
                  AND {_time_filter_sql(valid_time)}
                  AND quality_flag IN :usable_flags
                """
            ).bindparams(bindparam("usable_flags", expanding=True)),
            {
                "run_id": run_id,
                "valid_time": valid_time,
                "max_over_window": True,
                "usable_flags": tuple(USABLE_CURVE_FLAGS),
            },
        )
        .mappings()
        .one()["count"]
    )


def _frequency_thresholds_for_result(session: Session, row: Any) -> FrequencyThresholds | None:
    curve = session.execute(
        text(
            """
            SELECT q2, q5, q10, q20, q50, q100, parameters_json
            FROM flood.flood_frequency_curve
            WHERE model_id = :model_id
              AND river_network_version_id = :river_network_version_id
              AND river_segment_id = :river_segment_id
              AND duration = :duration
              AND quality_flag IN :usable_flags
            ORDER BY sample_period_end DESC
            LIMIT 1
            """
        ).bindparams(bindparam("usable_flags", expanding=True)),
        {
            "model_id": row["model_id"],
            "river_network_version_id": row["river_network_version_id"],
            "river_segment_id": row.get("river_segment_id") or row.get("segment_id"),
            "duration": row.get("duration") or "1h",
            "usable_flags": tuple(USABLE_CURVE_FLAGS),
        },
    ).mappings().first()
    if curve is None:
        return None
    parameters = _json_dict(curve.get("parameters_json"))
    return FrequencyThresholds(
        Q2=_optional_float(curve["q2"]),
        Q5=_optional_float(curve["q5"]),
        Q10=_optional_float(curve["q10"]),
        Q20=_optional_float(curve["q20"]),
        Q50=_optional_float(curve["q50"]),
        Q100=_optional_float(curve["q100"]),
        sample_quality=parameters.get("sample_quality") if isinstance(parameters, dict) else None,
    )


def _parse_threshold(value: str | float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    normalized = value.strip().upper()
    if normalized.startswith("Q"):
        normalized = normalized[1:]
    try:
        return float(normalized)
    except ValueError as error:
        raise ApiError(
            status_code=422,
            code="VALIDATION_ERROR",
            message="threshold must be numeric or a Q-prefixed return period such as Q10.",
            details={"threshold": value},
        ) from error


def _geometry_select_sql(session: Session) -> tuple[str, str]:
    if session.get_bind().dialect.name == "sqlite":
        return "rs.geom", "rs.geom"
    return "ST_AsGeoJSON(rs.geom)::text", "ST_AsGeoJSON(ST_Centroid(rs.geom))::text"


def _centroid_payload(value: Any) -> GeoPoint | None:
    geometry = _geojson_geometry(value)
    if geometry is None:
        return None
    if geometry.get("type") == "Point":
        coordinates = geometry.get("coordinates") or []
        return GeoPoint(coordinates=[float(coordinates[0]), float(coordinates[1])])
    if geometry.get("type") == "LineString":
        coordinates = geometry.get("coordinates") or []
        if not coordinates:
            return None
        lon = sum(float(point[0]) for point in coordinates) / len(coordinates)
        lat = sum(float(point[1]) for point in coordinates) / len(coordinates)
        return GeoPoint(coordinates=[lon, lat])
    return None


def _geojson_geometry(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        if stripped.startswith("{"):
            parsed = json.loads(stripped)
            return parsed if isinstance(parsed, dict) else None
    return None


def _segment_name(value: Any) -> str | None:
    properties = _json_dict(value)
    for key in ("name", "segment_name", "display_name"):
        candidate = properties.get(key)
        if candidate:
            return str(candidate)
    return None


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _split_csv(value: str | None) -> list[str]:
    if value is None:
        return []
    return [token.strip() for token in value.split(",") if token.strip()]


def _optional_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _format_time(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    text_value = str(value).replace("+00:00", "Z")
    if len(text_value) >= 19 and text_value[10] == " ":
        text_value = f"{text_value[:10]}T{text_value[11:]}"
    return text_value
