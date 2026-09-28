"""#2630 tasks 3.3: master's D1 latest-cycle discovery vs the bounded LATERAL branch, 440 pins.

Adapted from /home/nwm/tmp/l2/probes/equivalence_d1.py (#2424 3.3). There the old side
was the frozen pre-#2424 oracle; here it is MASTER's ``_per_source_latest_cycles``
(unbounded ``WHERE EXISTS``), loaded from the master checkout's file, against the
branch's (bounded ``CROSS JOIN LATERAL (... LIMIT 1)``).

    WT=<branch checkout> OLD_WT=<origin/master checkout> DSNFILE=<read-only DSN file> \\
        /home/nwm/NWM/.venv/bin/python -B equivalence_d1_bounded.py > equivalence-bounded.out

Only ``packages/common/forecast_store.py`` differs between the two checkouts under
packages/ services/ apps/ workers/ (asserted below), so importing master's copy of that
one file next to the branch's other modules runs exactly master's code.

Pins: every core.river_network_version (its basin_version_id), first and last ``_reach_``
segment by id (reach -> shud_riv via the store's own ``_timeseries_segment_id``) x
scenarios {GFS} {IFS} {GFS,IFS}, no identity filter; plus ONE model_id-filtered pin per
basin (first reach, {GFS,IFS}, the model of the basin's latest forecast run).

Per pin, in ONE REPEATABLE READ read-only transaction (shared snapshot): master's method,
the branch's method, then EXPLAIN (ANALYZE, BUFFERS) of BOTH executed statements (each
already warmed by its own execution). Read-only throughout (nhms_display_ro).
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from _probe_common import WT, RecordingCursor, RecordingStore, begin, checkout_head, connect, explain, root_summary

from packages.common import forecast_store

OLD_WT = os.environ["OLD_WT"].strip()
REACH_PATTERN = os.environ.get("D1_REACH_PATTERN", "%\\_reach\\_%")
SCENARIO_SETS = (["GFS"], ["IFS"], ["GFS", "IFS"])


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def _preamble() -> dict:
    old_head = _git("-C", OLD_WT, "rev-parse", "HEAD")
    origin_master = _git("-C", OLD_WT, "rev-parse", "origin/master")
    assert old_head == origin_master, (old_head, origin_master)
    assert _git("-C", OLD_WT, "status", "--porcelain") == "", "master checkout is dirty"
    branch_base = _git("-C", WT, "rev-parse", "HEAD")
    code_diff = _git("-C", WT, "diff", "--name-only", old_head, "--", "packages", "services", "apps", "workers")
    assert code_diff.split() == ["packages/common/forecast_store.py"], code_diff
    assert "cand AS MATERIALIZED" in Path(OLD_WT, "packages/common/forecast_store.py").read_text()
    return {"old_wt": OLD_WT, "old_head": old_head, "wt": WT, "wt_base": branch_base, "code_diff": code_diff}


def _load_master_store():
    path = Path(OLD_WT, "packages", "common", "forecast_store.py")
    spec = importlib.util.spec_from_file_location("forecast_store_master", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MASTER = _load_master_store()


class MasterRecordingStore(MASTER.PsycopgForecastStore):
    def __init__(self, connection, sink) -> None:
        super().__init__("recording://l2-probe-master")
        object.__setattr__(self, "_probe_connection", connection)
        object.__setattr__(self, "_probe_sink", sink)

    @contextmanager
    def _transaction(self):
        cursor = self._probe_connection.cursor()
        try:
            yield RecordingCursor(cursor, self._probe_sink)
        finally:
            cursor.close()


def _networks(connection) -> list[dict]:
    begin(connection, "60s")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT rnv.basin_version_id, rnv.river_network_version_id,
                   MIN(rs.river_segment_id) AS first_reach, MAX(rs.river_segment_id) AS last_reach
            FROM core.river_network_version rnv
            JOIN core.river_segment rs ON rs.river_network_version_id = rnv.river_network_version_id
            WHERE rs.river_segment_id LIKE %(reach_pattern)s
            GROUP BY rnv.basin_version_id, rnv.river_network_version_id
            ORDER BY rnv.basin_version_id, rnv.river_network_version_id
            """,
            {"reach_pattern": REACH_PATTERN},
        )
        networks = [dict(row) for row in cursor.fetchall()]
        for network in networks:
            cursor.execute(
                """
                SELECT model_id FROM hydro.hydro_run
                WHERE basin_version_id = %s AND run_type = 'forecast' AND cycle_time IS NOT NULL
                ORDER BY cycle_time DESC LIMIT 1
                """,
                (network["basin_version_id"],),
            )
            row = cursor.fetchone()
            network["model_id"] = row["model_id"] if row else None
    connection.rollback()
    return networks


