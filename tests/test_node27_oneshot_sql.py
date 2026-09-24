"""Unit seams of the node-27 one-shot SQL runner (#1729 / #1480), no database.

The runner's database behaviour is exercised end to end by
``tests/test_node27_1729_evidence_basin_delete_integration.py`` and
``tests/test_node27_1480_seed_provenance_backfill_integration.py``; this file
pins the marker grammar every checked-in script relies on and the refusals
that fire before any connection is opened.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.ops.node27_oneshot_sql import Segment, main, parse_segments, run_sql_file

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


def test_the_1729_rollback_reads_exactly_what_the_backup_writes_parents_first() -> None:
    assert _copies(_OPS / "node27_1729_delete_evidence_basin_backup.sql", "copy-out") == list(_1729)
    assert _copies(_OPS / "node27_1729_delete_evidence_basin_rollback.sql", "copy-in") == list(_1729)
    assert _copies(_OPS / "node27_1729_delete_evidence_basin.sql", "copy-out") == []


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


def test_cli_refuses_an_unset_dsn_variable(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("NHMS_IT_ONESHOT_DSN", raising=False)
    assert main(["whatever.sql", "--dsn-env", "NHMS_IT_ONESHOT_DSN"]) == 2
    assert "NHMS_IT_ONESHOT_DSN is unset" in capsys.readouterr().err
