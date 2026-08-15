"""Shared cursor-level fakes for the #1339 backfill runner's unit tests.

The database is faked at the CURSOR boundary — the same seam
``tests/test_node27_timeseries_compression.py`` uses. Extracted into its own
module (house style: ``tests/integration_helpers.py``) so the runner's two test
modules share one fake instead of two drifting copies.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts import node27_river_identity_backfill as backfill

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas/river_identity_backfill_receipt.schema.json"
RUNNER_SOURCE_PATH = ROOT / "scripts/node27_river_identity_backfill.py"
MIGRATION_PATH = ROOT / "db/migrations/000050_river_identity_normalization.sql"

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
LAG = 604_800  # 7 days, the compression lane's default


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "enforce": False,
        "probe": False,
        "final_sweep": False,
        "receipt_path": None,
        "lock_path": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def env(tmp_path: Path, **overrides: str | None) -> dict[str, str]:
    values: dict[str, str] = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": str(LAG),
        "NODE27_RIVER_IDENTITY_BACKFILL_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_RIVER_IDENTITY_BACKFILL_LOCK_PATH": str(tmp_path / "runner.lock"),
    }
    for key, value in overrides.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return values


_MODE_FLAGS = {"enforce", "probe", "final_sweep"}


def config(tmp_path: Path, **overrides: Any) -> backfill.BackfillConfig:
    base = backfill.config_from_args(
        args(**{k: v for k, v in overrides.items() if k in _MODE_FLAGS}),
        env(tmp_path),
    )
    replacements = {k: v for k, v in overrides.items() if k not in _MODE_FLAGS}
    return dataclasses.replace(base, **replacements) if replacements else base


def chunk(
    name: str,
    *,
    days_old: float,
    compressed: bool = False,
    pages: int = 10,
    total_bytes: int = 8192 * 10,
) -> backfill.ChunkRow:
    end = NOW - timedelta(days=days_old)
    return backfill.ChunkRow(
        chunk_schema="_timescaledb_internal",
        chunk_name=name,
        range_start=end - timedelta(days=7),
        range_end=end,
        is_compressed=compressed,
        total_bytes=total_bytes,
        relpages=pages,
        pages_planned=pages,
        approx_rows=pages * 80,
    )


def inventory_row(row: backfill.ChunkRow) -> dict[str, Any]:
    return {
        "chunk_schema": row.chunk_schema,
        "chunk_name": row.chunk_name,
        "range_start": row.range_start,
        "range_end": row.range_end,
        "is_compressed": row.is_compressed,
        "total_bytes": row.total_bytes,
        "relpages": row.relpages,
        "pages_planned": row.pages_planned,
        "approx_rows": row.approx_rows,
    }


class QueryCancelled(Exception):
    """Stand-in for psycopg2's QueryCanceled (SQLSTATE 57014)."""

    pgcode = "57014"

    def __str__(self) -> str:  # pragma: no cover - message shape only
        return "canceling statement due to statement timeout"


class FakeCursor:
    """Cursor that answers by SQL-substring match and records every call."""

    def __init__(self, connection: "FakeConnection") -> None:
        self.connection = connection
        self._result: Any = None
        self._rows: list[Any] = []
        self.rowcount = -1

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.connection.executions.append((sql, params))
        for matcher, handler in self.connection.handlers:
            if matcher in sql:
                outcome = handler(sql, params)
                if isinstance(outcome, Exception):
                    raise outcome
                if isinstance(outcome, dict):
                    self._result = outcome.get("fetchone")
                    self._rows = outcome.get("fetchall", [])
                    self.rowcount = int(outcome.get("rowcount", -1))
                else:
                    self._result = (outcome,)
                    self._rows = []
                    self.rowcount = -1
                return
        self._result = None
        self._rows = []
        self.rowcount = -1

    def fetchone(self) -> Any:
        return self._result

    def fetchall(self) -> list[Any]:
        return self._rows

    def close(self) -> None:
        return None


class FakeConnection:
    def __init__(self, handlers: list[tuple[str, Any]]) -> None:
        self.handlers = handlers
        self.executions: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


# The guard's chunk-identity lookup answers "not compressed". Every fake that
# drives a real batch needs it: the guard now runs inside each batch
# transaction and fails closed on an unknown chunk.
UNCOMPRESSED_GUARD: tuple[str, Any] = (
    "SELECT is_compressed FROM timescaledb_information.chunks",
    lambda _sql, _params: {"fetchone": (False,)},
)


def statements(connection: FakeConnection, needle: str) -> list[tuple[str, Any]]:
    return [call for call in connection.executions if needle in call[0]]
