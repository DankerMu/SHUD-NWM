from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg2
from psycopg2.extras import Json, execute_values

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = Path(os.getenv("QHH_RUN_ROOT", ROOT / ".nhms-runs" / "qhh-smoke")).resolve()
MODEL_ID = os.getenv("QHH_MODEL_ID", "basins_qhh_shud")
PACKAGE_VERSION = os.getenv("QHH_PACKAGE_VERSION", "v0.0.1-qhh-smoke")
PROJECT_NAME = os.getenv("QHH_PROJECT_NAME", "qhh")


def main() -> int:
    database_url = os.environ["DATABASE_URL"]
    riv_path = RUN_ROOT / "models" / MODEL_ID / PACKAGE_VERSION / "package" / f"{PROJECT_NAME}.sp.riv"
    segment_count = int(riv_path.read_text(encoding="utf-8").split()[0])
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT mi.river_network_version_id
            FROM core.model_instance mi
            WHERE mi.model_id = %s
            """,
            (MODEL_ID,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"Model {MODEL_ID!r} was not imported before SHUD segment seeding.")
        river_network_version_id = row[0]
        cur.execute(
            """
            SELECT COALESCE(MAX(segment_order), 0)
            FROM core.river_segment
            WHERE river_network_version_id = %s
              AND COALESCE(properties_json->>'shud_output_river', 'false') <> 'true'
            """,
            (river_network_version_id,),
        )
        order_offset = int(cur.fetchone()[0] or 0)
        rows = [
            (
                f"{MODEL_ID}_shud_riv_{index:06d}",
                river_network_version_id,
                order_offset + index,
                Json(
                    {
                        "shud_output_river": True,
                        "shud_riv_index": index,
                        "source": f"{PROJECT_NAME}.sp.riv",
                        "geometry_source": "gis_rivseg_iRiv",
                    }
                ),
            )
            for index in range(1, segment_count + 1)
        ]
        execute_values(
            cur,
            """
            INSERT INTO core.river_segment (
                river_segment_id,
                river_network_version_id,
                segment_order,
                properties_json
            )
            VALUES %s
            ON CONFLICT (river_segment_id, river_network_version_id) DO UPDATE
            SET segment_order = EXCLUDED.segment_order,
                properties_json = EXCLUDED.properties_json
            """,
            rows,
            template="(%s, %s, %s, %s)",
            page_size=1000,
        )
        cur.execute(
            """
            WITH gis_points AS (
                SELECT
                    (properties_json->>'source_raw_segment_id')::int AS shud_riv_index,
                    segment_order,
                    length_m,
                    (dump).path[1] AS point_order,
                    (dump).geom AS point_geom
                FROM (
                    SELECT properties_json, segment_order, length_m, ST_DumpPoints(geom) AS dump
                    FROM core.river_segment
                    WHERE river_network_version_id = %s
                      AND geom IS NOT NULL
                      AND COALESCE(properties_json->>'shud_output_river', 'false') <> 'true'
                      AND properties_json ? 'source_raw_segment_id'
                      AND (properties_json->>'source_raw_segment_id') ~ '^[0-9]+$'
                ) source
            ),
            numbered_points AS (
                SELECT
                    shud_riv_index,
                    segment_order,
                    length_m,
                    point_order,
                    point_geom,
                    LAG(ST_AsEWKB(point_geom)) OVER (
                        PARTITION BY shud_riv_index
                        ORDER BY segment_order, point_order
                    ) AS previous_point
                FROM gis_points
            ),
            deduped_points AS (
                SELECT *
                FROM numbered_points
                WHERE previous_point IS NULL
                   OR previous_point <> ST_AsEWKB(point_geom)
            ),
            gis_by_riv AS (
                SELECT
                    shud_riv_index,
                    ST_MakeLine(point_geom ORDER BY segment_order, point_order)::geometry(LineString, 4490) AS geom,
                    SUM(DISTINCT length_m) AS length_m,
                    COUNT(DISTINCT segment_order) AS source_segment_count
                FROM deduped_points
                GROUP BY shud_riv_index
                HAVING COUNT(*) >= 2
            ),
            updated AS (
                UPDATE core.river_segment target
                SET geom = gis.geom,
                    length_m = gis.length_m,
                    properties_json = target.properties_json
                        || jsonb_build_object(
                            'geometry_source', 'gis_rivseg_iRiv',
                            'geometry_source_segment_count', gis.source_segment_count,
                            'geometry_source_length_m', gis.length_m
                        )
                FROM gis_by_riv gis
                WHERE target.river_network_version_id = %s
                  AND COALESCE(target.properties_json->>'shud_output_river', 'false') = 'true'
                  AND (target.properties_json->>'shud_riv_index')::int = gis.shud_riv_index
                RETURNING 1
            )
            SELECT COUNT(*) AS updated_rows
            FROM updated
            """,
            (river_network_version_id, river_network_version_id),
        )
        geometry_rows = int(cur.fetchone()[0] or 0)
        cur.execute(
            """
            SELECT COUNT(*)
            FROM core.river_segment
            WHERE river_network_version_id = %s
              AND COALESCE(properties_json->>'shud_output_river', 'false') = 'true'
              AND geom IS NOT NULL
            """,
            (river_network_version_id,),
        )
        shud_segments_with_geom = int(cur.fetchone()[0] or 0)
    print(
        json.dumps(
            {
                "status": "seeded",
                "model_id": MODEL_ID,
                "river_network_version_id": river_network_version_id,
                "segment_count": segment_count,
                "geometry_rows": geometry_rows,
                "shud_segments_with_geom": shud_segments_with_geom,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
