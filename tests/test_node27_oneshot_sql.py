"""Unit seams of the node-27 one-shot SQL runner (#1729 / #1480), no database.

The runner's database behaviour is exercised end to end by
``tests/test_node27_1729_evidence_basin_delete_integration.py`` and
``tests/test_node27_1480_seed_provenance_backfill_integration.py``; this file
pins the marker grammar every checked-in script relies on, the refusals that
fire before any connection is opened, the #1729 column contract shared by the
delete / precheck / rollback scripts, and -- against a fake driver at the
psycopg2 boundary -- the backend attribution and the fsync-before-COMMIT order.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import psycopg2
import pytest

from scripts.ops.node27_oneshot_sql import Segment, _code_lines, main, parse_segments, run_sql_file

_OPS = Path(__file__).resolve().parents[1] / "scripts" / "ops"
_1729 = ("core.basin", "core.basin_version", "core.river_network_version", "core.mesh_version",
         "core.model_instance", "met.met_station")  # fmt: skip


def _copies(path: Path, kind: str) -> list[str]:
    return [segment.name for segment in parse_segments(path.read_text(encoding="utf-8")) if segment.copy == kind]


def test_a_marker_binds_exactly_the_next_copy_statement() -> None:
    text = "SET LOCAL a.b = 1;\n-- only a comment\n-- @copy-out t1\nCOPY (\n  SELECT 1\n) TO STDOUT;\nSELECT 2;\n"
    assert parse_segments(text) == [
        Segment("SET LOCAL a.b = 1;\n-- only a comment"),
        Segment("COPY (\n  SELECT 1\n) TO STDOUT", "copy-out", "t1"),
        Segment("SELECT 2;"),
    ]


def test_comment_only_chunks_are_not_statements() -> None:
    assert parse_segments("-- header\n\n-- @copy-in t\nCOPY t (a) FROM STDIN;\n-- trailer\n") == [
        Segment("COPY t (a) FROM STDIN", "copy-in", "t"),
    ]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("-- @copy-out t\nSELECT 1;\n", "must be `COPY ... TO STDOUT;`"),
        ("-- @copy-in t\nCOPY t (a) TO STDOUT;\n", "must be `COPY ... FROM STDIN;`"),
        ("-- @copy-out t\nCOPY (SELECT 1) TO STDOUT\n", "no statement ending in ';'"),
        ("-- @copy-out t\nCOPY (SELECT 1) TO STDOUT;\n-- @copy-out t\nCOPY (SELECT 2) TO STDOUT;\n", "duplicate"),
    ],
)
def test_malformed_markers_are_refused(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_segments(text)


_1729_BACKUP = _OPS / "node27_1729_delete_evidence_basin_backup.sql"
_1729_DELETE = _OPS / "node27_1729_delete_evidence_basin.sql"
_1729_ROLLBACK = _OPS / "node27_1729_delete_evidence_basin_rollback.sql"


def test_the_1729_rollback_reads_exactly_what_the_delete_writes_parents_first() -> None:
    # The delete run writes the authoritative backup; the precheck writes the same names.
    assert _copies(_1729_DELETE, "copy-out") == list(_1729)
    assert _copies(_1729_BACKUP, "copy-out") == list(_1729)
    assert _copies(_1729_ROLLBACK, "copy-in") == list(_1729)


def _column_guard(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index("-- Column-set guard:")
    return text[start : text.index("$$;", start) + len("$$;")]


def _guard_columns(guard: str) -> dict[str, list[str]]:
    return {
        table: columns.split(",")
        for table, columns in re.findall(r"\('([a-z_.]+)', '\{([a-z_,]+)\}'::text\[\]\)", guard)
    }


def _copy_columns(path: Path, kind: str) -> dict[str, list[str]]:
    """NAME -> the explicit column list of its COPY, in order."""
    pattern = r"SELECT\s+(.*?)\s+FROM" if kind == "copy-out" else r"\((.*?)\)\s+FROM STDIN"
    columns: dict[str, list[str]] = {}
    for segment in parse_segments(path.read_text(encoding="utf-8")):
        if segment.copy == kind:
            listed = re.search(pattern, segment.sql, re.DOTALL)
            assert listed is not None, (path.name, segment.name)
            columns[segment.name] = [column.strip() for column in listed.group(1).split(",")]
    return columns


def test_the_1729_column_lists_and_guard_cannot_drift_between_the_three_scripts() -> None:
    """One column contract: delete (authoritative backup), precheck and rollback.

    The guard block is byte-identical in all three files, and every COPY names
    exactly the guard's columns for its table, in the same order everywhere --
    so a column added to one list but not the others (lost on restore, or a
    shifted COPY FROM) fails here before any database sees it.
    """
    guards = {path.name: _column_guard(path) for path in (_1729_DELETE, _1729_BACKUP, _1729_ROLLBACK)}
    assert len(set(guards.values())) == 1, sorted(guards)
    guard_columns = _guard_columns(guards[_1729_DELETE.name])
    assert list(guard_columns) == list(_1729)
    delete_columns = _copy_columns(_1729_DELETE, "copy-out")
    assert delete_columns == guard_columns
    assert _copy_columns(_1729_BACKUP, "copy-out") == delete_columns
    assert _copy_columns(_1729_ROLLBACK, "copy-in") == delete_columns


def test_the_1729_delete_backs_up_after_its_locks_and_before_its_deletes() -> None:
    """Six row locks, then the six COPYs, then the six DELETEs -- code only, comments stripped."""
    segments = parse_segments(_1729_DELETE.read_text(encoding="utf-8"))
    kinds = ["copy" if segment.copy else "sql" for segment in segments]
    first_copy, last_copy = kinds.index("copy"), len(kinds) - 1 - kinds[::-1].index("copy")
    assert kinds[first_copy : last_copy + 1] == ["copy"] * 6

    def code(chunk: list[Segment]) -> str:
        return "\n".join(line for segment in chunk for line in _code_lines(segment.sql))

    before, after = code(segments[:first_copy]), code(segments[last_copy + 1 :])
    assert before.count("FOR UPDATE") == 6 and "DELETE FROM" not in before
    assert after.count("DELETE FROM") == 6 and "FOR UPDATE" not in after


def test_the_1480_rollback_reads_exactly_what_the_backfill_writes() -> None:
    assert _copies(_OPS / "node27_1480_backfill_seed_station_provenance.sql", "copy-out") == [
        "met_station_properties_json"
    ]
    assert _copies(_OPS / "node27_1480_backfill_seed_station_provenance_rollback.sql", "copy-in") == [
        "met_station_properties_json"
    ]


_TRANSACTION_CONTROL = re.compile(r"(?im)^\s*(BEGIN|COMMIT|ROLLBACK|START\s+TRANSACTION)\b[^;\n]*;")


def test_scripts_carry_no_transaction_control() -> None:
    # The runner owns the one transaction; a COMMIT inside a file would defeat the dry-run.
    scripts = sorted(_OPS.glob("node27_1[47]*.sql"))
    assert len(scripts) == 5, scripts
    for path in scripts:
        assert _TRANSACTION_CONTROL.search(path.read_text(encoding="utf-8")) is None, path.name
    assert _TRANSACTION_CONTROL.search("SELECT 1;\nCOMMIT;\n") is not None


def test_refusals_before_any_connection(tmp_path: Path) -> None:
    script = tmp_path / "x.sql"
    script.write_text("-- @copy-out t\nCOPY (SELECT 1) TO STDOUT;\n", encoding="utf-8")
    with pytest.raises(ValueError, match="copy directory is required"):
        run_sql_file(script, database_url="postgresql://127.0.0.1:1/none")
    plain = tmp_path / "y.sql"
    plain.write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(ValueError, match="custom `prefix.name` settings"):
        run_sql_file(plain, database_url="postgresql://127.0.0.1:1/none", settings={"statement_timeout": "0"})


class _FakeCursor:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._events.append("execute")

    def copy_expert(self, sql: str, handle: Any) -> None:
        handle.write("row\n")
        self._events.append("copy")


class _FakeConnection:
    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.notices: list[str] = []
        self.autocommit = True

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._events)

    def commit(self) -> None:
        self._events.append("commit")

    def rollback(self) -> None:
        self._events.append("rollback")

    def close(self) -> None:
        self._events.append("close")


def test_the_backend_is_attributed_and_copy_files_are_fsynced_before_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    connects: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def connect(*args: Any, **kwargs: Any) -> _FakeConnection:
        connects.append((args, kwargs))
        return _FakeConnection(events)

    real_fsync = os.fsync

    def fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    monkeypatch.setattr(psycopg2, "connect", connect)
    monkeypatch.setattr(os, "fsync", fsync)
    script = tmp_path / "x.sql"
    script.write_text(
        "SELECT 1;\n-- @copy-out a\nCOPY (SELECT 1) TO STDOUT;\n-- @copy-out b\nCOPY (SELECT 2) TO STDOUT;\n"
    )
    copy_dir = tmp_path / "copies"
    copy_dir.mkdir()

    report = run_sql_file(script, database_url="postgresql://u:p@127.0.0.1:1/nhms", copy_dir=copy_dir, apply=True)

    assert report.committed
    # fallback_, so an operator's explicit ?application_name= in the DSN still wins.
    assert connects == [(("postgresql://u:p@127.0.0.1:1/nhms",), {"fallback_application_name": "nhms-oneshot-sql"})]
    # Each file right after its COPY, then the directory, all before COMMIT.
    assert events == ["execute", "copy", "fsync", "copy", "fsync", "fsync", "commit", "close"]
    assert (copy_dir / "a.copy").read_text(encoding="utf-8") == "row\n"


def test_cli_refuses_an_unset_dsn_variable(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("NHMS_IT_ONESHOT_DSN", raising=False)
    assert main(["whatever.sql", "--dsn-env", "NHMS_IT_ONESHOT_DSN"]) == 2
    assert "NHMS_IT_ONESHOT_DSN is unset" in capsys.readouterr().err