def _pins(networks: list[dict]) -> list[dict]:
    pins = []
    for network in networks:
        for reach in dict.fromkeys((network["first_reach"], network["last_reach"])):
            for scenarios in SCENARIO_SETS:
                pins.append({**network, "reach": reach, "scenarios": scenarios, "model_filter": None})
        if network["model_id"]:
            pins.append(
                {
                    **network,
                    "reach": network["first_reach"],
                    "scenarios": ["GFS", "IFS"],
                    "model_filter": network["model_id"],
                }
            )
    return pins


def _run_pin(connection, pin: dict) -> dict:
    scenario_filter = forecast_store._scenario_filter(pin["scenarios"])
    identity_filter = forecast_store._run_identity_filter(run_id=None, model_id=pin["model_filter"])
    identity = {
        "basin_version_id": pin["basin_version_id"],
        "segment_id": forecast_store._timeseries_segment_id(pin["reach"]),
        "river_network_version_id": pin["river_network_version_id"],
        "scenario_filter": scenario_filter,
        "identity_filter": identity_filter,
    }
    result: dict = {
        "pin": {
            k: pin[k] for k in ("basin_version_id", "river_network_version_id", "reach", "scenarios", "model_filter")
        }
    }
    begin(connection, "60s")
    old_sink: list[dict] = []
    started = time.monotonic()
    with connection.cursor() as raw:
        old = MasterRecordingStore(connection, old_sink)._per_source_latest_cycles(
            RecordingCursor(raw, old_sink), **identity
        )
    result["old_ms"] = round((time.monotonic() - started) * 1000, 1)
    new_sink: list[dict] = []
    started = time.monotonic()
    with connection.cursor() as raw:
        new = RecordingStore(connection, new_sink)._per_source_latest_cycles(RecordingCursor(raw, new_sink), **identity)
    result["new_ms"] = round((time.monotonic() - started) * 1000, 1)
    old_statement, new_statement = old_sink[-1], new_sink[-1]
    assert "WHERE EXISTS (" in old_statement["sql"], "the master store did not run master's D1 statement"
    assert "CROSS JOIN LATERAL (" in new_statement["sql"] and "EXISTS" not in new_statement["sql"], (
        "the branch store did not run the bounded statement"
    )
    result["old_explain"] = root_summary(explain(connection, old_statement["sql"], old_statement["params"]))
    result["new_explain"] = root_summary(explain(connection, new_statement["sql"], new_statement["params"]))
    connection.rollback()
    result["old"] = {k: v.isoformat() for k, v in old.items()}
    result["new"] = {k: v.isoformat() for k, v in new.items()}
    result["equal"] = old == new
    return result


def _stats(values: list[int]) -> dict:
    values = sorted(values)
    if not values:
        return {"n": 0, "max": None, "p95_nearest_rank": None}
    return {"n": len(values), "max": values[-1], "p95_nearest_rank": values[max(0, math.ceil(0.95 * len(values)) - 1)]}


def main() -> None:
    print(json.dumps({**_preamble(), "branch_head_file": checkout_head()}))
    connection = connect(isolation="REPEATABLE READ")
    try:
        networks = _networks(connection)
        pins = _pins(networks)
        print(json.dumps({"networks": len(networks), "pins": len(pins)}))
        results = []
        for pin in pins:
            result = _run_pin(connection, pin)
            results.append(result)
            print(json.dumps(result, default=str))
        mismatches = [r for r in results if not r["equal"]]
        empty = [r for r in results if not r["new"]]
        non_empty = [r for r in results if r["new"]]

        def hits(rows, side):
            return [r[f"{side}_explain"]["shared_hit"] or 0 for r in rows]

        summary = {
            "summary": True,
            "pin_count": len(results),
            "non_empty_pins": len(non_empty),
            "empty_pins": len(empty),
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
            "new_empty_shared_hit": _stats(hits(empty, "new")),
            "new_non_empty_shared_hit": _stats(hits(non_empty, "new")),
            "old_empty_shared_hit": _stats(hits(empty, "old")),
            "old_non_empty_shared_hit": _stats(hits(non_empty, "old")),
            "gates": {
                "zero_mismatch": len(mismatches) == 0,
                "empty_max_le_5000": (_stats(hits(empty, "new"))["max"] or 0) <= 5000,
                "non_empty_max_le_3301": (_stats(hits(non_empty, "new"))["max"] or 0) <= 3301,
                "non_empty_p95_le_2530": (_stats(hits(non_empty, "new"))["p95_nearest_rank"] or 0) <= 2530,
            },
            "new_ms_max": max((r["new_ms"] for r in results), default=None),
            "old_ms_max": max((r["old_ms"] for r in results), default=None),
        }
        print(json.dumps(summary, default=str))
    finally:
        connection.rollback()
        connection.close()


if __name__ == "__main__":
    main()
