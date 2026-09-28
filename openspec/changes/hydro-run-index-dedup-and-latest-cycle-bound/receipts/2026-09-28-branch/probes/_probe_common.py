"""Shared helpers for the L2 node-27 read-only probes (#2424 #2516 #2418).

Environment:
  WT       absolute path of the checkout whose code is imported (master or branch)
  DSNFILE  file holding ONE libpq DSN line for a READ-ONLY role (nhms_display_ro)

Every connection is ``set_session(readonly=True, autocommit=False)`` and every
transaction starts with ``SET LOCAL statement_timeout``. Nothing here writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

WT = os.environ.get("WT", "").strip()
DSNFILE = os.environ.get("DSNFILE", "").strip()
if not WT or not Path(WT, "packages", "common", "forecast_store.py").is_file():
    raise SystemExit("WT must be the absolute path of an NWM checkout")
if not DSNFILE or not Path(DSNFILE).is_file():
    raise SystemExit("DSNFILE must name a file holding the read-only DSN")
sys.path.insert(0, WT)

import psycopg2  # noqa: E402
from psycopg2.extras import RealDictCursor  # noqa: E402

from packages.common.forecast_store import PsycopgForecastStore  # noqa: E402


def dsn() -> str:
    return Path(DSNFILE).read_text(encoding="utf-8").strip()


def connect(*, isolation: str | None = None) -> Any:
    connection = psycopg2.connect(dsn(), cursor_factory=RealDictCursor, application_name="nhms-l2-probe")
    if isolation:
        connection.set_session(isolation_level=isolation, readonly=True, autocommit=False)
    else:
        connection.set_session(readonly=True, autocommit=False)
    return connection


def begin(connection: Any, timeout: str = "60s") -> None:
    """Open the next read-only transaction with its SET LOCAL timeout."""
    with connection.cursor() as cursor:
        cursor.execute(f"SET LOCAL statement_timeout = '{timeout}'")
        cursor.execute("SHOW transaction_read_only")
        assert cursor.fetchone()["transaction_read_only"] == "on", "session is not read-only"


def checkout_head() -> str:
    head = Path(WT, ".git")
    try:
        import subprocess

        return subprocess.run(
            ["git", "-C", WT, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return f"unknown ({head})"


class RecordingCursor:
    """Pass-through cursor recording each statement, its binding and its rows."""

    def __init__(self, cursor: Any, sink: list[dict[str, Any]]) -> None:
        self._cursor = cursor
        self._sink = sink
        self._last: dict[str, Any] | None = None

    def execute(self, statement: Any, parameters: Any = None) -> None:
        if isinstance(parameters, Mapping):
            captured: Any = dict(parameters)
        elif parameters is None:
            captured = None
        else:
            captured = tuple(parameters)
        self._last = {"sql": str(statement), "params": captured}
        self._sink.append(self._last)
        self._cursor.execute(statement, parameters)

    def fetchall(self) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self._cursor.fetchall()]
        if self._last is not None:
            self._last["rows"] = rows
        return rows

    def fetchone(self) -> dict[str, Any] | None:
        row = self._cursor.fetchone()
        if self._last is not None:
            self._last["rows"] = [] if row is None else [dict(row)]
        return None if row is None else dict(row)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class RecordingStore(PsycopgForecastStore):
    """The checkout's real store, driven over a probe-owned read-only connection."""

    def __init__(self, connection: Any, sink: list[dict[str, Any]]) -> None:
        super().__init__("recording://l2-probe")
        object.__setattr__(self, "_probe_connection", connection)
        object.__setattr__(self, "_probe_sink", sink)

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        cursor = self._probe_connection.cursor()
        try:
            yield RecordingCursor(cursor, self._probe_sink)
        finally:
            cursor.close()


def explain(connection: Any, statement: str, parameters: Any) -> dict[str, Any]:
    """EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) of one captured statement."""
    with connection.cursor() as cursor:
        cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters)
        payload = cursor.fetchone()["QUERY PLAN"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload[0]


def root_summary(plan_document: Mapping[str, Any]) -> dict[str, Any]:
    root = plan_document["Plan"]
    return {
        "shared_hit": root.get("Shared Hit Blocks"),
        "shared_read": root.get("Shared Read Blocks"),
        "execution_ms": plan_document.get("Execution Time"),
        "planning_ms": plan_document.get("Planning Time"),
        "actual_rows": root.get("Actual Rows"),
    }


def walk(node: Mapping[str, Any], parent: Mapping[str, Any] | None = None) -> Iterator[tuple[Any, Any]]:
    yield node, parent
    for child in node.get("Plans", []) or []:
        yield from walk(child, node)


def row_digest(rows: list[Mapping[str, Any]]) -> str:
    preimage = "\n".join(repr(sorted((str(k), str(v)) for k, v in row.items())) for row in rows)
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def fact_statements(sink: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [st for st in sink if "hydro.river_timeseries" in st["sql"] and isinstance(st["params"], dict)]


def statement_label(sql: str) -> str:
    if "cand AS MATERIALIZED" in sql:
        return "latest_cycle_discovery(D1)"
    if "MAX(h.cycle_time) AS cycle_time" in sql:
        return "per_source_latest_cycles(pre-D1)"
    if "pushdown_run_keys" in sql or "selected_cycles" in sql:
        return "forecast_segment_rows"
    if "analysis_true_field" in sql:
        return "analysis"
    return "fact_statement"
