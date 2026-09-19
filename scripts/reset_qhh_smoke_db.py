from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    # Run directly (`python scripts/reset_qhh_smoke_db.py`) as well as imported
    # as `scripts.reset_qhh_smoke_db`, so the `packages.common` import below
    # resolves either way.
    sys.path.insert(0, str(ROOT))

from packages.common.forcing_ts_render import (  # noqa: E402
    FORCING_TABLE_LEGACY,
    FORCING_TABLE_TOKEN,
    ForcingTemplatePair,
    render_forcing_ts_sql,
)

# Reader #9 (#1990 task 7.2). ONE `_delete`, and river now has one too: river's
# two-group split read its routing column to decide which physical table a run's
# rows lived in, and #1342's contract (task 6.3) dropped both. Forcing's own
# routing column `met.forcing_version.timeseries_store` arrives with task 7.3 and
# the split lands with it; here store is the constant `legacy`.
_FORCING_TIMESERIES_DELETE_TEMPLATES = ForcingTemplatePair(
    legacy=f"DELETE FROM {FORCING_TABLE_TOKEN} WHERE forcing_version_id = ANY(%s)",
    # `AS fst` is not decoration: it makes the fact-table reference carry the same
    # alias the other eight narrow variants use, which is what the shape oracle
    # scans for. An unaliased `DELETE FROM … WHERE forcing_version_key = …` reads
    # identically to Postgres and is invisible to that oracle.
    narrow=(
        f"DELETE FROM {FORCING_TABLE_TOKEN} AS fst WHERE fst.forcing_version_key IN ("
        "SELECT forcing_version_key FROM met.forcing_version WHERE forcing_version_id = ANY(%s))"
    ),
)

RUN_ROOT = Path(os.getenv("QHH_RUN_ROOT", ROOT / ".nhms-runs" / "qhh-smoke")).resolve()
MODEL_ID = os.getenv("QHH_MODEL_ID", "basins_qhh_shud")
BASIN_ID = os.getenv("QHH_BASIN_ID", "basins_qhh")
BASIN_VERSION_ID = os.getenv("QHH_BASIN_VERSION_ID", "basins_qhh_vbasins")
RIVER_NETWORK_VERSION_ID = os.getenv("QHH_RIVER_NETWORK_VERSION_ID", "basins_qhh_rivnet_vbasins")
MESH_VERSION_ID = os.getenv("QHH_MESH_VERSION_ID", "basins_qhh_mesh_vbasins")
SOURCE_ID = os.getenv("QHH_SOURCE_ID", "gfs")
STATION_ID = os.getenv("QHH_SMOKE_STATION_ID", "qhh_smoke_forcing_proxy")


