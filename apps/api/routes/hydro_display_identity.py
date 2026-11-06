"""Source-identity digests and existence probes for the display tile routes.

Split out of `apps/api/routes/hydro_display.py` (#2026). Every consumer of these
helpers is a route handler that stays on the facade, so no patch target moves.
`_require_display_ready` deliberately stays on the facade: it calls `_run_row`,
which stays.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from apps.api.errors import ApiError
from apps.api.routes.hydro_display_instants import _mvt_time_text
from packages.common.river_ts_render import render_river_ts_sql
from services.tiles.mvt import public_hydro_layer_id


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
            "valid_time": _mvt_time_text(valid_time),
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
