"""Tests for the scoped replay tile invalidation tool (#1164 change 2, tasks.md 4.2).

The database double below is a boundary double only: it answers the module's own
SQL constants and applies the ``(source_id, valid_time)`` predicate the tool
sends, so "which rows survive" is asserted on real state rather than on a call
log.  Any change to the SQL text fails ``test_the_double_answers_the_module_sql``
so the double can never drift silently away from the statement under test.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_invalidate_tiles as tool
from scripts.node27_invalidate_tiles import (
    TileInvalidationIncomplete,
    TileInvalidationRefused,
    invalidate_tiles,
)

WINDOW_START = datetime(2026, 7, 5, 0, tzinfo=UTC)
WINDOW_END = datetime(2026, 7, 21, 12, tzinfo=UTC)
IN_SCOPE_SOURCE = "fcst_ifs_2026070500_dg_alpha"
OTHER_SOURCE = "fcst_ifs_2026070500_dg_outside"


def _key(seed: int) -> str:
    return f"{seed:064x}"


def _row(
    *,
    cache_key: str,
    source_id: str,
    valid_time: datetime | None,
    layer_id: str = "hydro:q-down",
) -> dict[str, Any]:
    return {
        "cache_key": cache_key,
        "layer_id": layer_id,
        "z": 7,
        "x": 101,
        "y": 52,
        "source_id": source_id,
        "valid_time": valid_time,
    }


class _FakeCursor:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> None:
        self.connection.statements.append((statement, dict(params or {})))
        if statement.startswith("SET "):
            self.connection.result = None
            return
        if statement == tool.TABLE_PRESENT_SQL:
            self.connection.result = (self.connection.table_name,)
            return
        if statement == tool.CANDIDATE_SQL:
            assert params is not None
            sources = set(params["source_ids"])
            start = params["window_start"]
            end = params["window_end"]
            self.connection.rows_result = [
                dict(row)
                for row in sorted(self.connection.rows, key=lambda row: row["cache_key"])
                if row["source_id"] in sources
                and row["valid_time"] is not None
                and start <= row["valid_time"] <= end
            ]
            return
        if statement == tool.DELETE_SQL:
            assert params is not None
            keys = set(params["cache_keys"])
            before = len(self.connection.rows)
            self.connection.pending = [row for row in self.connection.rows if row["cache_key"] not in keys]
            self.rowcount = before - len(self.connection.pending)
            self.connection.rowcount = self.rowcount
            return
        raise AssertionError(f"unexpected statement: {statement}")

    def fetchone(self) -> Any:
        return self.connection.result

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self.connection.rows_result)

    @property
    def rowcount(self) -> int:
        return self.connection.rowcount

    @rowcount.setter
    def rowcount(self, value: int) -> None:
        self.connection.rowcount = value


class _FakeConnection:
    def __init__(self, rows: Sequence[Mapping[str, Any]], *, table_name: str | None = "map.tile_cache") -> None:
        self.rows: list[dict[str, Any]] = [dict(row) for row in rows]
        self.pending: list[dict[str, Any]] | None = None
        self.rows_result: list[dict[str, Any]] = []
        self.result: Any = None
        self.rowcount = 0
        self.table_name = table_name
        self.statements: list[tuple[str, dict[str, Any]]] = []
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.committed = True
        if self.pending is not None:
            self.rows = self.pending
            self.pending = None

    def rollback(self) -> None:
        self.rolled_back = True
        self.pending = None

    def close(self) -> None:
        self.closed = True

    def cache_keys(self) -> set[str]:
        return {row["cache_key"] for row in self.rows}


def _site(tmp_path: Path) -> tuple[_FakeConnection, Path, Path]:
    """Three rows: in scope, same source but outside the window, other source."""

    rows = [
        _row(cache_key=_key(1), source_id=IN_SCOPE_SOURCE, valid_time=WINDOW_START + timedelta(hours=6)),
        _row(cache_key=_key(2), source_id=IN_SCOPE_SOURCE, valid_time=WINDOW_END + timedelta(hours=12)),
        _row(cache_key=_key(3), source_id=OTHER_SOURCE, valid_time=WINDOW_START + timedelta(hours=6)),
        _row(cache_key=_key(4), source_id=IN_SCOPE_SOURCE, valid_time=None, layer_id="river-network"),
    ]
    connection = _FakeConnection(rows)
    cache_dir = tmp_path / "tile-file-cache"
    for seed in (1, 2, 3, 4):
        key = _key(seed)
        path = cache_dir / key[:2] / f"{key}.pbf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"tile-{seed}".encode())
    return connection, cache_dir, tmp_path / "receipt.json"


def _run(
    connection: _FakeConnection,
    cache_dir: Path | None,
    receipt_path: Path,
    *,
    execute: bool,
    source_ids: Sequence[str] = (IN_SCOPE_SOURCE,),
) -> dict[str, Any]:
    return invalidate_tiles(
        source_ids=source_ids,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        receipt_path=receipt_path,
        file_cache_dir=cache_dir,
        execute=execute,
        database_url="postgresql://fake/nhms",
        connect=lambda _url: connection,
    )


# ---------------------------------------------------------------------------


def test_dry_run_deletes_nothing_in_either_tier(tmp_path: Path) -> None:
    connection, cache_dir, receipt_path = _site(tmp_path)

    receipt = _run(connection, cache_dir, receipt_path, execute=False)

    assert receipt["executed"] is False
    assert receipt["outcome"] == "completed"
    assert receipt["totals"]["candidate_rows"] == 1
    assert receipt["totals"]["deleted_rows"] == 0
    assert receipt["totals"]["file_cache_deleted"] == 0
    assert connection.cache_keys() == {_key(1), _key(2), _key(3), _key(4)}
    assert connection.committed is False
    assert all(
        (cache_dir / _key(seed)[:2] / f"{_key(seed)}.pbf").is_file() for seed in (1, 2, 3, 4)
    )
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["totals"] == receipt["totals"]


def test_execute_deletes_only_the_in_scope_row_and_its_file_entry(tmp_path: Path) -> None:
    connection, cache_dir, receipt_path = _site(tmp_path)

    receipt = _run(connection, cache_dir, receipt_path, execute=True)

    assert receipt["outcome"] == "completed"
    assert receipt["totals"] == {
        "candidate_rows": 1,
        "deleted_rows": 1,
        "file_cache_deleted": 1,
        "file_cache_absent": 0,
        "file_cache_present": 1,
        "file_cache_undeterminable": 0,
        "blocked_keys": 0,
    }
    assert connection.committed is True
    # same-source row outside the window and the other-source row both survive
    assert connection.cache_keys() == {_key(2), _key(3), _key(4)}
    assert not (cache_dir / _key(1)[:2] / f"{_key(1)}.pbf").exists()
    for seed in (2, 3, 4):
        assert (cache_dir / _key(seed)[:2] / f"{_key(seed)}.pbf").is_file()


def test_same_source_rows_outside_the_window_are_never_candidates(tmp_path: Path) -> None:
    connection, cache_dir, receipt_path = _site(tmp_path)

    receipt = _run(connection, cache_dir, receipt_path, execute=True)

    scoped = {entry["cache_key"] for entry in receipt["entries"]}
    assert scoped == {_key(1)}
    assert _key(2) not in scoped
    # a NULL valid_time row (river-network layer) is out of scope by construction
    assert _key(4) not in scoped


def test_other_source_rows_are_never_candidates(tmp_path: Path) -> None:
    connection, cache_dir, receipt_path = _site(tmp_path)

    receipt = _run(connection, cache_dir, receipt_path, execute=True)

    assert all(entry["source_id"] == IN_SCOPE_SOURCE for entry in receipt["entries"])
    assert _key(3) in connection.cache_keys()


def test_window_bounds_are_inclusive(tmp_path: Path) -> None:
    rows = [
        _row(cache_key=_key(10), source_id=IN_SCOPE_SOURCE, valid_time=WINDOW_START),
        _row(cache_key=_key(11), source_id=IN_SCOPE_SOURCE, valid_time=WINDOW_END),
        _row(cache_key=_key(12), source_id=IN_SCOPE_SOURCE, valid_time=WINDOW_START - timedelta(seconds=1)),
        _row(cache_key=_key(13), source_id=IN_SCOPE_SOURCE, valid_time=WINDOW_END + timedelta(seconds=1)),
    ]
    connection = _FakeConnection(rows)

    receipt = _run(connection, None, tmp_path / "receipt.json", execute=True)

    assert {entry["cache_key"] for entry in receipt["entries"]} == {_key(10), _key(11)}
    assert connection.cache_keys() == {_key(12), _key(13)}


def test_an_unreadable_file_cache_entry_blocks_its_row_instead_of_stranding_it(tmp_path: Path) -> None:
    """A key whose file leg is undeterminable keeps BOTH tiers (re-runnable)."""

    connection, cache_dir, receipt_path = _site(tmp_path)
    blocked_key = _key(1)
    entry_path = cache_dir / blocked_key[:2] / f"{blocked_key}.pbf"
    entry_path.unlink()
    entry_path.mkdir()  # a directory where a tile file belongs: not a completed negative

    with pytest.raises(TileInvalidationIncomplete) as excinfo:
        _run(connection, cache_dir, receipt_path, execute=True)

    receipt = excinfo.value.receipt
    assert receipt["outcome"] == "incomplete"
    assert receipt["blocked_cache_keys"] == [blocked_key]
    assert receipt["totals"]["deleted_rows"] == 0
    assert blocked_key in connection.cache_keys()
    assert entry_path.is_dir()
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["outcome"] == "incomplete"


def test_a_missing_file_cache_entry_is_not_an_obstacle(tmp_path: Path) -> None:
    connection, cache_dir, receipt_path = _site(tmp_path)
    (cache_dir / _key(1)[:2] / f"{_key(1)}.pbf").unlink()

    receipt = _run(connection, cache_dir, receipt_path, execute=True)

    assert receipt["outcome"] == "completed"
    assert receipt["totals"]["file_cache_absent"] == 1
    assert receipt["totals"]["deleted_rows"] == 1
    assert _key(1) not in connection.cache_keys()


def test_an_absent_tile_cache_table_refuses_without_touching_anything(tmp_path: Path) -> None:
    connection, cache_dir, receipt_path = _site(tmp_path)
    connection.table_name = None

    with pytest.raises(TileInvalidationRefused) as excinfo:
        _run(connection, cache_dir, receipt_path, execute=True)

    assert excinfo.value.reason == "tile_cache_table_absent"
    assert connection.rolled_back is True
    assert connection.committed is False
    assert not receipt_path.exists()


def test_an_inverted_window_refuses_before_connecting(tmp_path: Path) -> None:
    connection, cache_dir, receipt_path = _site(tmp_path)

    with pytest.raises(TileInvalidationRefused) as excinfo:
        invalidate_tiles(
            source_ids=[IN_SCOPE_SOURCE],
            window_start=WINDOW_END,
            window_end=WINDOW_START,
            receipt_path=receipt_path,
            file_cache_dir=cache_dir,
            execute=True,
            database_url="postgresql://fake/nhms",
            connect=lambda _url: connection,
        )

    assert excinfo.value.reason == "window_inverted"
    assert connection.statements == []
    assert not receipt_path.exists()


def test_an_empty_scope_refuses(tmp_path: Path) -> None:
    connection, cache_dir, receipt_path = _site(tmp_path)

    with pytest.raises(TileInvalidationRefused) as excinfo:
        _run(connection, cache_dir, receipt_path, execute=True, source_ids=["  "])

    assert excinfo.value.reason == "source_ids_missing"
    assert connection.statements == []


def test_source_ids_are_harvested_from_a_replacement_receipt(tmp_path: Path) -> None:
    receipt_file = tmp_path / "replacement.json"
    receipt_file.write_text(
        json.dumps(
            {
                "schema_version": "nhms.production_replay_replacement.v1",
                "rows": [
                    {"run_id": IN_SCOPE_SOURCE},
                    {"run_id": IN_SCOPE_SOURCE},
                    {"run_id": "fcst_ifs_2026070512_dg_alpha"},
                ],
            }
        ),
        encoding="utf-8",
    )

    assert tool.source_ids_from_replacement_receipt(receipt_file) == [
        IN_SCOPE_SOURCE,
        "fcst_ifs_2026070512_dg_alpha",
    ]


def test_a_foreign_receipt_is_refused_rather_than_read_loosely(tmp_path: Path) -> None:
    receipt_file = tmp_path / "other.json"
    receipt_file.write_text(json.dumps({"schema_version": "something.else", "rows": []}), encoding="utf-8")

    with pytest.raises(TileInvalidationRefused) as excinfo:
        tool.source_ids_from_replacement_receipt(receipt_file)

    assert excinfo.value.reason == "replacement_receipt_schema_unsupported"


def test_an_unset_file_cache_dir_is_refused_unless_declared_absent() -> None:
    args = tool._build_parser().parse_args(
        ["--source-id", IN_SCOPE_SOURCE, "--window-start", "2026-07-05T00:00Z", "--window-end", "2026-07-21T12:00Z",
         "--receipt-path", "/tmp/receipt.json"]
    )
    args.file_cache_dir = None

    with pytest.raises(TileInvalidationRefused) as excinfo:
        tool._resolve_file_cache_dir(args)
    assert excinfo.value.reason == "file_cache_dir_unset"

    args.no_file_cache = True
    assert tool._resolve_file_cache_dir(args) is None


def test_the_double_answers_the_module_sql() -> None:
    """Guard: the double is written against these exact statements."""

    assert "source_id = ANY(%(source_ids)s)" in tool.CANDIDATE_SQL
    assert "valid_time IS NOT NULL" in tool.CANDIDATE_SQL
    assert "valid_time >= %(window_start)s" in tool.CANDIDATE_SQL
    assert "valid_time <= %(window_end)s" in tool.CANDIDATE_SQL
    assert tool.DELETE_SQL == "DELETE FROM map.tile_cache WHERE cache_key = ANY(%(cache_keys)s)"


def test_the_file_cache_layout_matches_the_serving_path(tmp_path: Path) -> None:
    """The tool must delete exactly what services/tiles/mvt.py would read."""

    from services.tiles import mvt

    key = _key(9)
    monkey_root = tmp_path / "cache"
    previous = tool.os.environ.get(mvt.MVT_FILE_CACHE_DIR_ENV)
    tool.os.environ[mvt.MVT_FILE_CACHE_DIR_ENV] = str(monkey_root)
    try:
        assert tool.file_cache_path(monkey_root, key) == mvt._file_cache_path(key)
        assert tool.file_cache_path(monkey_root, "not-a-key") is None
        assert mvt._file_cache_path("not-a-key") is None
    finally:
        if previous is None:
            tool.os.environ.pop(mvt.MVT_FILE_CACHE_DIR_ENV, None)
        else:
            tool.os.environ[mvt.MVT_FILE_CACHE_DIR_ENV] = previous
