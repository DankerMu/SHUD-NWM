"""#2418 D4: every workload receipt names the cluster that produced its samples.

Driven through the public CLI (``scripts/node27_pgdata_workload.py main``) with a
database double at the connection boundary — ``connect=`` injection, so the real
``open_readonly_connection`` issues its ``SET LOCAL`` timeouts on the recorded
cursor and the real ``prove_readonly_session`` / ``prove_server_identity`` run
on it — plus one real-PostgreSQL case for the savepoint property a double
cannot prove.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from packages.common import node27_pgdata_workload_io as workload_io
from packages.common.node27_pgdata_workload import SCHEMA_VERSION
from scripts.node27_pgdata_workload import main as cli_main
from tests.test_node27_pgdata_workload import (
    SERVER_IDENTITY_ROW,
    SHA,
    _Connection,
    _measure_argv,
    _MeasureCursor,
    _plan,
    _workload_run_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "l2-super-secret-pw"
DSN = f"host=127.0.0.1 port=5432 dbname=nhms user=nhms_display_ro password={PASSWORD}"
SERVER_KEYS = ["database", "system_identifier", "server_version", "server_addr", "server_port"]
NULL_SERVER = dict.fromkeys(SERVER_KEYS)


class _IdentityCursor(_MeasureCursor):
    """`_MeasureCursor` whose cluster-identity answers are chosen per test."""

    def __init__(self, *, executable: bool = True, row: Any = None, control_raises: bool = False) -> None:
        super().__init__()
        self.executable = executable
        self.row = dict(SERVER_IDENTITY_ROW) if row is None else row
        self.control_raises = control_raises

    def execute(self, statement: str, parameters: object = None) -> None:
        super().execute(statement, parameters)
        if self.control_raises and "pg_control_system" in statement and "has_function_privilege" not in statement:
            raise RuntimeError("permission denied for function pg_control_system")

    def fetchone(self) -> Any:
        last = self.calls[-1][0] if self.calls else ""
        if "has_function_privilege" in last:
            return {"control_executable": self.executable}
        if "SELECT" in last and "current_database()" in last:
            # Without the privilege the probe never calls pg_control_system().
            if "pg_control_system" not in last and isinstance(self.row, dict):
                return {**self.row, "system_identifier": None}
            return self.row
        return super().fetchone()


def _dsn_file(parent: Path) -> Path:
    path = parent / "reader.dsn"
    path.write_text(DSN, encoding="utf-8")
    path.chmod(0o600)
    return path


def _run(
    tmp_path: Path, cursor: _IdentityCursor, *, evidence_kind: str | None = None
) -> tuple[int, Path, _Connection, list[list[str]]]:
    """Run the CLI with `connect=` injection; record the statements each SQL sample saw."""
    parent = _workload_run_dir(tmp_path)
    output = parent / "workload.json"
    connection = _Connection(cursor)
    seen_by_samples: list[list[str]] = []
    plan = _plan(decompress=["_hyper_1_1_chunk"])

    def sql_probe(_index: int) -> dict[str, Any]:
        assert connection.rolled_back is False, "the measuring transaction was rolled back before a sample"
        seen_by_samples.append([statement for statement, _params in cursor.calls])
        return {"duration_ms": 10, "explain_json": plan}

    def api_probe(_index: int) -> dict[str, Any]:
        return {"duration_ms": 20, "status": 200, "body_len": 32, "content_digest": "ab" * 32}

    rc = cli_main(
        _measure_argv(dsn_path=_dsn_file(parent), output=output, evidence_kind=evidence_kind),
        connect=lambda _dsn: connection,
        head_resolver=lambda: SHA,
        sql_probe=sql_probe,
        api_probe=api_probe,
    )
    return rc, output, connection, seen_by_samples


def test_the_receipt_carries_the_server_block_with_the_exact_key_set(tmp_path: Path) -> None:
    rc, output, _connection, _seen = _run(tmp_path, _IdentityCursor(), evidence_kind="live")
    assert rc == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert SCHEMA_VERSION == "1.1"
    assert document["schema_version"] == "1.1"
    # Exact key SET (the receipt is serialised key-sorted).
    assert sorted(document["server"]) == sorted(SERVER_KEYS)
    assert document["server"] == {
        "database": "nhms",
        "system_identifier": "7651116524569100322",
        "server_version": "15.2",
        "server_addr": "127.0.0.1",
        "server_port": 5432,
    }
    # A decimal STRING: the identifier overflows 2**53, so it is never a JSON number.
    assert isinstance(document["server"]["system_identifier"], str)
    assert int(document["server"]["system_identifier"]) > 2**53


def test_the_server_block_carries_no_credential(tmp_path: Path) -> None:
    rc, output, _connection, _seen = _run(tmp_path, _IdentityCursor(), evidence_kind="live")
    assert rc == 0
    raw = output.read_text(encoding="utf-8")
    assert PASSWORD not in raw
    assert "password" not in raw
    server = json.loads(raw)["server"]
    assert not {"user", "password", "dsn", "host", "port", "dbname"} & set(server)
    assert PASSWORD not in json.dumps(server)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({**SERVER_IDENTITY_ROW, "system_identifier": 7651116524569100322}, None),
        ({**SERVER_IDENTITY_ROW, "system_identifier": "76511165x"}, None),
        ({**SERVER_IDENTITY_ROW, "system_identifier": ""}, None),
        ({**SERVER_IDENTITY_ROW, "system_identifier": " 7651116524569100322 "}, "7651116524569100322"),
    ],
)
def test_only_a_decimal_string_is_recorded_as_the_system_identifier(row: dict[str, Any], expected: Any) -> None:
    connection = _Connection(_IdentityCursor(row=row))
    assert workload_io.prove_server_identity(connection)["system_identifier"] == expected


#: The three ways the identity can be missing, per tasks.md 2.3.
MISSES = {
    "not_executable": {"executable": False},
    "control_raises": {"control_raises": True},
    "returns_null": {"row": {**SERVER_IDENTITY_ROW, "system_identifier": None}},
}


@pytest.mark.parametrize("miss", tuple(MISSES))
def test_live_refuses_a_receipt_that_cannot_name_its_cluster(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], miss: str
) -> None:
    rc, output, _connection, seen = _run(tmp_path, _IdentityCursor(**MISSES[miss]), evidence_kind="live")
    assert rc == 1
    assert "SERVER_IDENTITY_MISSING" in capsys.readouterr().err
    assert not output.exists()
    assert sorted(entry.name for entry in output.parent.iterdir()) == ["reader.dsn"]
    assert seen == [], "a refused live run must not take a sample"


@pytest.mark.parametrize("miss", tuple(MISSES))
def test_isolated_records_null_and_keeps_sampling_under_the_set_local_timeouts(tmp_path: Path, miss: str) -> None:
    cursor = _IdentityCursor(**MISSES[miss])
    rc, output, _connection, seen = _run(tmp_path, cursor)
    assert rc == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["evidence_kind"] == "isolated"
    if miss == "returns_null":
        assert document["server"] == {**SERVER_IDENTITY_ROW, "system_identifier": None}
    else:
        assert document["server"]["system_identifier"] is None
    assert len(seen) == 21
    statements = seen[0]
    timeout = statements.index(f"SET LOCAL statement_timeout = {workload_io.STATEMENT_TIMEOUT_MS}")
    lock = statements.index(f"SET LOCAL lock_timeout = '{workload_io.LOCK_TIMEOUT}'")
    savepoint = statements.index(f"SAVEPOINT {workload_io.SERVER_IDENTITY_SAVEPOINT}")
    assert timeout < lock < savepoint
    # Nothing between the timeouts and the samples ended the transaction; a
    # failed probe is undone only back to its own savepoint, which keeps them.
    after = statements[timeout:]
    assert not [s for s in after if s.strip().upper() in {"ROLLBACK", "COMMIT", "RESET ALL", "END"}]
    assert not [s for s in after if s.strip().upper().startswith(("RESET", "SET SESSION"))]
    if miss == "control_raises":
        assert f"ROLLBACK TO SAVEPOINT {workload_io.SERVER_IDENTITY_SAVEPOINT}" in after
    if miss == "not_executable":
        assert not [s for s in after if "pg_control_system()" in s and "has_function_privilege" not in s]


#: The receipts archived at 1.0 are historical artifacts and are never rewritten.
ARCHIVED_RECEIPTS = {
    "archive/*-compressed-chunk-cold-tablespace-tiering/evidence/receipts/retirement-pgdata-workload-smoke.json": (
        "d80f5aa35aa8c2cf239a82040e6faa210fae455ac8a92cf46483617065065c16"
    ),
    "*/receipts/2026-09-18-live-ab/d11-live-receipt.json": (
        "df66937066aff77ba87fab5dd998ab5c5c2be029d47ca44974e43171bd535f42"
    ),
}


@pytest.mark.parametrize("pattern", tuple(ARCHIVED_RECEIPTS))
def test_the_archived_1_0_receipts_are_unchanged(pattern: str) -> None:
    changes = REPO_ROOT / "openspec" / "changes"
    matches = sorted(changes.glob(pattern)) or sorted(changes.glob(f"archive/{pattern}"))
    assert len(matches) == 1, (pattern, matches)
    raw = matches[0].read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ARCHIVED_RECEIPTS[pattern]
    document = json.loads(raw)
    assert "server" not in document
    if document.get("artifact") == "nhms-pgdata-workload":
        assert document["schema_version"] == "1.0"


@pytest.mark.integration
def test_a_failed_identity_probe_keeps_the_real_transaction_and_its_timeouts(
    throwaway_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a real server can show the savepoint keeps the SET LOCAL timeouts."""
    connection = psycopg2.connect(throwaway_database_url, cursor_factory=RealDictCursor)
    try:
        connection.set_session(readonly=True, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = 4321")
            cursor.execute("SELECT system_identifier::text AS identifier FROM pg_control_system()")
            identifier = cursor.fetchone()["identifier"]
            cursor.execute("SELECT current_database() AS database")
            database = cursor.fetchone()["database"]
        server = workload_io.prove_server_identity(connection)
        assert server["system_identifier"] == identifier
        assert server["system_identifier"].isdigit()
        assert server["database"] == database
        assert set(server) == set(SERVER_KEYS)

        monkeypatch.setattr(workload_io, "SERVER_IDENTITY_SQL", "SELECT 1 / 0 AS database")
        assert workload_io.prove_server_identity(connection) == NULL_SERVER
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('statement_timeout') AS timeout, 1 AS alive")
            row = cursor.fetchone()
        assert row == {"timeout": "4321ms", "alive": 1}
    finally:
        connection.rollback()
        connection.close()
