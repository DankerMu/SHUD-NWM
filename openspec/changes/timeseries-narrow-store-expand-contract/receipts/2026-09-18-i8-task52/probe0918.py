"""#1987 task 5.2: curve EXPLAIN gate on node-27, read-only.

Drives the real PsycopgForecastStore of the checkout on sys.path through a
recording pass-through cursor against a READ ONLY connection, then EXPLAIN
(ANALYZE, BUFFERS) each fact-reading statement five times warm.

Three storage states are compared for two pinned networks:
  narrow_compressed   - a cycle whose window opens on hydro.river_timeseries's
                        only compressed chunk (_hyper_9_126, 2026-08-26..08-27)
  narrow_uncompressed - a recent cycle, entirely on uncompressed narrow chunks
  legacy              - a legacy-routed run, on hydro.river_timeseries_legacy

DSN comes from DATABASE_URL in the environment only. Never argv, never printed.

Usage:  DATABASE_URL=... python probe1987.py <checkout> <outfile>
"""

import hashlib
import json
import os
import sys
from collections.abc import Mapping
from contextlib import contextmanager

CHECKOUT, OUTFILE = sys.argv[1], sys.argv[2]
sys.path.insert(0, CHECKOUT)

import psycopg2  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402

from packages.common.forecast_store import PsycopgForecastStore  # noqa: E402

DSN = os.environ["DATABASE_URL"]
WARM_ROUNDS = 5

FACT_MARKERS = ("hydro.river_timeseries", "hydro.river_timeseries_legacy")

SHJ = dict(
    basin_version_id="basins_shj_nj_vbasins",
    river_network_version_id="basins_shj_nj_rivnet_vbasins",
    segment_id="basins_shj_nj_shud_reach_000001",
)
SMALL = dict(
    basin_version_id="basins_tailanhe_vbasins",
    river_network_version_id="basins_tailanhe_rivnet_vbasins",
    segment_id="basins_tailanhe_shud_reach_000001",
)

# run_id / model_id are resolved live so the probe never carries a stale identity.
CASES = [
    dict(label="shj_nj/narrow_compressed", pin=SHJ, store="narrow",
         cycle="compressed", scenario="GFS"),
    dict(label="shj_nj/narrow_uncompressed", pin=SHJ, store="narrow",
         cycle="newest", scenario="GFS"),
    dict(label="shj_nj/legacy", pin=SHJ, store="legacy",
         cycle="newest", scenario="GFS"),
    dict(label="small_tailanhe/narrow_compressed", pin=SMALL, store="narrow",
         cycle="compressed", scenario="GFS"),
    dict(label="small_tailanhe/narrow_uncompressed", pin=SMALL, store="narrow",
         cycle="newest", scenario="GFS"),
    dict(label="small_tailanhe/legacy", pin=SMALL, store="legacy",
         cycle="newest", scenario="GFS"),
]

RESOLVE_SQL = """
SELECT run_id, run_key, model_id, scenario_id, source_id, cycle_time
FROM hydro.hydro_run
WHERE basin_version_id = %(basin)s
  AND timeseries_store = %(store)s
  AND run_type = 'forecast'
  AND source_id = %(source)s
  {cycle_clause}
ORDER BY cycle_time {order}
LIMIT 1
"""


def digest(rows):
    blob = "\n".join(repr(sorted(r.items())) for r in rows)
    return hashlib.sha256(blob.encode()).hexdigest()[:16], len(rows)


class _RecCursor:
    def __init__(self, cursor, sink):
        self._c = cursor
        self._sink = sink
        self._last = None

    def execute(self, statement, parameters=None):
        if isinstance(parameters, Mapping):
            captured = dict(parameters)
        elif parameters is None:
            captured = {}
        else:
            captured = list(parameters)
        self._last = {"sql": str(statement), "params": captured}
        self._sink.append(self._last)
        self._c.execute(statement, parameters)

    def fetchall(self):
        rows = [dict(r) for r in self._c.fetchall()]
        if self._last is not None:
            self._last["rows"] = rows
        return rows

    def fetchone(self):
        row = self._c.fetchone()
        return dict(row) if row is not None else None

    def __getattr__(self, name):
        return getattr(self._c, name)


class ProbeStore(PsycopgForecastStore):
    def __init__(self, connection, sink):
        super().__init__("probe-1987")
        object.__setattr__(self, "_conn", connection)
        object.__setattr__(self, "_sink", sink)

    @contextmanager
    def _transaction(self):
        cur = self._conn.cursor()
        try:
            yield _RecCursor(cur, self._sink)
        finally:
            cur.close()


def walk(node, out, depth=0):
    """Flatten a plan tree, keeping only the fields the 5.2 gate asks about."""
    entry = {
        "depth": depth,
        "node": node.get("Node Type"),
        "relation": node.get("Relation Name"),
        "index": node.get("Index Name"),
        "index_cond": node.get("Index Cond"),
        "filter": node.get("Filter"),
        "rows_removed_by_filter": node.get("Rows Removed by Filter"),
        "actual_rows": node.get("Actual Rows"),
        "shared_hit": node.get("Shared Hit Blocks"),
        "shared_read": node.get("Shared Read Blocks"),
    }
    out.append({k: v for k, v in entry.items() if v is not None})
    for child in node.get("Plans", []) or []:
        walk(child, out, depth + 1)
    return out


