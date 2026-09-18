"""#1987 task 5.2: identity-existence probe, interior-gap MISS branch, before/after.

"Before" = the legacy store (the measurement #1596's E4 receipt made, against the
retained single-column river_timeseries_valid_time_idx). "After" = the narrow store,
which creates NO single-column valid_time index — that is the index this change makes
disappear, so `timeseries-index-hygiene` requires the miss branch to be measured, not
presumed (specs/timeseries-narrow-store/spec.md:176; design.md:57).

The probe SQL is rendered by the REAL mvt.py functions, not retyped, so the plan is the
plan the route issues. READ ONLY, nhms_display_ro, DSN via env only.

A MISS here is the interior-gap branch the spec names: a :valid_time strictly inside a
run's run_display_coverage window (so the discovery sub-select yields candidates) for
which the fact table holds no row at that instant. Hourly series => :30 past the hour.
"""
import json
import os
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

sys.path.insert(0, "/home/nwm/NWM")
from packages.common.river_ts_render import render_river_ts_sql
from services.tiles.mvt import (
    _hydro_national_identity_source_template as tpl,
)

legacy = render_river_ts_sql(tpl("legacy"), "legacy").sql
narrow = render_river_ts_sql(tpl("narrow"), "narrow").sql
identity_source = f"{legacy}\nUNION ALL\n{narrow}"

PROBE = f"""
SELECT CASE WHEN EXISTS (
    SELECT 1
    FROM (
        SELECT DISTINCT ON (mi.river_network_version_id)
               h.run_key, rnv.river_network_version_key,
               h.run_id, mi.river_network_version_id, h.timeseries_store
        FROM hydro.hydro_run h
        JOIN core.model_instance mi ON mi.basin_version_id = h.basin_version_id
        JOIN core.river_network_version rnv
          ON rnv.river_network_version_id = mi.river_network_version_id
        JOIN hydro.run_display_coverage rdc
          ON rdc.run_id = h.run_id AND rdc.segment_count > 0
         AND rdc.river_valid_time_start <= %(valid_time)s
         AND rdc.river_valid_time_end >= %(valid_time)s
        WHERE h.status IN ('succeeded', 'parsed', 'published')
          AND mi.river_network_version_id IS NOT NULL
          AND mi.active_flag
          AND (CAST(%(source)s AS text) IS NULL OR lower(h.source_id) = %(source)s)
          AND (CAST(%(cycle)s AS timestamptz) IS NULL OR h.cycle_time = %(cycle)s)
        ORDER BY mi.river_network_version_id, h.cycle_time DESC, h.run_id DESC
    ) lr
    CROSS JOIN LATERAL ( {identity_source} LIMIT 1 ) hit
    LIMIT 1
) THEN 1 ELSE 0 END AS source_identity_count
""".replace(":variable", "%(variable)s").replace(":valid_time", "%(valid_time)s")

PICK = """
SELECT h.timeseries_store, h.run_id, h.source_id, h.cycle_time,
       rdc.river_valid_time_start AS vstart, rdc.river_valid_time_end AS vend
FROM hydro.hydro_run h
JOIN hydro.run_display_coverage rdc ON rdc.run_id = h.run_id AND rdc.segment_count > 0
WHERE h.timeseries_store = %(store)s AND h.status IN ('succeeded','parsed','published')
ORDER BY h.cycle_time DESC LIMIT 1
"""

def walk(n, out, d=0):
    out.append({k: v for k, v in {
        "depth": d, "node": n.get("Node Type"), "relation": n.get("Relation Name"),
        "index": n.get("Index Name"), "index_cond": n.get("Index Cond"),
        "filter": n.get("Filter"), "rows_removed_by_filter": n.get("Rows Removed by Filter"),
        "actual_rows": n.get("Actual Rows"), "actual_loops": n.get("Actual Loops"),
        "shared_hit": n.get("Shared Hit Blocks"), "shared_read": n.get("Shared Read Blocks"),
    }.items() if v is not None})
    for c in n.get("Plans", []) or []:
        walk(c, out, d + 1)
    return out

conn = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=RealDictCursor)
conn.set_session(readonly=True, autocommit=False)
cur = conn.cursor()
report = {"probe_sql_sha": __import__("hashlib").sha256(PROBE.encode()).hexdigest()[:16], "cases": {}}

for store in ("legacy", "narrow"):
    cur.execute(PICK, {"store": store})
    run = cur.fetchone()
    conn.rollback()
    if run is None:
        report["cases"][store] = {"error": "NO_RUN"}
        continue
    mid = run["vstart"] + (run["vend"] - run["vstart"]) / 2
    hit_t = mid.replace(minute=0, second=0, microsecond=0)
    miss_t = hit_t.replace(minute=30)          # interior gap: hourly series, no :30 row
    for branch, vt in (("hit", hit_t), ("miss", miss_t)):
        params = {"valid_time": vt, "source": run["source_id"].lower(),
                  "cycle": run["cycle_time"], "variable": "q_down"}
        cur.execute(PROBE, params)
        got = cur.fetchone()["source_identity_count"]
        conn.rollback()
        samples, nodes = [], None
        for _ in range(5):
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + PROBE, params)
            blob = cur.fetchone()["QUERY PLAN"]
            if isinstance(blob, str):
                blob = json.loads(blob)
            root = blob[0]["Plan"]
            samples.append({"exec_ms": blob[0].get("Execution Time"),
                            "shared_hit": root.get("Shared Hit Blocks"),
                            "shared_read": root.get("Shared Read Blocks")})
            nodes = walk(root, [])
            conn.rollback()
        t = sorted(s["exec_ms"] for s in samples)
        key = f"{store}/{branch}"
        report["cases"][key] = {
            "run_id": run["run_id"], "cycle_time": str(run["cycle_time"]),
            "coverage_window": [str(run["vstart"]), str(run["vend"])],
            "valid_time": str(vt), "source_identity_count": got,
            "p95_exec_ms": t[min(len(t) - 1, round(0.95 * (len(t) - 1)))],
            "samples": samples,
            "fact_nodes": [n for n in nodes if (n.get("relation") or "").startswith(
                ("_hyper_3_", "_hyper_9_", "compress_hyper_"))],
        }
        fn = report["cases"][key]["fact_nodes"]
        print(f"{key:14s} vt={vt} answer={got} p95={report['cases'][key]['p95_exec_ms']:.1f}ms "
              f"hit={samples[-1]['shared_hit']} fact_nodes={len(fn)}")
        for n in fn:
            print(f"    {n.get('relation'):28s} idx={(n.get('index') or '-')[:44]:44s} "
                  f"loops={n.get('actual_loops')} rows={n.get('actual_rows')} "
                  f"removed={n.get('rows_removed_by_filter',0)} hit={n.get('shared_hit')}")
conn.close()
json.dump(report, open("/home/nwm/tmp/1987/identity-probe-0918.json", "w"), indent=2, default=str)
print("written identity-probe-0918.json")
