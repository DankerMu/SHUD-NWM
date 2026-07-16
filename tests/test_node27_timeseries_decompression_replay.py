"""Focused tests for the bounded issue #1069 decompression producer."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_timeseries_decompression_replay as replay


class _FakeCursor:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = iter(responses)
        self.executed: list[tuple[Any, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, statement: Any, params: Any = None) -> None:
        self.executed.append((statement, params))

    def fetchone(self) -> Any:
        return next(self.responses)


class _FakeConnection:
    def __init__(self, responses: list[Any]) -> None:
        self.cursor_value = _FakeCursor(responses)
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self.cursor_value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _responses(*, after_count: int = 7, after_compressed: bool = False) -> list[Any]:
    return [
        ("nhms", "15.2", "2.10.2"),
        (True, datetime(2026, 5, 28, tzinfo=UTC), datetime(2026, 6, 4, tzinfo=UTC)),
        None,
        (7,),
        (replay.TARGET_RELATION,),
        (after_compressed, datetime(2026, 5, 28, tzinfo=UTC), datetime(2026, 6, 4, tzinfo=UTC)),
        None,
        (after_count,),
    ]


def test_fake_db_exact_decompression_publishes_structured_receipt(tmp_path: Path) -> None:
    connection = _FakeConnection(_responses())
    receipt_path = tmp_path / "recovery.json"
    receipt = replay.produce_recovery_receipt(
        database_url="opaque",
        mutation_head_sha="a" * 40,
        receipt_path=receipt_path,
        connect=lambda _url: connection,
    )
    assert connection.committed and connection.closed and not connection.rolled_back
    assert json.loads(receipt_path.read_text()) == receipt
    assert receipt["target"] == replay.TARGET
    assert receipt["decompress_return_relation"] == replay.TARGET_RELATION
    assert receipt["after_row_count"] == 7
    mutation_calls = [
        params for statement, params in connection.cursor_value.executed if "decompress_chunk" in str(statement)
    ]
    assert mutation_calls == [(replay.TARGET_RELATION,)]


def test_fake_db_post_state_mismatch_publishes_indeterminate_without_retry(tmp_path: Path) -> None:
    connection = _FakeConnection(_responses(after_count=6))
    receipt_path = tmp_path / "failed.json"
    with pytest.raises(replay.DecompressionError, match="producer failed"):
        replay.produce_recovery_receipt(
            database_url="opaque",
            mutation_head_sha="a" * 40,
            receipt_path=receipt_path,
            connect=lambda _url: connection,
        )
    failure = json.loads(receipt_path.read_text())
    assert failure["outcome"] == "failed"
    assert failure["failure"]["mutation_state"] == "indeterminate"
    assert connection.rolled_back and connection.closed and not connection.committed
    assert sum("decompress_chunk" in str(statement) for statement, _ in connection.cursor_value.executed) == 1


@pytest.mark.integration
def test_real_timescaledb_production_entrypoint_decompresses_ephemeral_exact_fixture(
    integration_database_url: str, tmp_path: Path
) -> None:
    import psycopg2  # type: ignore[import-untyped]

    connection = psycopg2.connect(integration_database_url)
    target: dict[str, str]
    database = connection.get_dsn_parameters()["dbname"]
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                cursor.execute("DROP SCHEMA IF EXISTS recovery_replay_test CASCADE")
                cursor.execute("CREATE SCHEMA recovery_replay_test")
                cursor.execute(
                    "CREATE TABLE recovery_replay_test.series (valid_time timestamptz NOT NULL, value integer NOT NULL)"
                )
                cursor.execute(
                    "SELECT create_hypertable('recovery_replay_test.series','valid_time',"
                    "chunk_time_interval=>INTERVAL '7 days')"
                )
                cursor.execute("ALTER TABLE recovery_replay_test.series SET (timescaledb.compress)")
                cursor.execute(
                    "INSERT INTO recovery_replay_test.series SELECT ts, 1 FROM generate_series(" 
                    "'2026-05-28T00:00:00Z'::timestamptz,'2026-06-03T00:00:00Z','1 day') ts"
                )
                cursor.execute(
                    "SELECT chunk_schema,chunk_name,range_start,range_end FROM timescaledb_information.chunks "
                    "WHERE hypertable_schema='recovery_replay_test' AND hypertable_name='series' "
                    "ORDER BY range_start LIMIT 1"
                )
                chunk_schema, chunk_name, range_start, range_end = cursor.fetchone()
                cursor.execute("SELECT compress_chunk(%s::regclass)", (f"{chunk_schema}.{chunk_name}",))
                target = {
                    "hypertable_schema": "recovery_replay_test",
                    "hypertable_name": "series",
                    "chunk_schema": str(chunk_schema),
                    "chunk_name": str(chunk_name),
                    "range_start": replay._iso_value(range_start),
                    "range_end": replay._iso_value(range_end),
                }
        receipt = tmp_path / "real-recovery.json"
        argv = [
            sys.executable,
            str(Path(replay.__file__).resolve()),
            "--database",
            database,
            "--mutation-head-sha",
            "a" * 40,
            "--receipt-path",
            str(receipt),
            *[item for key, value in target.items() for item in (f"--{key.replace('_', '-')}", value)],
        ]
        result = subprocess.run(
            argv,
            cwd=Path(__file__).parents[1],
            env={**os.environ, "DATABASE_URL": integration_database_url},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(receipt.read_text())
        assert payload["target"] == target
        assert payload["exit_code"] == 0
        assert payload["after_compressed"] is False
        assert payload["after_row_count"] > 0
    finally:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("DROP SCHEMA IF EXISTS recovery_replay_test CASCADE")
        connection.close()
