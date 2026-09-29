"""Unit seams of the node-27 one-shot SQL runner (#1729 / #1480), no database.

The runner's database behaviour is exercised end to end by
``tests/test_node27_1729_evidence_basin_delete_integration.py`` and
``tests/test_node27_1480_seed_provenance_backfill_integration.py``; this file
pins the marker grammar every checked-in script relies on, the refusals that
fire before any connection is opened, the #1729 and #2621 column contracts
shared by each delete and its rollback, and -- against a fake driver at the
psycopg2 boundary -- the backend attribution and the fsync-before-COMMIT order.
``tests/test_node27_2621_rename_leftovers_delete_integration.py`` executes the
#2621 pair.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import psycopg2
import pytest

from scripts.ops.node27_oneshot_sql import (
    Segment,
    _code_lines,
    declared_settings,
    main,
    parse_segments,
    run_sql_file,
)

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


_2621 = ("core.basin", "core.basin_version", "core.river_network_version", "core.mesh_version",
         "core.model_instance", "met.met_station", "core.river_segment", "core.river_segment_crosswalk")  # fmt: skip
_2621_DELETE = _OPS / "node27_2621_delete_rename_leftovers.sql"
_2621_ROLLBACK = _OPS / "node27_2621_delete_rename_leftovers_rollback.sql"


def _guard_columns_with_generated(guard: str) -> dict[str, tuple[list[str], list[str]]]:
    """table -> (restored columns, pinned GENERATED columns) of a #2621-shape guard."""
    rows = re.findall(r"\('([a-z_.]+)', '\{([a-z_,]*)\}'::text\[\], '\{([a-z_,]*)\}'::text\[\]\)", guard)
    return {table: (cols.split(","), generated.split(",") if generated else []) for table, cols, generated in rows}


def test_the_2621_rollback_reads_exactly_what_the_delete_writes_parents_first() -> None:
    assert _copies(_2621_DELETE, "copy-out") == list(_2621)
    assert _copies(_2621_ROLLBACK, "copy-in") == list(_2621)


def test_the_2621_column_lists_and_guard_cannot_drift_between_delete_and_rollback() -> None:
    """Byte-identical guard in both files; each COPY names exactly the guard's restored columns, in order.

    ``core.river_segment.stream_type`` is GENERATED ALWAYS ... STORED on node-27:
    the guard pins it as the one generated column, the COPYs leave it out and
    COPY FROM recomputes it. The identity keys (``*_key``) are plain listed
    columns, so the restore keeps their values.
    """
    guards = {path.name: _column_guard(path) for path in (_2621_DELETE, _2621_ROLLBACK)}
    assert len(set(guards.values())) == 1, sorted(guards)
    guard = _guard_columns_with_generated(guards[_2621_DELETE.name])
    assert list(guard) == list(_2621)
    assert {table: generated for table, (_, generated) in guard.items() if generated} == {
        "core.river_segment": ["stream_type"]
    }
    restored = {table: columns for table, (columns, _) in guard.items()}
    identity_keys = {
        "core.basin_version": "basin_version_key",
        "core.river_network_version": "river_network_version_key",
        "met.met_station": "station_key",
        "core.river_segment": "river_segment_key",
        "core.river_segment_crosswalk": "crosswalk_id",
    }
    for table, key in identity_keys.items():
        assert key in restored[table], (table, key)
    assert _copy_columns(_2621_DELETE, "copy-out") == restored
    assert _copy_columns(_2621_ROLLBACK, "copy-in") == restored


