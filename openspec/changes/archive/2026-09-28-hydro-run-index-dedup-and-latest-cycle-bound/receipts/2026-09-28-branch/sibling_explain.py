"""#2634 tasks 3.4 (pre-apply): the hydro_run sibling selectors still use an index after 000065.

Runs on a node-27 scratch database (nhms_scratch_batchdb_*) holding read-only copies of
production hydro.hydro_run, core.basin, core.basin_version, core.river_network_version,
core.model_instance, met.forcing_version and hydro.run_display_coverage. The SQL is taken
from the code, not retyped: display_ready_run (services/tiles/mvt.py) through a capturing
session, the QHH latest-product candidate CTE (forecast_store._qhh_latest_candidate_runs_sql
and the fast path's positional twin through a capturing cursor), the display-coverage
candidate CTE (display_coverage._CANDIDATE_RUNS_SQL) and _eligible_run_ids, and the
autopipeline publish UPDATE (plain EXPLAIN inside a rolled-back transaction).
Environment: SCRATCH_URL, WT.
"""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0, os.environ["WT"])
import psycopg2  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402

from packages.common import display_coverage, forecast_store  # noqa: E402
from services.tiles import mvt  # noqa: E402

URL = os.environ["SCRATCH_URL"]
assert urlsplit(URL).path.lstrip("/").startswith("nhms_scratch_batchdb_"), "refusing a non-scratch database"


class _Capture(Exception):
    pass


class _CaptureSession:
    def execute(self, clause, params=None):
        self.sql = str(clause)
        raise _Capture


def _display_ready_run_sql() -> str:
    session = _CaptureSession()
    try:
        mvt.display_ready_run(session)
    except _Capture:
        return session.sql
    raise AssertionError("display_ready_run did not execute")


class _CaptureCursor:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=None):
        self.sink.append((sql, params))
        raise _Capture

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _CaptureConnection:
    def __init__(self):
        self.sink = []

    def cursor(self, *args, **kwargs):
        return _CaptureCursor(self.sink)


def _eligible_run_ids_sql() -> str:
    connection = _CaptureConnection()
    try:
        display_coverage._eligible_run_ids(connection)
    except _Capture:
        return connection.sink[0][0]
    raise AssertionError("_eligible_run_ids did not execute")


def _fast_path_statement(basin_id: str, source_id: str):
    store = forecast_store.PsycopgForecastStore("postgresql://capture")
    sink = []
    try:
        store._fetch_latest_qhh_display_candidates_fast(
            _CaptureCursor(sink),
            basin_id=basin_id,
            source_id=source_id,
            identity_sql="",
            identity_params=(),
            candidate_limit=1,
        )
    except _Capture:
        return sink[0]
    except TypeError as error:  # signature drift: say so rather than guess
        raise AssertionError(f"fast path signature changed: {error}")
    raise AssertionError("fast path did not execute")


def main() -> None:
    conn = psycopg2.connect(URL, cursor_factory=RealDictCursor)
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("SELECT current_database() AS db, (SELECT max(version) FROM public.schema_migrations) AS ledger")
    print(json.dumps(dict(cur.fetchone())))
    cur.execute("SELECT indexname FROM pg_indexes WHERE schemaname = 'hydro' AND tablename = 'hydro_run' ORDER BY 1")
    print(json.dumps({"hydro_run_indexes": [r["indexname"] for r in cur.fetchall()]}))
    cur.execute(
        "SELECT bv.basin_id, h.source_id FROM hydro.hydro_run h JOIN core.basin_version bv USING (basin_version_id) "
        "WHERE h.status = 'published' ORDER BY h.cycle_time DESC LIMIT 1"
    )
    pin = cur.fetchone()
    print(json.dumps({"pin": dict(pin)}))
    horizon = 168
    statements = {
        "display_ready_run (services/tiles/mvt.py)": (_display_ready_run_sql(), None),
        "qhh_latest candidate_runs (forecast_store._qhh_latest_candidate_runs_sql)": (
            forecast_store._qhh_latest_candidate_runs_sql(identity_sql="", pin_scan_run_id=False),
            {"basin_id": pin["basin_id"], "source_id": pin["source_id"], "horizon": horizon, "candidate_limit": 1},
        ),
        "display_coverage candidate_runs (display_coverage._CANDIDATE_RUNS_SQL)": (
            display_coverage._CANDIDATE_RUNS_SQL,
            {"basin_id": pin["basin_id"], "run_id": None, "horizon": horizon},
        ),
        "display_coverage _eligible_run_ids": (_eligible_run_ids_sql(), None),
    }
    try:
        statements["qhh_latest fast path (positional twin)"] = _fast_path_statement(pin["basin_id"], pin["source_id"])
    except AssertionError as error:
        print(json.dumps({"fast_path": str(error)}))
    for label, (sql, params) in statements.items():
        for _ in range(2):
            cur.execute("EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) " + sql, params)
            plan = "\n".join(r["QUERY PLAN"] for r in cur.fetchall())
        on_hydro_run = re.findall(r"(\w[\w ]*Scan)(?: using (\w+))? on hydro_run h", plan)
        print(json.dumps({"statement": label, "hydro_run_access": on_hydro_run}))
        print(plan)
        conn.rollback()
    cur.execute(
        "EXPLAIN (COSTS OFF) UPDATE hydro.hydro_run h SET status = 'published' "
        "WHERE h.status = 'parsed' AND h.parsed_at IS NOT NULL"
    )
    plan = "\n".join(r["QUERY PLAN"] for r in cur.fetchall())
    print(json.dumps({"statement": "node27_autopipeline _publish_display_runs UPDATE (EXPLAIN only)"}))
    print(plan)
    conn.rollback()
    conn.close()


if __name__ == "__main__":
    main()
