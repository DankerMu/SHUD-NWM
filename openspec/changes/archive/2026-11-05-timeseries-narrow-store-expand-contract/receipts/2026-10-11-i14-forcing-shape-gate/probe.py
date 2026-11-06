"""#1993 task 8.2, fixture I14-1993-task82.md D5: forcing reader shape probe. READ-ONLY.

Drives the REAL forcing readers of the checkout on PYTHONPATH through a
recording pass-through cursor, so every statement and every parameter in the
output is the product's own, not re-typed here. For each of the eight reader
statements it then asks PostgreSQL for

  * a plan only            EXPLAIN (BUFFERS, COSTS, FORMAT JSON)
  * one measured execution EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)

and writes ONE JSON file. ``gate.py`` beside this file judges that JSON.

Safety, in the order it matters:

  * The one connection is opened with
    ``options='-c default_transaction_read_only=on -c statement_timeout=120s'``.
    Nothing here commits; ``commit()`` on the recording connection is a rollback.
  * The DSN comes from an environment variable (``DATABASE_URL`` unless
    ``--dsn-env`` names another). It is never an argument, never printed, never
    written to the output file.
  * ``scripts/reset_qhh_smoke_db.py``'s forcing DELETE is the ninth registered
    forcing statement. It is a write, not a reader: it is listed in the output
    as not probed and is never executed, planned or imported here.

What is NOT the product's default path, and is recorded as such in the JSON:

  * ``forecast_store.latest_product_station_source`` is the QHH latest-product
    FALLBACK. On node-27 the ``hydro.run_display_coverage`` fast path answers
    first and the fallback never renders. The probe switches the fast path off
    by replacing ``forecast_store._run_display_coverage_available`` for that one
    call (the product's own switch: node-22 has no such table). The SQL and
    parameters of the fallback are still produced by the store.
  * ``display_coverage.station_sample_rows`` lives inside the coverage refresh,
    whose statement ends in ``INSERT ... ON CONFLICT``. Read-only, the product
    call is refused by PostgreSQL after the statement was recorded. The plan-only
    EXPLAIN is taken on that exact INSERT (EXPLAIN without ANALYZE executes
    nothing). The ANALYZE leg cannot run an INSERT, so it runs the same CTE chain
    with the INSERT tail replaced by ``SELECT * FROM coverage`` and is labelled
    ``derived_select``.
  * ``best_available.forcing_inputs``: the repository opens its own connection
    per statement; the probe subclass routes ``_fetch_all`` onto the probe's
    read-only connection instead. The statement and parameters still come from
    ``list_forcing_inputs``.

Load. Every reader is first CALLED (that is how its statement and parameters are
obtained) and then measured once more under EXPLAIN ANALYZE, each bounded by the
120 s ``statement_timeout``. A reader with no index-usable predicate on the fact
table can therefore read it for up to two timeouts. ``--plan-only-for KEY``
(repeatable) drops the ANALYZE leg for a statement whose plan is already known
to be a full scan; ``gate.py`` then reports that statement as not measured.

Usage (node-27, verify checkout, the repo's venv):

    DATABASE_URL=... PYTHONPATH=<checkout> python probe.py --out <file>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor

from packages.common import best_available, display_coverage, forecast_store
from packages.common.forcing_ts_render import render_forcing_ts_sql
from workers.model_registry import qhh_production_bootstrap

CONNECT_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=120s"

FACT_SCHEMA = "met"
FACT_TABLE = "forcing_station_timeseries"

#: The eight forcing READER statements on master (fixture D5(a)): registry key,
#: the module that holds the template pair, the pair's attribute name, and the
#: driver whose recorded statements are searched for the rendered text.
READERS: tuple[tuple[str, Any, str, str], ...] = (
    ("forecast_store.latest_product_station_source", forecast_store,
     "_LATEST_PRODUCT_STATION_SOURCE_TEMPLATES", "qhh_latest_product_fallback"),
    ("forecast_store.station_forcing_membership", forecast_store,
     "_STATION_FORCING_MEMBERSHIP_TEMPLATES", "station_series"),
    ("forecast_store.station_series_rows", forecast_store,
     "_STATION_SERIES_ROWS_TEMPLATES", "station_series"),
    ("forecast_store.forcing_readiness_overall", forecast_store,
     "_FORCING_READINESS_OVERALL_TEMPLATES", "station_forcing_readiness"),
    ("forecast_store.forcing_readiness_variable_rows", forecast_store,
     "_FORCING_READINESS_VARIABLE_ROWS_TEMPLATES", "station_forcing_readiness"),
    ("display_coverage.station_sample_rows", display_coverage,
     "_STATION_SAMPLE_ROWS_TEMPLATES", "display_coverage_refresh"),
    ("best_available.forcing_inputs", best_available,
     "_FORCING_INPUTS_TEMPLATES", "best_available_forcing_inputs"),
    ("qhh_production_bootstrap.dynamic_forcing_count", qhh_production_bootstrap,
     "_DYNAMIC_FORCING_COUNT_TEMPLATES", "qhh_bootstrap_dynamic_forcing_counts"),
)  # fmt: skip

NOT_PROBED = (
    {
        "key": "reset_qhh_smoke_db.forcing_timeseries_delete",
        "path": "scripts/reset_qhh_smoke_db.py",
        "reason": "not a reader, not probed (a DELETE; never executed, planned or imported by this probe)",
    },
)

#: Marks where the coverage refresh stops reading and starts writing.
_COVERAGE_INSERT_MARKER = "INSERT INTO hydro.run_display_coverage"

#: Candidate runs examined, newest first, until one has narrow forcing facts.
_IDENTITY_CANDIDATES = 50

_IDENTITY_CANDIDATES_SQL = """
SELECT h.run_id, h.model_id, h.source_id, h.cycle_time, h.status,
       h.basin_version_id, bv.basin_id,
       fv.forcing_version_id, fv.forcing_version_key, fv.station_count
