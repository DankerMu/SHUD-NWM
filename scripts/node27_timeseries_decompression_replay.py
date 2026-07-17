#!/usr/bin/env python3
"""Bounded one-chunk decompression producer for issue #1069 replay."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.evidence_io import reject_secret_material
from packages.common.safe_fs import atomic_write_bytes_no_follow

CONNECT_TIMEOUT_SECONDS = 5
STATEMENT_TIMEOUT_MS = 240_000
LOCK_TIMEOUT_MS = 5_000
EXPECTED_DATABASE = "nhms"
EXPECTED_NODE = "node-27"
EXPECTED_INSTANCE = "node27-primary-pg15"
TARGET = {
    "hypertable_schema": "hydro",
    "hypertable_name": "river_timeseries",
    "chunk_schema": "_timescaledb_internal",
    "chunk_name": "_hyper_3_7_chunk",
    "range_start": "2026-05-28T00:00:00Z",
    "range_end": "2026-06-04T00:00:00Z",
}
TARGET_RELATION = "_timescaledb_internal._hyper_3_7_chunk"
CATALOG_SQL = """
SELECT is_compressed, range_start, range_end
FROM timescaledb_information.chunks
WHERE hypertable_schema = %s
  AND hypertable_name = %s
  AND chunk_schema = %s
  AND chunk_name = %s
"""
IDENTITY_SQL = """
SELECT current_database(), current_setting('server_version'),
       (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb')
"""


class DecompressionError(RuntimeError):
    """The exact authorized decompression could not be proven safe/complete."""


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _iso_value(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value).replace("+00:00", "Z")


def _canonical(value: Mapping[str, Any]) -> bytes:
    reject_secret_material(value, label="decompression receipt")
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _publish_once(path: Path, payload: Mapping[str, Any]) -> None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    else:
        raise DecompressionError("recovery receipt path already exists")
    atomic_write_bytes_no_follow(path, _canonical(payload), mode=0o600, require_durable_replace=True)


def _catalog_state(cursor: Any, target: Mapping[str, str]) -> tuple[bool, str, str]:
    cursor.execute(
        CATALOG_SQL,
        (
            target["hypertable_schema"],
            target["hypertable_name"],
            target["chunk_schema"],
            target["chunk_name"],
        ),
    )
    row = cursor.fetchone()
    if row is None or cursor.fetchone() is not None:
        raise DecompressionError("authorized recovery target is missing or ambiguous")
    compressed, range_start, range_end = row
    start = _iso_value(range_start)
    end = _iso_value(range_end)
    if start != target["range_start"] or end != target["range_end"]:
        raise DecompressionError("authorized recovery target range differs")
    return bool(compressed), start, end


def _row_count(cursor: Any, target: Mapping[str, str]) -> int:
    from psycopg2 import sql  # type: ignore[import-untyped]

    cursor.execute(
        sql.SQL("SELECT count(*) FROM {}.{}").format(
            sql.Identifier(target["chunk_schema"]),
            sql.Identifier(target["chunk_name"]),
        )
    )
    row = cursor.fetchone()
    count = int(row[0]) if row is not None else 0
    if count < 1:
        raise DecompressionError("authorized recovery target has no rows")
    return count


def _identity(cursor: Any, expected_database: str) -> dict[str, str]:
    cursor.execute(IDENTITY_SQL)
    row = cursor.fetchone()
    if row is None or row[0] != expected_database or not row[1] or not row[2]:
        raise DecompressionError("database identity differs")
    return {
        "dbname": str(row[0]),
        "instance": EXPECTED_INSTANCE,
        "postgres_version": str(row[1]),
        "timescaledb_version": str(row[2]),
    }


def _connect_default(database_url: str) -> Any:
    import psycopg2  # type: ignore[import-untyped]

    return psycopg2.connect(database_url, connect_timeout=CONNECT_TIMEOUT_SECONDS)


def produce_recovery_receipt(
    *,
    database_url: str,
    mutation_head_sha: str,
    receipt_path: Path,
    connect: Callable[[str], Any] = _connect_default,
    target: Mapping[str, str] = TARGET,
    expected_database: str = EXPECTED_DATABASE,
) -> dict[str, Any]:
    """Execute exactly one decompression transaction and publish its receipt."""

    if re.fullmatch(r"[0-9a-f]{40}", mutation_head_sha) is None:
        raise DecompressionError("mutation SHA is invalid")
    started_at = _iso_now()
    connection: Any | None = None
    possible_mutation = False
    try:
        connection = connect(database_url)
        with connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
            cursor.execute(f"SET lock_timeout = {LOCK_TIMEOUT_MS}")
            database_identity = _identity(cursor, expected_database)
            before_compressed, _, _ = _catalog_state(cursor, target)
            if not before_compressed:
                raise DecompressionError("authorized recovery target is not currently compressed")
            before_rows = _row_count(cursor, target)
            possible_mutation = True
            target_relation = f"{target['chunk_schema']}.{target['chunk_name']}"
            cursor.execute("SELECT decompress_chunk(%s::regclass, true)::text", (target_relation,))
            result = cursor.fetchone()
            returned_relation = str(result[0]) if result is not None else ""
            if returned_relation != target_relation:
                raise DecompressionError("decompress_chunk returned a different relation")
            after_compressed, _, _ = _catalog_state(cursor, target)
            after_rows = _row_count(cursor, target)
            if after_compressed or after_rows != before_rows:
                raise DecompressionError("post-decompression state/row parity differs")
        connection.commit()
        receipt = {
            "started_at": started_at,
            "finished_at": _iso_now(),
            "node": EXPECTED_NODE,
            "mutation_head_sha": mutation_head_sha,
            "database_identity": database_identity,
            "target": dict(target),
            "exit_code": 0,
            "decompress_return_relation": returned_relation,
            "after_compressed": False,
            "after_row_count": after_rows,
        }
        _publish_once(receipt_path, receipt)
        return receipt
    except Exception as error:
        if connection is not None:
            try:
                connection.rollback()
            except Exception:
                possible_mutation = True
        failure = {
            "schema_version": "1.0",
            "outcome": "failed",
            "generated_at": _iso_now(),
            "node": EXPECTED_NODE,
            "mutation_head_sha": mutation_head_sha,
            "target": dict(target),
            "failure": {
                "stage": "decompression-producer",
                "mutation_state": "indeterminate" if possible_mutation else "failed_before_mutation",
                "reason": type(error).__name__,
            },
        }
        _publish_once(receipt_path, failure)
        raise DecompressionError("bounded decompression producer failed") from error
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--mutation-head-sha", required=True)
    parser.add_argument("--receipt-path", type=Path, required=True)
    for name in TARGET:
        parser.add_argument(f"--{name.replace('_', '-')}", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise DecompressionError("DATABASE_URL is required")
    produce_recovery_receipt(
        database_url=database_url,
        mutation_head_sha=args.mutation_head_sha,
        receipt_path=args.receipt_path,
        target={name: getattr(args, name) for name in TARGET},
        expected_database=args.database,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
