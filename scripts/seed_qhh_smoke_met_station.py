from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import psycopg2

MODEL_ID = os.getenv("QHH_MODEL_ID", "basins_qhh_shud")
STATION_ID = os.getenv("QHH_SMOKE_STATION_ID", "qhh_smoke_forcing_proxy")


def main() -> int:
    database_url = os.environ["DATABASE_URL"]
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT mi.basin_version_id,
                   ST_X(ST_PointOnSurface(bv.geom)) AS longitude,
                   ST_Y(ST_PointOnSurface(bv.geom)) AS latitude
            FROM core.model_instance mi
            JOIN core.basin_version bv
              ON bv.basin_version_id = mi.basin_version_id
            WHERE mi.model_id = %s
            """,
            (MODEL_ID,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"Model {MODEL_ID!r} was not imported before station seeding.")
        basin_version_id, longitude, latitude = row
        cur.execute(
            """
            INSERT INTO met.met_station (
                station_id,
                basin_version_id,
                station_name,
                geom,
                elevation_m,
                station_role,
                active_flag,
                properties_json
            )
            VALUES (
                %s,
                %s,
                'QHH smoke forcing proxy',
                ST_SetSRID(ST_MakePoint(%s, %s), 4490),
                0.0,
                'forcing_proxy',
                true,
                %s::jsonb
            )
            ON CONFLICT (station_id) DO UPDATE
            SET basin_version_id = EXCLUDED.basin_version_id,
                station_name = EXCLUDED.station_name,
                geom = EXCLUDED.geom,
                elevation_m = EXCLUDED.elevation_m,
                station_role = EXCLUDED.station_role,
                active_flag = true,
                properties_json = EXCLUDED.properties_json
            """,
            (
                STATION_ID,
                basin_version_id,
                float(longitude),
                float(latitude),
                json.dumps(
                    {
                        "seed": "qhh_backend_smoke",
                        "model_id": MODEL_ID,
                        "source": "basin_point_on_surface",
                        "recorded_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    },
                    sort_keys=True,
                ),
            ),
        )
    print(
        json.dumps(
            {
                "status": "seeded",
                "model_id": MODEL_ID,
                "basin_version_id": basin_version_id,
                "station_id": STATION_ID,
                "longitude": float(longitude),
                "latitude": float(latitude),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
