from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from packages.common.source_identity import normalize_source_id

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = Path(os.getenv("QHH_RUN_ROOT", ROOT / ".nhms-runs" / "qhh-continuous")).resolve()
MODEL_ID = os.getenv("QHH_MODEL_ID", "basins_qhh_shud")
PACKAGE_VERSION = os.getenv("QHH_PACKAGE_VERSION", "v0.0.1-qhh-smoke-lake2")
SOURCE_ID = normalize_source_id(os.getenv("QHH_SOURCE_ID", "gfs"))
PROJECT_NAME = os.getenv("QHH_PROJECT_NAME", "qhh")
OUTPUT_INTERVAL_MINUTES = int(os.getenv("QHH_MODEL_OUTPUT_INTERVAL", "180"))
THREADS = int(os.getenv("QHH_SHUD_THREADS", "1"))


def main() -> int:
    cycle_token = os.environ["QHH_CYCLE_TIME"]
    cycle_time = datetime.strptime(cycle_token, "%Y%m%d%H").replace(tzinfo=UTC)
    source_segment = SOURCE_ID.lower()
    forcing_version_id = f"forc_{source_segment}_{cycle_token}_{MODEL_ID}"
    run_id = os.getenv("QHH_RUN_ID", f"fcst_{source_segment}_{cycle_token}_{MODEL_ID}")
    database_url = os.environ["DATABASE_URL"]

    with psycopg2.connect(database_url) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT model_id, basin_version_id, river_network_version_id, mesh_version_id
            FROM core.model_instance
            WHERE model_id = %s
            """,
            (MODEL_ID,),
        )
        model = _one(cur.fetchone(), f"model_instance not found: {MODEL_ID}")
        cur.execute(
            """
            SELECT forcing_version_id, model_id, source_id, cycle_time, start_time, end_time, forcing_package_uri
            FROM met.forcing_version
            WHERE forcing_version_id = %s
            """,
            (forcing_version_id,),
        )
        forcing = _one(cur.fetchone(), f"forcing_version not found: {forcing_version_id}")
        cur.execute(
            """
            SELECT count(*) AS station_count
            FROM met.met_station
            WHERE basin_version_id = %s
              AND station_role = 'forcing_grid'
              AND active_flag
            """,
            (model["basin_version_id"],),
        )
        station_count = int((cur.fetchone() or {}).get("station_count") or 0)

    segment_count = _first_int(RUN_ROOT / "models" / MODEL_ID / PACKAGE_VERSION / "package" / f"{PROJECT_NAME}.sp.riv")
    start_time = _format_time(forcing["start_time"])
    end_time = _format_time(forcing["end_time"])
    manifest = {
        "run_id": run_id,
        "run_type": "forecast",
        "scenario_id": _scenario_for_source(SOURCE_ID),
        "source_id": SOURCE_ID,
        "cycle_time": _format_time(cycle_time),
        "start_time": start_time,
        "end_time": end_time,
        "model": {
            "model_id": MODEL_ID,
            "basin_version_id": model["basin_version_id"],
            "river_network_version_id": model["river_network_version_id"],
            "mesh_version_id": model["mesh_version_id"],
            "model_package_uri": f"s3://nhms/models/{MODEL_ID}/{PACKAGE_VERSION}/package/",
            "project_name": PROJECT_NAME,
            "segment_count": segment_count,
            "segment_source": "shud_sp_riv",
        },
        "initial_state": {
            "state_id": "qhh_packaged_calibrated_state",
            "ic_file_uri": None,
            "valid_time": start_time,
            "checksum": None,
            "quality": "packaged_calibrated_state",
        },
        "forcing": {
            "forcing_version_id": forcing["forcing_version_id"],
            "forcing_uri": forcing["forcing_package_uri"],
            "station_count": station_count,
            "station_source": "qhh.tsd.forc",
            "shud_forcing_layout": "standard_multi_station",
        },
        "runtime": {
            "command_style": "shud_project",
            "output_interval_minutes": OUTPUT_INTERVAL_MINUTES,
            "init_mode": 3,
            "threads": THREADS,
        },
        "outputs": {
            "output_uri": f"s3://nhms/runs/{run_id}/output/",
            "log_uri": f"s3://nhms/runs/{run_id}/logs/",
            "run_manifest_uri": f"s3://nhms/runs/{run_id}/input/manifest.json",
        },
    }

    manifest_path = RUN_ROOT / "runs" / run_id / "input" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
    manifest_path.write_text(manifest_json + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"status": "manifest_ready", "run_id": run_id, "manifest_path": str(manifest_path)},
            sort_keys=True,
        )
    )
    return 0


def _one(row: Any, message: str) -> dict[str, Any]:
    if row is None:
        raise RuntimeError(message)
    return dict(row)


def _format_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _first_int(path: Path) -> int:
    return int(path.read_text(encoding="utf-8").split()[0])


def _scenario_for_source(source_id: str) -> str:
    if source_id == "gfs":
        return "forecast_gfs_deterministic"
    if source_id == "IFS":
        return "forecast_ifs_deterministic"
    return f"forecast_{source_id.lower()}_deterministic"


if __name__ == "__main__":
    raise SystemExit(main())
