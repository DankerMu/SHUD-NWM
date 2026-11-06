"""#1987 5.2: node-27 LOCAL single-source forecast-series warm P95, run-bound shape.

The spec bound is 500 ms. The shape is the one §6 of the 2026-09-17 receipt pinned by
user decision: run-bound (run_id + model_id + explicit issue_time), not the
issue_time=latest production default — that one is red for an unrelated reason (#2424)
and is recorded separately, never folded into this verdict.

Reads the six cases the EXPLAIN probe resolved, so the API and SQL legs measure the
same runs. DB access is READ ONLY and only to look up model_id; DSN via env only.

N = 30, not the spec minimum of 5. At n = 8 the P95 index lands on the maximum, so a
single scheduler blip on a node that also runs an autopipeline tick every ten minutes
reads as the P95 -- one 8-sample pass of this script returned 699 ms for a cell whose
median was 214 ms. 30 samples puts P95 at the 29th value and makes the number a
percentile again. All samples are kept in the JSON so the distribution is re-readable.
"""
import json
import os
import statistics
import time
import urllib.request

import psycopg2
from psycopg2.extras import RealDictCursor

CASES = json.load(open("/home/nwm/tmp/1987/explain-0918.json"))["cases"]
BASE = "http://127.0.0.1:8080/api/v1/basin-versions"
WARM, N = 2, 30

conn = psycopg2.connect(os.environ["DATABASE_URL"], cursor_factory=RealDictCursor)
conn.set_session(readonly=True, autocommit=False)
cur = conn.cursor()

out = {}
for label, c in CASES.items():
    cur.execute("SELECT model_id, basin_version_id FROM hydro.hydro_run WHERE run_id = %s",
                (c["run_id"],))
    r = cur.fetchone()
    conn.rollback()
    issue = c["cycle_time"].replace(" ", "T").replace("+00:00", "Z")
    url = (f"{BASE}/{r['basin_version_id']}/river-segments/{c['segment']}/forecast-series"
           f"?river_network_version_id={c['network']}&issue_time={issue}"
           f"&variables=q_down&scenarios=GFS&include_analysis=false&run_types=forecast"
           f"&run_id={c['run_id']}&model_id={r['model_id']}")
    code = body = None
    for _ in range(WARM):
        with urllib.request.urlopen(url, timeout=60) as resp:
            code, body = resp.status, resp.read()
    ms = []
    for _ in range(N):
        t0 = time.perf_counter()
        with urllib.request.urlopen(url, timeout=60) as resp:
            resp.read()
        ms.append((time.perf_counter() - t0) * 1000)
    ms.sort()
    p95 = ms[min(len(ms) - 1, round(0.95 * (len(ms) - 1)))]
    payload = json.loads(body)
    pts = sum(len(s.get("points") or []) for s in (payload.get("series") or []))
    out[label] = {"http": code, "bytes": len(body), "series": len(payload.get("series") or []),
                  "points": pts, "n": N, "min_ms": round(ms[0], 1),
                  "median_ms": round(statistics.median(ms), 1), "p95_ms": round(p95, 1),
                  "max_ms": round(ms[-1], 1), "pass_500ms": p95 <= 500,
                  "samples_ms": [round(v, 1) for v in ms]}
    print(f"{'PASS' if p95 <= 500 else 'FAIL'} {label:34s} http={code} bytes={len(body)} "
          f"series={out[label]['series']} points={pts} "
          f"min={ms[0]:.1f} med={statistics.median(ms):.1f} p95={p95:.1f} max={ms[-1]:.1f} ms")
conn.close()
json.dump(out, open("/home/nwm/tmp/1987/api-0918.json", "w"), indent=2)
print("\nAPI GATE:", "GREEN" if all(v["pass_500ms"] for v in out.values()) else "RED")
