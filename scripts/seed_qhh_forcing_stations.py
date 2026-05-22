from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import Json, execute_values

ROOT = Path(__file__).resolve().parents[1]
BASINS_ROOT = Path(os.getenv("NHMS_BASINS_ROOT", ROOT / "data" / "Basins"))
MODEL_ID = os.getenv("QHH_MODEL_ID", "basins_qhh_shud")
PROJECT_NAME = os.getenv("QHH_PROJECT_NAME", "qhh")
SOURCE_FILE = Path(
    os.getenv(
        "QHH_TSD_FORC_PATH",
        BASINS_ROOT / "qhh" / "input" / PROJECT_NAME / f"{PROJECT_NAME}.tsd.forc",
    )
)


def main() -> int:
    stations = _read_tsd_forc(SOURCE_FILE)
    database_url = os.environ["DATABASE_URL"]
    with psycopg2.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT basin_version_id
            FROM core.model_instance
            WHERE model_id = %s
            """,
            (MODEL_ID,),
        )
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"Model {MODEL_ID!r} was not imported before station seeding.")
        basin_version_id = str(row[0])
        execute_values(
            cur,
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
            VALUES %s
            ON CONFLICT (station_id) DO UPDATE
            SET basin_version_id = EXCLUDED.basin_version_id,
                station_name = EXCLUDED.station_name,
                geom = EXCLUDED.geom,
                elevation_m = EXCLUDED.elevation_m,
                station_role = EXCLUDED.station_role,
                active_flag = true,
                properties_json = EXCLUDED.properties_json
            """,
            [
                (
                    station["station_id"],
                    basin_version_id,
                    station["station_name"],
                    station["longitude"],
                    station["latitude"],
                    station["elevation_m"],
                    station["station_role"],
                    Json(station["properties_json"]),
                )
                for station in stations
            ],
            template=(
                "(%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4490), "
                "%s, %s, true, %s)"
            ),
            page_size=1000,
        )
    print(
        json.dumps(
            {
                "status": "seeded",
                "model_id": MODEL_ID,
                "basin_version_id": basin_version_id,
                "station_count": len(stations),
                "source_file": str(SOURCE_FILE),
                "first_station": stations[0]["station_id"] if stations else None,
                "last_station": stations[-1]["station_id"] if stations else None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _read_tsd_forc(path: Path) -> list[dict[str, Any]]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) < 4:
        raise RuntimeError(f"Invalid SHUD forcing station file: {path}")
    try:
        expected_count = int(lines[0].split()[0])
    except (IndexError, ValueError) as error:
        raise RuntimeError(f"Invalid SHUD forcing station header: {path}") from error
    rows: list[dict[str, Any]] = []
    recorded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    for raw in lines[3:]:
        parts = raw.split()
        if len(parts) < 7:
            continue
        forcing_index = int(float(parts[0]))
        filename = Path(parts[6]).name
        station_id = f"qhh_forc_{forcing_index:03d}"
        z = float(parts[5])
        rows.append(
            {
                "station_id": station_id,
                "station_name": f"QHH forcing station {forcing_index:03d}",
                "longitude": float(parts[1]),
                "latitude": float(parts[2]),
                "elevation_m": 0.0 if z <= -9990 else z,
                "station_role": "forcing_grid",
                "properties_json": {
                    "seed": "qhh_standard_forcing",
                    "model_id": MODEL_ID,
                    "project_name": PROJECT_NAME,
                    "source": "qhh.tsd.forc",
                    "source_file": str(path),
                    "recorded_at": recorded_at,
                    "shud_forcing_index": forcing_index,
                    "forcing_filename": filename,
                    "original_id": parts[0],
                    "x": float(parts[3]),
                    "y": float(parts[4]),
                    "z": z,
                },
            }
        )
    if len(rows) != expected_count:
        raise RuntimeError(f"Expected {expected_count} qhh forcing stations, parsed {len(rows)} from {path}.")
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