def test_the_2621_delete_locks_then_backs_up_then_deletes_with_replica_only_around_two_deletes() -> None:
    segments = parse_segments(_2621_DELETE.read_text(encoding="utf-8"))
    kinds = ["copy" if segment.copy else "sql" for segment in segments]
    first_copy, last_copy = kinds.index("copy"), len(kinds) - 1 - kinds[::-1].index("copy")
    assert kinds[first_copy : last_copy + 1] == ["copy"] * len(_2621)

    def code(chunk: list[Segment]) -> str:
        return "\n".join(line for segment in chunk for line in _code_lines(segment.sql))

    before, after = code(segments[:first_copy]), code(segments[last_copy + 1 :])
    assert before.count("FOR UPDATE") == len(_2621) and "DELETE FROM" not in before
    assert after.count("DELETE FROM") == len(_2621) and "FOR UPDATE" not in after
    set_replica = "set_config('session_replication_role', 'replica', true)"
    assert before.count(set_replica) == 1, "before the delete: only the up-front permission trial"
    assert after.count(set_replica) == 2
    # Each replica window: set, one DELETE, back to origin, asserted -- nothing else in between.
    windows = re.findall(r"'replica', true\);\n(.*?)\n\s*PERFORM set_config\('session_replication_role', 'origin'",
                         after, re.DOTALL)  # fmt: skip
    assert [re.findall(r"DELETE FROM ([a-z_.]+)", window) for window in windows] == [
        ["core.river_segment"],
        ["met.met_station"],
    ]
    back_to_origin = (
        "PERFORM set_config('session_replication_role', 'origin', true);\n"
        "IF current_setting('session_replication_role') <> 'origin' THEN"
    )
    assert after.count(back_to_origin) == 2


def test_the_2621_hypertable_probes_use_only_array_equality_each_under_its_own_timeout() -> None:
    text = _2621_DELETE.read_text(encoding="utf-8")
    probes = re.findall(r"FROM (hydro\.river_timeseries|met\.forcing_station_timeseries(?:_legacy)?)\s+WHERE ([^;]*);",
                        text)  # fmt: skip
    assert sorted(relation for relation, _ in probes) == [
        "hydro.river_timeseries",
        "met.forcing_station_timeseries",
        "met.forcing_station_timeseries_legacy",
    ]
    for relation, predicate in probes:
        assert re.fullmatch(r"[a-z_]+ = ANY \(v_[a-z_]+\)", predicate.strip()), (relation, predicate)
    # A statement_timeout is armed when a top-level statement starts, so each probe
    # is its own statement, right after the statement that sets its budget.
    budget = (
        "SELECT set_config('statement_timeout', "
        "coalesce(nullif(current_setting('nhms.probe_timeout_s', true), ''), '3600') || 's', true);"
    )
    assert text.count(budget) == 3


def test_2621_declares_exactly_its_two_settings_and_its_rollback_none() -> None:
    assert declared_settings(_2621_DELETE.read_text(encoding="utf-8")) == frozenset(
        {"nhms.manifest_basins", "nhms.probe_timeout_s"}
    )
    assert declared_settings(_2621_ROLLBACK.read_text(encoding="utf-8")) == frozenset()


def test_settings_declarations_bind_the_runner_only_where_present(tmp_path: Path) -> None:
    assert declared_settings("SELECT 1;\n") is None
    assert declared_settings("-- @settings\nSELECT 1;\n") == frozenset()
    assert declared_settings("-- @settings a.b c.d\n") == frozenset({"a.b", "c.d"})
    with pytest.raises(ValueError, match="more than one"):
        declared_settings("-- @settings a.b\n-- @settings c.d\n")
    with pytest.raises(ValueError, match="malformed"):
        declared_settings("-- @settings statement_timeout\n")

    declared = tmp_path / "declared.sql"
    declared.write_text("-- @settings a.b\nSELECT 1;\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"--set c\.d: not declared by .*declared\.sql \(@settings a\.b\)"):
        run_sql_file(declared, database_url="postgresql://127.0.0.1:1/none", settings={"a.b": "1", "c.d": "2"})
    none = tmp_path / "none.sql"
    none.write_text("-- @settings\nSELECT 1;\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"--set a\.b: not declared by .*none\.sql \(@settings -\)"):
        run_sql_file(none, database_url="postgresql://127.0.0.1:1/none", settings={"a.b": "1"})


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
    scripts = sorted(_OPS.glob("node27_*.sql"))
    assert len(scripts) == 7, scripts
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
