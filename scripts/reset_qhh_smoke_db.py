from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[1]
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

        _delete(cur, deleted, "hydro.river_timeseries", "run_id = ANY(%s)", (run_ids,))
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

        _delete(cur, deleted, "met.forcing_station_timeseries", "forcing_version_id = ANY(%s)", (forcing_ids,))
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


if __name__ == "__main__":
    raise SystemExit(main())