def main() -> int:
    database_url = os.environ["DATABASE_URL"]
    with psycopg2.connect(database_url) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        ids = _load_ids(cur)
        deleted: dict[str, int] = {}

        run_ids = _list_values(
            cur,
            "SELECT run_id FROM hydro.hydro_run WHERE model_id = %s OR run_id LIKE %s",
            (MODEL_ID, "qhh_%_smoke"),
            "run_id",
        )
        forcing_ids = _list_values(
            cur,
            """
            SELECT forcing_version_id
            FROM met.forcing_version
            WHERE model_id = %s OR forcing_version_id LIKE %s
            """,
            (MODEL_ID, f"forc_{SOURCE_ID}_%_{MODEL_ID}"),
            "forcing_version_id",
        )

        # #1442: located by surrogate key. Sound in this order only because the
        # hydro_run rows this resolves against are deleted further down, after
        # every child table.
        _delete(
            cur,
            deleted,
            "hydro.river_timeseries",
            "run_key IN (SELECT run_key FROM hydro.hydro_run WHERE run_id = ANY(%s))",
            (run_ids,),
        )
        _delete(cur, deleted, "hydro.state_snapshot", "model_id = %s OR run_id = ANY(%s)", (MODEL_ID, run_ids))
        _delete(cur, deleted, "ops.qc_result", _qc_where(), (MODEL_ID, run_ids, forcing_ids, "qhh_%_smoke"))
        _delete(cur, deleted, "ops.pipeline_job", "run_id = ANY(%s)", (run_ids,))
        _delete(
            cur,
            deleted,
            "ops.pipeline_event",
            """
            (entity_type IN ('hydro_run', 'run') AND entity_id = ANY(%s))
            OR (entity_type IN ('model', 'model_instance') AND entity_id = %s)
            OR entity_id = ANY(%s)
            """,
            (run_ids, MODEL_ID, forcing_ids),
        )
        _delete(cur, deleted, "hydro.hydro_run", "run_id = ANY(%s)", (run_ids,))

        _delete_rendered(
            cur,
            deleted,
            FORCING_TABLE_LEGACY,
            render_forcing_ts_sql(
                _FORCING_TIMESERIES_DELETE_TEMPLATES,
                "legacy",
                entry="reset_qhh_smoke_db.forcing_timeseries_delete",
            ).sql,
            (forcing_ids,),
        )
        _delete(cur, deleted, "met.forcing_version_component", "forcing_version_id = ANY(%s)", (forcing_ids,))
        _delete(cur, deleted, "met.forcing_version", "forcing_version_id = ANY(%s)", (forcing_ids,))
        _delete(
            cur,
            deleted,
            "met.interp_weight",
            "model_id = %s OR station_id = %s OR station_id LIKE %s",
            (MODEL_ID, STATION_ID, "qhh_forc_%"),
        )
        _delete(
            cur,
            deleted,
            "met.met_station",
            """
            station_id = %s
            OR station_id LIKE %s
            OR (basin_version_id = %s AND properties_json->>'seed' IN (
                'qhh_backend_smoke',
                'qhh_standard_forcing'
            ))
            """,
            (STATION_ID, "qhh_forc_%", ids["basin_version_id"]),
        )

        _delete(cur, deleted, "core.model_instance", "model_id = %s", (MODEL_ID,))
        _delete(
            cur,
            deleted,
            "core.river_segment_crosswalk",
            "river_network_version_id = %s",
            (ids["river_network_version_id"],),
        )
        _delete(
            cur,
            deleted,
            "core.river_segment",
            "river_network_version_id = %s",
            (ids["river_network_version_id"],),
        )
        _delete(cur, deleted, "core.mesh_version", "mesh_version_id = %s", (ids["mesh_version_id"],))
        _delete(
            cur,
            deleted,
            "core.river_network_version",
            "river_network_version_id = %s",
            (ids["river_network_version_id"],),
        )
        _delete(cur, deleted, "core.basin_version", "basin_version_id = %s", (ids["basin_version_id"],))
        _delete(cur, deleted, "core.basin", "basin_id = %s", (ids["basin_id"],))

    payload = {"status": "reset", "model_id": MODEL_ID, "ids": ids, "deleted": deleted}
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    (RUN_ROOT / "reset-qhh-smoke-db.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _load_ids(cur: Any) -> dict[str, str]:
    cur.execute(
        """
        SELECT basin_version_id, river_network_version_id, mesh_version_id
        FROM core.model_instance
        WHERE model_id = %s
        """,
        (MODEL_ID,),
    )
    model = cur.fetchone()
    if model:
        basin_version_id = str(model["basin_version_id"])
        cur.execute("SELECT basin_id FROM core.basin_version WHERE basin_version_id = %s", (basin_version_id,))
        basin = cur.fetchone()
        return {
            "basin_id": str(basin["basin_id"]) if basin else BASIN_ID,
            "basin_version_id": basin_version_id,
            "river_network_version_id": str(model["river_network_version_id"]),
            "mesh_version_id": str(model["mesh_version_id"]),
        }
    return {
        "basin_id": BASIN_ID,
        "basin_version_id": BASIN_VERSION_ID,
        "river_network_version_id": RIVER_NETWORK_VERSION_ID,
        "mesh_version_id": MESH_VERSION_ID,
    }


def _list_values(cur: Any, sql: str, params: tuple[Any, ...], column: str) -> list[str]:
    cur.execute(sql, params)
    return [str(row[column]) for row in cur.fetchall()]


def _qc_where() -> str:
    return """
    target_id = %s
    OR run_id = ANY(%s)
    OR target_id = ANY(%s)
    OR target_id LIKE %s
    """


def _delete(cur: Any, deleted: dict[str, int], table: str, where: str, params: tuple[Any, ...]) -> None:
    cur.execute(f"DELETE FROM {table} WHERE {where}", params)
    deleted[table] = cur.rowcount


def _delete_rendered(
    cur: Any,
    deleted: dict[str, int],
    table: str,
    statement: str,
    params: tuple[Any, ...],
) -> None:
    """:func:`_delete` for a statement a renderer already composed.

    :func:`_delete` builds ``DELETE FROM {table} WHERE {where}`` itself, so it
    cannot take renderer output — and the forcing fact table's name must come
    from ``forcing_ts_render``'s constants rather than from this file (the
    discovery-set census counts a literal spelling here as an unregistered read).
    ``table`` survives only as the ``deleted`` payload key, which is therefore
    the *physical* relation the statement touched and follows task 7.3's rename
    for free.

    SO THE RECEIPT KEY CHANGES AT 7.3, AND THAT IS INTENDED. The forcing entry
    reads ``met.forcing_station_timeseries`` today and will read
    ``met.forcing_station_timeseries_legacy`` the moment
    ``forcing_ts_render.FORCING_TABLE_LEGACY`` is flipped in the migration's own
    commit, because a receipt that names the relation the DELETE did not touch is
    worse than no receipt. No consumer pins either spelling — the smoke receipt is
    read by a human — so 7.3 has nothing to migrate here and should not treat the
    changed key as a regression.
    """
    cur.execute(statement, params)
    deleted[table] = cur.rowcount


if __name__ == "__main__":
    raise SystemExit(main())
