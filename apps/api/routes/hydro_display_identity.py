"""Source-identity digests and existence probes for the display tile routes.

Split out of `apps/api/routes/hydro_display.py` (#2026). Every consumer of these
helpers is a route handler that stays on the facade, so no patch target moves.
`_require_display_ready` deliberately stays on the facade: it calls `_run_row`,
which stays.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.errors import ApiError
from apps.api.routes.hydro_display_instants import _format_time
from packages.common.river_ts_render import render_river_ts_sql
from services.tiles.mvt import MVT_MAX_FEATURES, public_hydro_layer_id


def _station_source_version(session: Session, basin_version_id: str) -> str:
    try:
        row_limit = MVT_MAX_FEATURES + 1
        if session.get_bind().dialect.name == "sqlite":
            rows = session.execute(
                text(
                    """
                    SELECT station_id, basin_version_id, COALESCE(station_name, '') AS station_name,
                           station_role, active_flag, geom, created_at
                    FROM met.met_station
                    WHERE basin_version_id = :basin_version_id
                      AND active_flag = 1
                    ORDER BY station_id
                    LIMIT :limit
                    """
                ),
                {"basin_version_id": basin_version_id, "limit": row_limit},
            ).mappings().all()
        else:
            rows = session.execute(
                text(
                    """
                    SELECT station_id, basin_version_id, COALESCE(station_name, '') AS station_name,
                           station_role, active_flag, encode(ST_AsEWKB(geom), 'hex') AS geom, created_at
                    FROM met.met_station
                    WHERE basin_version_id = :basin_version_id
                      AND active_flag = true
                    ORDER BY station_id
                    LIMIT :limit
                    """
                ),
                {"basin_version_id": basin_version_id, "limit": row_limit},
            ).mappings().all()
    except SQLAlchemyError as exc:
        try:
            session.rollback()
        except SQLAlchemyError:
            pass
        raise ApiError(
            status_code=424,
            code="MVT_LIVE_POSTGIS_UNAVAILABLE",
            message="Station MVT source inventory is unavailable for canonical .pbf tile generation.",
            details={"layer_id": "met-stations", "basin_version_id": basin_version_id},
        ) from exc

    if not rows:
        raise ApiError(
            status_code=404,
            code="MVT_SOURCE_IDENTITY_NOT_FOUND",
            message="Station MVT source identity was not found for the requested basin version.",
            details={"layer_id": "met-stations", "basin_version_id": basin_version_id},
        )
    if len(rows) > MVT_MAX_FEATURES:
        raise ApiError(
            status_code=413,
            code="MVT_TILE_BUDGET_EXCEEDED",
            message="Station MVT source inventory exceeded the configured feature budget.",
            details={"layer_id": "met-stations", "basin_version_id": basin_version_id},
        )
    basis = {
        "rows": [
            [
                row.get("station_id"),
                row.get("basin_version_id"),
                row.get("station_name"),
                row.get("station_role"),
                _station_active_flag(row.get("active_flag")),
                row.get("geom"),
                _format_time(row.get("created_at")) if row.get("created_at") is not None else None,
            ]
            for row in rows
        ],
    }
    digest = hashlib.sha256(
        json.dumps(basis, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()[:16]
    return f"met-stations:{digest}:{basin_version_id}:{len(rows)}"


def _station_active_flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "t", "true", "yes"}
    return bool(value)


def _require_hydro_mvt_source_identity(
    session: Session,
    *,
    run_id: str,
    variable: str,
    valid_time: datetime,
    basin_version_id: str,
    river_network_version_id: str,
) -> None:
    # Issue #1341: the existence probe filters on the integer surrogate keys /
    # enum column served by migration 000051, resolving the caller's text
    # identity through the authority tables inside the query. An unknown
    # identity or an out-of-vocabulary variable makes a resolution subquery
    # NULL, so the probe finds no row and the route still answers 404 —
    # the same outcome the text predicates produced, never a SQL error.
    #
    # The redundant text conjuncts that used to sit beside each key predicate
    # went with the text columns in #1342's contract
    # (task 6.3); the fact table is key/enum-only and there is one store.
    row = session.execute(
        text(
            render_river_ts_sql(
                """
            SELECT 1
            FROM hydro.river_timeseries
            WHERE run_key = (
                      SELECT run_key FROM hydro.hydro_run WHERE run_id = :run_id
                  )
              AND basin_version_key = (
                      SELECT basin_version_key FROM core.basin_version
                      WHERE basin_version_id = :basin_version_id
                  )
              AND river_network_version_key = (
                      SELECT river_network_version_key FROM core.river_network_version
                      WHERE river_network_version_id = :river_network_version_id
                  )
              AND variable_e = (
                      SELECT e FROM unnest(enum_range(NULL::hydro.river_variable)) e
                      WHERE e::text = :variable
                  )
              AND valid_time = :valid_time
            LIMIT 1
            """,
                "narrow",
                entry="hydro_display:mvt_source_identity_probe",
            ).sql
        ),
        {
            "run_id": run_id,
            "variable": variable,
            "valid_time": valid_time,
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
        },
    ).first()
    if row is not None:
        return
    raise ApiError(
        status_code=404,
        code="MVT_SOURCE_IDENTITY_NOT_FOUND",
        message="Hydrological MVT source/time identity was not found for the requested route.",
        details={
            "layer_id": public_hydro_layer_id(variable),
            "run_id": run_id,
            "variable": variable,
            "valid_time": _format_time(valid_time),
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
        },
    )


def _require_run_source_identity(run: dict[str, Any] | Any, *, layer_id: str) -> tuple[str, str]:
    basin_version_id = run.get("basin_version_id")
    river_network_version_id = run.get("river_network_version_id")
    if basin_version_id and river_network_version_id:
        return str(basin_version_id), str(river_network_version_id)
    raise ApiError(
        status_code=404,
        code="MVT_SOURCE_IDENTITY_NOT_FOUND",
        message="Run-scoped MVT source identity was not found for the selected ready run.",
        details={
            "layer_id": layer_id,
            "run_id": run.get("run_id"),
            "basin_version_id": basin_version_id,
            "river_network_version_id": river_network_version_id,
        },
    )
