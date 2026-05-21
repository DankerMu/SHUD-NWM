from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = Path(os.getenv("QHH_RUN_ROOT", ROOT / ".nhms-runs" / "qhh-smoke")).resolve()


def main() -> int:
    run_id = os.environ["QHH_RUN_ID"]
    database_url = os.environ["DATABASE_URL"]
    with psycopg2.connect(database_url) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            SELECT run_id, status, model_id, basin_version_id, forcing_version_id,
                   source_id, cycle_time, start_time, end_time, output_uri, log_uri
            FROM hydro.hydro_run
            WHERE run_id = %s
            """,
            (run_id,),
        )
        run = dict(cur.fetchone() or {})
        cur.execute(
            """
            SELECT count(*) AS rows,
                   count(DISTINCT river_segment_id) AS segment_count,
                   min(valid_time) AS first_valid_time,
                   max(valid_time) AS last_valid_time,
                   min(value) AS min_m3s,
                   max(value) AS max_m3s,
                   avg(value) AS avg_m3s
            FROM hydro.river_timeseries
            WHERE run_id = %s
            """,
            (run_id,),
        )
        timeseries = dict(cur.fetchone() or {})
        cur.execute(
            """
            SELECT passed, severity, message, checks_json
            FROM ops.qc_result
            WHERE run_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id,),
        )
        qc = dict(cur.fetchone() or {})
    payload = {"run": _jsonable(run), "river_timeseries": _jsonable(timeseries), "qc": _jsonable(qc)}
    output_path = RUN_ROOT / "qhh-result-summary.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": "summarized", "run_id": run_id, "summary_path": str(output_path)}, sort_keys=True))
    return 0


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


if __name__ == "__main__":
    raise SystemExit(main())
