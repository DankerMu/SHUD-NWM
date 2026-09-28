"""#2626 D2 (tasks 1.2): candidate index shapes for the D1 `cand` CTE, on a node-27 scratch DB.

The scratch database (prefix nhms_scratch_batchdb_) is built from db/migrations through
000064 and holds a read-only copy of production hydro.hydro_run. Each candidate is built
alone (every other candidate dropped), then the `cand` SELECT of
PsycopgForecastStore._per_source_latest_cycles is EXPLAIN (ANALYZE, BUFFERS)ed for each pin,
three warm runs, last one recorded. Two visibility-map states per candidate:
  * "analyze-only": no VACUUM after the COPY, so the VM is (nearly) empty — production's
    hydro_run has relallvisible 47 of relpages 1299, i.e. index-only scans there fetch heap;
  * "vacuumed": VACUUM ANALYZE, every page all-visible (the best case for INCLUDE).
Environment: SCRATCH_URL (must name a nhms_scratch_batchdb_* database), WT (checkout).
"""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urlsplit

WT = os.environ["WT"]
sys.path.insert(0, WT)
import psycopg2  # noqa: E402

from packages.common import forecast_store  # noqa: E402

URL = os.environ["SCRATCH_URL"]
assert urlsplit(URL).path.lstrip("/").startswith("nhms_scratch_batchdb_"), "refusing a non-scratch database"

PRED = "WHERE run_type = 'forecast' AND cycle_time IS NOT NULL"
CANDIDATES = {
    "none (seq scan)": None,
    "A (basin_version_id, cycle_time DESC)": "(basin_version_id, cycle_time DESC)",
    "B (basin_version_id, cycle_time DESC) INCLUDE (run_key, scenario_id, source_id, end_time)": (
        "(basin_version_id, cycle_time DESC) INCLUDE (run_key, scenario_id, source_id, end_time)"
    ),
    "C (basin_version_id, scenario_id, cycle_time DESC)": "(basin_version_id, scenario_id, cycle_time DESC)",
    "D (basin_version_id)": "(basin_version_id)",
}
PINS = (
    ("basins_zhaochen_bst_vbasins", ["GFS", "IFS"], None),
    ("basins_byh_vbasins", ["GFS", "IFS"], None),
    ("basins_byh_vbasins", ["IFS"], None),
    ("basins_byh_vbasins", ["GFS", "IFS"], "__latest_model__"),
)


def cand_sql(scenarios, model_id):
    sf = forecast_store._scenario_filter(scenarios)
    idf = forecast_store._run_identity_filter(run_id=None, model_id=model_id)
    sql = f"""
    SELECT h.run_key, h.scenario_id, h.cycle_time, h.end_time
    FROM hydro.hydro_run h
    WHERE h.run_type = 'forecast'
      AND h.cycle_time IS NOT NULL
      AND h.basin_version_id = %(basin_version_id)s
      {sf.sql}
      {idf.sql}
    """
    return sql, {**sf.params, **idf.params}


def top(plan_text: str) -> dict:
    first_buffers = next(line for line in plan_text.splitlines() if "Buffers:" in line)
    hit = int(re.search(r"shared hit=(\d+)", first_buffers).group(1))
    read = re.search(r"read=(\d+)", first_buffers)
    scan = next(
        (line.strip() for line in plan_text.splitlines() if re.search(r"Scan", line)), plan_text.splitlines()[0]
    )
    heap = re.search(r"Heap Fetches: (\d+)", plan_text)
    rows = re.search(r"actual time=[\d.]+\.\.[\d.]+ rows=(\d+)", plan_text)
    return {
        "shared_hit": hit,
        "shared_read": int(read.group(1)) if read else 0,
        "scan": scan,
        "heap_fetches": int(heap.group(1)) if heap else None,
        "rows": int(rows.group(1)) if rows else None,
    }


def main() -> None:
    conn = psycopg2.connect(URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("SELECT current_database()")
    print(json.dumps({"database": cur.fetchone()[0]}))
    cur.execute(
        "SELECT model_id FROM hydro.hydro_run WHERE basin_version_id = 'basins_byh_vbasins' "
        "AND run_type = 'forecast' AND cycle_time IS NOT NULL ORDER BY cycle_time DESC LIMIT 1"
    )
    latest_model = cur.fetchone()[0]
    results = []
    for state in ("analyze-only", "vacuumed"):
        if state == "vacuumed":
            cur.execute("VACUUM ANALYZE hydro.hydro_run")
        for label, definition in CANDIDATES.items():
            for i in range(len(CANDIDATES)):
                cur.execute(f"DROP INDEX IF EXISTS hydro.d2_candidate_{i}")
            if definition is not None:
                cur.execute(f"CREATE INDEX d2_candidate_0 ON hydro.hydro_run {definition} {PRED}")
                cur.execute("ANALYZE hydro.hydro_run")
            cur.execute("SELECT relpages, relallvisible FROM pg_class WHERE oid = 'hydro.hydro_run'::regclass")
            relpages, relallvisible = cur.fetchone()
            for basin, scenarios, model in PINS:
                model_id = latest_model if model == "__latest_model__" else None
                sql, params = cand_sql(scenarios, model_id)
                params = {**params, "basin_version_id": basin}
                for _ in range(3):
                    cur.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + sql, params)
                    plan = "\n".join(row[0] for row in cur.fetchall())
                result = {
                    "state": state,
                    "candidate": label,
                    "relpages": relpages,
                    "relallvisible": relallvisible,
                    "pin": {"basin": basin, "scenarios": scenarios, "model_filter": model_id},
                    **top(plan),
                }
                results.append(result)
                print(json.dumps(result))
                print(plan)
    for i in range(len(CANDIDATES)):
        cur.execute(f"DROP INDEX IF EXISTS hydro.d2_candidate_{i}")
    print(
        "SUMMARY state | candidate | "
        + " | ".join(f"{b.split('_')[1]}:{'+'.join(s)}{'+model' if m else ''}" for b, s, m in PINS)
    )
    for state in ("analyze-only", "vacuumed"):
        for label in CANDIDATES:
            hits = [r["shared_hit"] for r in results if r["state"] == state and r["candidate"] == label]
            print(f"SUMMARY {state} | {label} | " + " | ".join(str(h) for h in hits))
    conn.close()


if __name__ == "__main__":
    main()