def explain_warm(cursor, sql, params, rounds=WARM_ROUNDS):
    samples = []
    last_nodes = None
    for _ in range(rounds):
        cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, params)
        row = cursor.fetchone()
        blob = row["QUERY PLAN"] if isinstance(row, dict) else row[0]
        if isinstance(blob, str):
            blob = json.loads(blob)
        root = blob[0]["Plan"]
        samples.append(
            {
                "shared_hit": root.get("Shared Hit Blocks"),
                "shared_read": root.get("Shared Read Blocks"),
                "actual_rows": root.get("Actual Rows"),
                "exec_ms": blob[0].get("Execution Time"),
            }
        )
        last_nodes = walk(root, [])
    times = sorted(s["exec_ms"] for s in samples if s["exec_ms"] is not None)
    p95 = times[min(len(times) - 1, int(round(0.95 * (len(times) - 1))))] if times else None
    return {"samples": samples, "p95_exec_ms": p95, "plan_nodes": last_nodes}


def resolve_run(cur, case):
    cycle_clause = ""
    order = "DESC"
    params = {
        "basin": case["pin"]["basin_version_id"],
        "store": case["store"],
        "source": "gfs" if case["scenario"] == "GFS" else "IFS",
    }
    if case["cycle"] == "compressed":
        # Resolve the compressed leg live: the OLDEST narrow forecast cycle that starts
        # inside the oldest currently-compressed narrow chunk. Pinning a literal date
        # (what the 2026-09-17 probe did) breaks as soon as retention moves the window.
        cycle_clause = """AND cycle_time >= (
            SELECT min(range_start) FROM timescaledb_information.chunks
             WHERE hypertable_name = 'river_timeseries' AND is_compressed)"""
        order = "ASC"
    elif case["cycle"] != "newest":
        cycle_clause = "AND cycle_time = %(cycle)s"
        params["cycle"] = case["cycle"]
    cur.execute(RESOLVE_SQL.format(cycle_clause=cycle_clause, order=order), params)
    return cur.fetchone()


def main():
    conn = psycopg2.connect(DSN, cursor_factory=RealDictCursor)
    conn.set_session(readonly=True, autocommit=False)
    report = {"checkout": CHECKOUT, "warm_rounds": WARM_ROUNDS, "cases": {}}
    try:
        meta_cur = conn.cursor()
        for case in CASES:
            label = case["label"]
            run = resolve_run(meta_cur, case)
            conn.rollback()
            if run is None:
                report["cases"][label] = {"error": "NO_RUN_FOUND", "case": case["cycle"]}
                print(f"{label}: NO_RUN_FOUND")
                continue

            sink = []
            store = ProbeStore(conn, sink)
            try:
                store.forecast_series(
                    basin_version_id=case["pin"]["basin_version_id"],
                    segment_id=case["pin"]["segment_id"],
                    river_network_version_id=case["pin"]["river_network_version_id"],
                    variables=["q_down"],
                    scenarios=[case["scenario"]],
                    include_analysis=False,
                    run_types=["forecast"],
                    issue_time=run["cycle_time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    run_id=run["run_id"],
                    model_id=run["model_id"],
                )
                err = None
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
            conn.rollback()

            entry = {
                "error": err,
                "run_id": run["run_id"],
                "run_key": run["run_key"],
                "cycle_time": str(run["cycle_time"]),
                "store": case["store"],
                "network": case["pin"]["river_network_version_id"],
                "segment": case["pin"]["segment_id"],
                "fact_statements": [],
                "total_shared_hit": 0,
            }
            cur = conn.cursor()
            for st in sink:
                if not any(m in st["sql"] for m in FACT_MARKERS):
                    continue
                if not isinstance(st["params"], dict):
                    continue
                ex = explain_warm(cur, st["sql"], st["params"])
                dg, nrows = digest(st.get("rows", []) or [])
                entry["fact_statements"].append(
                    {
                        "sql_head": " ".join(st["sql"].split())[:140],
                        "rows_returned": len(st.get("rows", []) or []),
                        "digest": dg,
                        "digest_rows": nrows,
                        "warm_shared_hit": ex["samples"][-1]["shared_hit"],
                        "p95_exec_ms": ex["p95_exec_ms"],
                        "samples": ex["samples"],
                        "plan_nodes": ex["plan_nodes"],
                    }
                )
                entry["total_shared_hit"] += ex["samples"][-1]["shared_hit"] or 0
            cur.close()
            conn.rollback()
            report["cases"][label] = entry

            n = len(entry["fact_statements"])
            print(
                f"{label}: run={run['run_id'][:40]} fact_stmts={n} "
                f"total_hit={entry['total_shared_hit']} err={err}"
            )
            for s in entry["fact_statements"]:
                decomp = [p for p in s["plan_nodes"] if p.get("node") == "Custom Scan"]
                rels = sorted({p.get("relation") for p in s["plan_nodes"] if p.get("relation")})
                print(
                    f"   hit={s['warm_shared_hit']} p95={s['p95_exec_ms']}ms "
                    f"rows={s['rows_returned']} digest={s['digest']} "
                    f"custom_scans={len(decomp)} rels={rels[:6]}"
                )
        meta_cur.close()
    finally:
        conn.close()
    with open(OUTFILE, "w") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"written {OUTFILE}")


main()
