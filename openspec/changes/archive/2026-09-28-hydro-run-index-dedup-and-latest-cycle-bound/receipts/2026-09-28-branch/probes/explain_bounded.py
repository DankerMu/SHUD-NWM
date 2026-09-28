"""#2630 tasks 3.3: per-chunk EXPLAIN (ANALYZE, BUFFERS) of the branch's bounded D1 statement.

    WT=<branch checkout> DSNFILE=<read-only DSN file> PIN_BV=... PIN_REACH=... PIN_NET=... \\
        /home/nwm/NWM/.venv/bin/python -B explain_bounded.py

Captures the statement the branch's real store executes for one pin (issue_time=latest,
GFS+IFS), then EXPLAINs it three times (warm) and prints the third plan in text form, so
every chunk's index choice and buffers are visible. Read-only (nhms_display_ro).
"""

from __future__ import annotations

import json
import os

from _probe_common import RecordingCursor, RecordingStore, begin, connect

from packages.common import forecast_store

pin = {"bv": os.environ["PIN_BV"], "reach": os.environ["PIN_REACH"], "net": os.environ["PIN_NET"]}
identity = {
    "basin_version_id": pin["bv"],
    "segment_id": forecast_store._timeseries_segment_id(pin["reach"]),
    "river_network_version_id": pin["net"],
    "scenario_filter": forecast_store._scenario_filter(["GFS", "IFS"]),
    "identity_filter": forecast_store._run_identity_filter(run_id=None, model_id=None),
}
connection = connect(isolation="REPEATABLE READ")
begin(connection, "120s")
sink: list[dict] = []
with connection.cursor() as raw:
    result = RecordingStore(connection, sink)._per_source_latest_cycles(RecordingCursor(raw, sink), **identity)
statement = sink[-1]
print(
    json.dumps(
        {"pin": pin, "segment_id": identity["segment_id"], "result": {k: v.isoformat() for k, v in result.items()}}
    )
)
print(statement["sql"])
print(json.dumps(statement["params"], default=str))
with connection.cursor() as cursor:
    for attempt in range(3):
        cursor.execute("EXPLAIN (ANALYZE, BUFFERS) " + statement["sql"], statement["params"])
        plan = "\n".join(row["QUERY PLAN"] for row in cursor.fetchall())
print(plan)
connection.rollback()
connection.close()