FROM hydro.hydro_run h
JOIN met.forcing_version fv ON fv.forcing_version_id = h.forcing_version_id
JOIN core.basin_version bv ON bv.basin_version_id = h.basin_version_id
WHERE h.run_type = 'forecast'
  AND h.status = 'published'
  AND fv.station_count > 0
  AND fv.checksum IS NOT NULL
  AND upper(h.source_id) IN ('GFS', 'IFS')
ORDER BY (bv.basin_id = %(preferred_basin)s) DESC, h.cycle_time DESC
LIMIT %(limit)s
"""

#: One index descent on the narrow table's successor index: does this version
#: have narrow facts, and for which station?
_IDENTITY_FACT_PROBE_SQL = """
SELECT ms.station_id, ms.station_key
FROM met.forcing_station_timeseries f
JOIN met.met_station ms ON ms.station_key = f.station_key
WHERE f.forcing_version_key = %(forcing_version_key)s
LIMIT 1
"""

_FACT_CHUNKS_SQL = """
SELECT chunk_name, is_compressed
FROM timescaledb_information.chunks
WHERE hypertable_schema = %(schema)s AND hypertable_name = %(table)s
ORDER BY range_start
"""


class RecordingCursor:
    """Pass-through cursor that records every statement and its parameters.

    Recorded BEFORE the driver sees the statement, so a statement PostgreSQL
    refuses (the coverage refresh's INSERT under read-only) is still captured.
    """

    def __init__(self, cursor: Any, sink: list[dict[str, Any]]) -> None:
        self._cursor = cursor
        self._sink = sink

    def execute(self, statement: Any, parameters: Any = None) -> Any:
        if isinstance(parameters, Mapping):
            captured: Any = dict(parameters)
        elif parameters is None:
            captured = None
        else:
            captured = tuple(parameters)
        self._sink.append({"sql": str(statement), "params": captured})
        return self._cursor.execute(statement, parameters)

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self._cursor.close()
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class RecordingConnection:
    """The connection the coverage refresh is handed: recording cursors, no commit."""

    def __init__(self, connection: Any, sink: list[dict[str, Any]]) -> None:
        self._connection = connection
        self._sink = sink

    def cursor(self, *args: Any, **kwargs: Any) -> RecordingCursor:
        return RecordingCursor(self._connection.cursor(*args, **kwargs), self._sink)

    def commit(self) -> None:
        # Read-only already makes a commit harmless; a rollback makes it moot.
        self._connection.rollback()

    def rollback(self) -> None:
        self._connection.rollback()


class ProbeForecastStore(forecast_store.PsycopgForecastStore):
    """The real store, with its transaction replaced by the probe's connection."""

    def __init__(self, connection: Any, sink: list[dict[str, Any]]) -> None:
        super().__init__("probe-1993-forcing-shape-gate")
        object.__setattr__(self, "_probe_connection", connection)
        object.__setattr__(self, "_probe_sink", sink)

    @contextmanager
    def _transaction(self) -> Any:
        cursor = self._probe_connection.cursor(cursor_factory=RealDictCursor)
        try:
            yield RecordingCursor(cursor, self._probe_sink)
        finally:
            cursor.close()


class ProbeBestAvailableRepository(best_available.PsycopgBestAvailableRepository):
    """The real repository; ``_fetch_all`` runs on the probe's connection."""

    def __init__(self, connection: Any, sink: list[dict[str, Any]]) -> None:
        super().__init__("probe-1993-forcing-shape-gate")
        object.__setattr__(self, "_probe_connection", connection)
        object.__setattr__(self, "_probe_sink", sink)

    def _fetch_all(self, statement: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        cursor = RecordingCursor(self._probe_connection.cursor(cursor_factory=RealDictCursor), self._probe_sink)
        try:
            cursor.execute(statement, parameters)
            return [dict(row) for row in cursor.fetchall()]
        finally:
            cursor.close()


def rendered_reader_sql(module: Any, attribute: str, key: str) -> str:
    """The reader's narrow statement text, rendered by the product's renderer."""
    return render_forcing_ts_sql(getattr(module, attribute), "narrow", entry=key).sql


def find_recorded(recorded: list[dict[str, Any]], rendered: str) -> dict[str, Any] | None:
    """The recorded statement that carries the reader's rendered text."""
    for statement in recorded:
        if rendered in statement["sql"]:
            return statement
    return None


def walk_plan(node: Mapping[str, Any], out: list[dict[str, Any]], depth: int = 0, parent: str | None = None) -> list:
    """Flatten a plan tree to the fields the gate judges.

    ``parent_relation`` is the nearest ancestor's relation: a Seq Scan on a
    ``compress_hyper_*`` relation under a DecompressChunk of a fact chunk is a
    whole-chunk read of the fact table and is judged as one.
    """
    relation = node.get("Relation Name")
    entry = {
        "depth": depth,
        "node": node.get("Node Type"),
        "custom_plan_provider": node.get("Custom Plan Provider"),
        "relation": relation,
        "parent_relation": parent,
        "index": node.get("Index Name"),
        "index_cond": node.get("Index Cond"),
        "filter": node.get("Filter"),
        "rows_removed_by_filter": node.get("Rows Removed by Filter"),
        "plan_rows": node.get("Plan Rows"),
        "total_cost": node.get("Total Cost"),
        "actual_rows": node.get("Actual Rows"),
        "actual_loops": node.get("Actual Loops"),
        "shared_hit": node.get("Shared Hit Blocks"),
        "shared_read": node.get("Shared Read Blocks"),
    }
    out.append({name: value for name, value in entry.items() if value is not None})
    for child in node.get("Plans") or ():
        walk_plan(child, out, depth + 1, relation or parent)
    return out


def _error_summary(error: BaseException) -> dict[str, Any]:
    """Class, SQLSTATE and the first message line. A server message never carries the DSN."""
    message = str(error).strip().splitlines()[0] if str(error).strip() else ""
    return {"type": type(error).__name__, "pgcode": getattr(error, "pgcode", None), "message": message[:400]}


def explain(connection: Any, sql: str, params: Any, *, analyze: bool) -> dict[str, Any]:
    """One EXPLAIN, flattened. Always leaves the connection outside a transaction."""
    options = "ANALYZE, BUFFERS, FORMAT JSON" if analyze else "BUFFERS, COSTS, FORMAT JSON"
    started = time.monotonic()
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN ({options}) {sql}", params)
            row = cursor.fetchone()
        blob = row["QUERY PLAN"] if isinstance(row, Mapping) else row[0]
        if isinstance(blob, str):
            blob = json.loads(blob)
        top = blob[0]
        root = top["Plan"]
        result: dict[str, Any] = {
            "explain_options": options,
            "client_wall_ms": round((time.monotonic() - started) * 1000, 3),
            "planning_ms": top.get("Planning Time"),
            "execution_ms": top.get("Execution Time"),
            "shared_hit": root.get("Shared Hit Blocks"),
            "shared_read": root.get("Shared Read Blocks"),
            "total_cost": root.get("Total Cost"),
            "actual_rows": root.get("Actual Rows"),
            "plan_nodes": walk_plan(root, []),
        }
        return result
    except psycopg2.Error as error:
        return {
            "explain_options": options,
            "client_wall_ms": round((time.monotonic() - started) * 1000, 3),
            "error": _error_summary(error),
        }
    finally:
        connection.rollback()


def derive_coverage_select(sql: str) -> str | None:
    """The coverage refresh's CTE chain with its INSERT tail replaced by a SELECT."""
    head, marker, _tail = sql.partition(_COVERAGE_INSERT_MARKER)
    if not marker:
        return None
    return head + "SELECT * FROM coverage"


def select_identity(connection: Any, preferred_basin: str) -> dict[str, Any] | None:
    """A recent published forecast run whose forcing version has narrow facts.

    Read-only: one bounded candidate list over the authority tables, then one
    index descent per candidate until a version with narrow facts is found.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                _IDENTITY_CANDIDATES_SQL, {"preferred_basin": preferred_basin, "limit": _IDENTITY_CANDIDATES}
            )
            candidates = [dict(row) for row in cursor.fetchall()]
            for candidate in candidates:
                cursor.execute(_IDENTITY_FACT_PROBE_SQL, {"forcing_version_key": candidate["forcing_version_key"]})
                station = cursor.fetchone()
                if station is not None:
                    return {**candidate, "station_id": station["station_id"], "station_key": station["station_key"]}
        return None
    finally:
        connection.rollback()


def fact_relations(connection: Any) -> dict[str, Any]:
    """The narrow hypertable and its chunks, from the PUBLIC information view."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(_FACT_CHUNKS_SQL, {"schema": FACT_SCHEMA, "table": FACT_TABLE})
            chunks = [dict(row) for row in cursor.fetchall()]
    finally:
        connection.rollback()
    return {
        "hypertable": f"{FACT_SCHEMA}.{FACT_TABLE}",
        "relations": [FACT_TABLE, *(chunk["chunk_name"] for chunk in chunks)],
        "chunks": len(chunks),
        "compressed_chunks": sum(1 for chunk in chunks if chunk["is_compressed"]),
    }


def server_facts(connection: Any) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    try:
        with connection.cursor() as cursor:
            for name in ("server_version", "default_transaction_read_only", "transaction_read_only",
                         "statement_timeout"):  # fmt: skip
                cursor.execute("SELECT current_setting(%s) AS value", (name,))
                facts[name] = cursor.fetchone()["value"]
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")
            row = cursor.fetchone()
            facts["timescaledb"] = row["extversion"] if row else None
            cursor.execute(
                "SELECT to_regclass('met.forcing_station_timeseries_legacy') IS NOT NULL AS legacy_table_present, "
                "to_regclass('hydro.run_display_coverage') IS NOT NULL AS run_display_coverage_present"
            )
            facts.update(dict(cursor.fetchone()))
    finally:
        connection.rollback()
    return facts


def _run_driver(connection: Any, call: Callable[[], Any]) -> dict[str, Any] | None:
    """Run one product entry point; return its error summary, if it raised."""
    try:
        call()
        return None
    except Exception as error:  # noqa: BLE001 - every product failure is evidence, not a crash
        return _error_summary(error)
    finally:
        connection.rollback()


def drive_readers(connection: Any, identity: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Call each real reader once; return ``{driver: {recorded, error, notes}}``."""
    drivers: dict[str, dict[str, Any]] = {}

    def run(name: str, body: Callable[[list[dict[str, Any]]], Any], notes: list[str]) -> None:
        sink: list[dict[str, Any]] = []
        error = _run_driver(connection, lambda: body(sink))
        drivers[name] = {"recorded": sink, "error": error, "notes": notes}

    def qhh_fallback(sink: list[dict[str, Any]]) -> None:
        original = forecast_store._run_display_coverage_available
        forecast_store._run_display_coverage_available = lambda cursor: False
        try:
            ProbeForecastStore(connection, sink).latest_qhh_display_product(
                str(identity["source_id"]),
                basin_id=str(identity["basin_id"]),
                run_id=str(identity["run_id"]),
                cycle_time=identity["cycle_time"],
                model_id=str(identity["model_id"]),
            )
        finally:
            forecast_store._run_display_coverage_available = original

    run(
        "qhh_latest_product_fallback",
        qhh_fallback,
        ["fast path forced off: forecast_store._run_display_coverage_available replaced by `lambda cursor: False` "
         "for this call, so the authoritative CTE fallback renders (strict identity: run_id + cycle_time + model_id)"],
    )  # fmt: skip
    run(
        "station_series",
        lambda sink: ProbeForecastStore(connection, sink).station_series(
            station_id=str(identity["station_id"]), forcing_version_id=str(identity["forcing_version_id"])
        ),
        [],
    )
    run(
        "station_forcing_readiness",
        lambda sink: ProbeForecastStore(connection, sink).station_forcing_readiness(
            forcing_version_id=str(identity["forcing_version_id"])
        ),
        [],
    )
    run(
        "display_coverage_refresh",
        lambda sink: display_coverage.refresh_run_display_coverage(
            RecordingConnection(connection, sink), str(identity["run_id"])
        ),
        ["the refresh ends in INSERT ... ON CONFLICT; under default_transaction_read_only=on PostgreSQL refuses it "
         "(ReadOnlySqlTransaction is the EXPECTED driver error here) after the statement was recorded"],
    )  # fmt: skip
    run(
        "best_available_forcing_inputs",
        lambda sink: ProbeBestAvailableRepository(connection, sink).list_forcing_inputs(
            str(identity["forcing_version_id"])
        ),
        ["_fetch_all routed onto the probe's read-only connection; the repository normally opens its own"],
    )

    def bootstrap_counts(sink: list[dict[str, Any]]) -> None:
        cursor = RecordingCursor(connection.cursor(cursor_factory=RealDictCursor), sink)
        try:
            qhh_production_bootstrap._dynamic_forcing_counts(cursor, str(identity["model_id"]))
        finally:
            cursor.close()

    run("qhh_bootstrap_dynamic_forcing_counts", bootstrap_counts, [])
    return drivers


def probe_statements(
    connection: Any, drivers: Mapping[str, Mapping[str, Any]], *, plan_only_for: frozenset[str] = frozenset()
) -> dict[str, dict[str, Any]]:
    """Match each reader to its recorded statement and take both EXPLAINs."""
    statements: dict[str, dict[str, Any]] = {}
    for key, module, attribute, driver_name in READERS:
        driver = drivers[driver_name]
        entry: dict[str, Any] = {
            "driver": driver_name,
            "template": f"{module.__name__}.{attribute}",
            "driver_error": driver["error"],
            "driver_notes": list(driver["notes"]),
            "driver_statements_recorded": len(driver["recorded"]),
        }
        recorded = find_recorded(driver["recorded"], rendered_reader_sql(module, attribute, key))
        if recorded is None:
            entry["capture_error"] = "NOT_RECORDED: the driver issued no statement carrying this reader's rendered SQL"
            statements[key] = entry
            continue
        sql, params = recorded["sql"], recorded["params"]
        entry["sql"] = sql
        entry["params"] = params
        entry["plan_only"] = explain(connection, sql, params, analyze=False)
        analyze_sql, analyze_statement = sql, "product"
        if _COVERAGE_INSERT_MARKER in sql:
            derived = derive_coverage_select(sql)
            assert derived is not None
            analyze_sql, analyze_statement = derived, "derived_select"
            entry["analyze_sql"] = derived
        entry["analyze_statement"] = analyze_statement
        if key in plan_only_for:
            entry["analyze"] = {"skipped": "--plan-only-for: the operator asked for no measured execution"}
        else:
            entry["analyze"] = explain(connection, analyze_sql, params, analyze=True)
        statements[key] = entry
    return statements


def build_report(
    connection: Any, *, preferred_basin: str, plan_only_for: frozenset[str] = frozenset()
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "probe": "2026-10-11-i14-forcing-shape-gate",
        "issue": 1993,
        "fixture": "openspec/changes/timeseries-narrow-store-expand-contract/fixtures/I14-1993-task82.md#D5",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "checkout": os.path.dirname(os.path.dirname(os.path.abspath(forecast_store.__file__))),
        "connect_options": CONNECT_OPTIONS,
        "server": server_facts(connection),
        "fact": fact_relations(connection),
        "expected_statements": [key for key, *_ in READERS],
        "not_probed": list(NOT_PROBED),
        "preferred_basin": preferred_basin,
        "plan_only_for": sorted(plan_only_for),
        "identity": None,
        "statements": {},
    }
    if str(report["server"].get("transaction_read_only")) != "on":
        report["error"] = "REFUSED: the session is not read-only; nothing was driven"
        return report
    identity = select_identity(connection, preferred_basin)
    if identity is None:
        report["error"] = "NO_IDENTITY: no recent published forecast run has narrow forcing facts"
        return report
    report["identity"] = identity
    report["statements"] = probe_statements(
        connection, drive_readers(connection, identity), plan_only_for=plan_only_for
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--out", required=True, help="path of the one JSON file to write")
    parser.add_argument("--dsn-env", default="DATABASE_URL", help="NAME of the environment variable holding the DSN")
    parser.add_argument(
        "--preferred-basin",
        default=forecast_store.QHH_BASIN_ID,
        help="basin whose newest published run is preferred as the identity (the 2026-09-20 baseline's basin)",
    )
    parser.add_argument(
        "--plan-only-for",
        action="append",
        default=[],
        choices=[key for key, *_ in READERS],
        metavar="KEY",
        help="skip the EXPLAIN ANALYZE leg for this reader statement (repeatable)",
    )
    args = parser.parse_args(argv)
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        print(f"probe: environment variable {args.dsn_env} is unset or empty", file=sys.stderr)
        return 2
    try:
        connection = psycopg2.connect(dsn, options=CONNECT_OPTIONS, cursor_factory=RealDictCursor)
    except psycopg2.Error as error:
        # Class only: a libpq connection message can echo host and user.
        print(f"probe: could not connect ({type(error).__name__})", file=sys.stderr)
        return 2
    try:
        report = build_report(
            connection, preferred_basin=args.preferred_basin, plan_only_for=frozenset(args.plan_only_for)
        )
    finally:
        connection.close()
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, default=str)
        handle.write("\n")
    captured = sum(1 for entry in report["statements"].values() if "plan_only" in entry)
    print(f"probe: wrote {args.out}; {captured} of {len(READERS)} reader statements captured")
    if report.get("error"):
        print(f"probe: {report['error']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
