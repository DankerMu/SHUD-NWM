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
                "outcome": "completed",
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


def _replacement_receipt(
    path: Path,
    *,
    run_ids: Sequence[str],
    outcome: str = "completed",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": "nhms.production_replay_replacement.v1",
                "outcome": outcome,
                "rows": [{"run_id": run_id} for run_id in run_ids],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_every_replacement_receipt_contributes_to_one_union(tmp_path: Path) -> None:
    """C2-1: the replay writes four receipts; one invalidation must cover them all.

    Harvesting a single receipt leaves the run ids of the other three phases
    serving tiles rendered from the pre-replay data.
    """

    ifs_phase1 = _replacement_receipt(
        tmp_path / "ifs-replay-phase1.json",
        run_ids=[IN_SCOPE_SOURCE, "fcst_ifs_2026070512_dg_alpha"],
    )
    gfs_phase2 = _replacement_receipt(
        tmp_path / "gfs-replay-phase2.json",
        run_ids=["fcst_gfs_2026071900_dg_alpha", IN_SCOPE_SOURCE],
    )

    assert tool.source_ids_from_replacement_receipts([ifs_phase1, gfs_phase2]) == [
        "fcst_gfs_2026071900_dg_alpha",
        IN_SCOPE_SOURCE,
        "fcst_ifs_2026070512_dg_alpha",
    ]


def test_the_cli_unions_repeated_replacement_receipts_with_explicit_source_ids(tmp_path: Path) -> None:
    args = tool._build_parser().parse_args(
        [
            "--source-id",
            "hydro-national",
            "--from-replacement-receipt",
            str(tmp_path / "a.json"),
            "--from-replacement-receipt",
            str(tmp_path / "b.json"),
            "--window-start",
            "2026-07-05T00:00Z",
            "--window-end",
            "2026-07-28T00:00Z",
            "--receipt-path",
            str(tmp_path / "receipt.json"),
        ]
    )

    assert args.source_ids == ["hydro-national"]
    assert args.from_replacement_receipts == [tmp_path / "a.json", tmp_path / "b.json"]


@pytest.mark.parametrize("outcome", ["in_progress", "halted"])
def test_a_replacement_receipt_that_did_not_complete_is_refused(tmp_path: Path, outcome: str) -> None:
    """A half-finished receipt names only the cycles that finished."""

    receipt_file = _replacement_receipt(
        tmp_path / "partial.json",
        run_ids=[IN_SCOPE_SOURCE],
        outcome=outcome,
    )

    with pytest.raises(TileInvalidationRefused) as excinfo:
        tool.source_ids_from_replacement_receipts([receipt_file])

    assert excinfo.value.reason == "replacement_receipt_not_completed"
    assert excinfo.value.details["outcome"] == outcome


def test_a_foreign_receipt_is_refused_rather_than_read_loosely(tmp_path: Path) -> None:
    receipt_file = tmp_path / "other.json"
    receipt_file.write_text(json.dumps({"schema_version": "something.else", "rows": []}), encoding="utf-8")

    with pytest.raises(TileInvalidationRefused) as excinfo:
        tool.source_ids_from_replacement_receipt(receipt_file)

    assert excinfo.value.reason == "replacement_receipt_schema_unsupported"


def test_a_file_cache_root_that_does_not_exist_refuses_before_reading_the_db(tmp_path: Path) -> None:
    """C-F4: a wrong root makes every per-key probe answer "absent".

    Without the root precheck the run would delete every candidate DB row while
    the real files keep serving stale tiles.
    """

    connection, _cache_dir, receipt_path = _site(tmp_path)

    with pytest.raises(TileInvalidationRefused) as excinfo:
        _run(connection, tmp_path / "typo-cache", receipt_path, execute=True)

    assert excinfo.value.reason == "file_cache_dir_absent"
    assert excinfo.value.details["probe"]["status"] == "absent"
    assert connection.statements == []
    assert connection.cache_keys() == {_key(1), _key(2), _key(3), _key(4)}
    assert not receipt_path.exists()


def test_a_file_cache_root_that_is_not_a_directory_refuses(tmp_path: Path) -> None:
    connection, _cache_dir, receipt_path = _site(tmp_path)
    not_a_dir = tmp_path / "cache-file"
    not_a_dir.write_text("not a directory", encoding="utf-8")

    with pytest.raises(TileInvalidationRefused) as excinfo:
        _run(connection, not_a_dir, receipt_path, execute=True)

    assert excinfo.value.reason == "file_cache_dir_absent"
    assert excinfo.value.details["probe"]["detail"] == "file cache root is not a directory"
    assert connection.statements == []


def test_an_unprobeable_file_cache_root_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three-way: a root that cannot be probed is not evidence of a usable root."""

    connection, cache_dir, receipt_path = _site(tmp_path)

    def _unprobeable(path: Path) -> Any:
        raise PermissionError("permission denied")

    monkeypatch.setattr(tool, "stat_no_follow", _unprobeable)

    with pytest.raises(TileInvalidationRefused) as excinfo:
        _run(connection, cache_dir, receipt_path, execute=True)

    assert excinfo.value.reason == "file_cache_dir_undeterminable"
    assert connection.statements == []
    assert not receipt_path.exists()


def test_a_commit_failure_after_file_unlinks_is_uncertain_never_zero(tmp_path: Path) -> None:
    """C-F3 + round-2 C2-5: a partial mutation may not surface as a bare traceback.

    The commit is its own failure class: once issued, the transaction's fate is
    unknown, so the receipt states ``deleted_rows: null`` instead of claiming a
    zero nobody verified.
    """

    connection, cache_dir, receipt_path = _site(tmp_path)
    unlinked_path = cache_dir / _key(1)[:2] / f"{_key(1)}.pbf"

    def _fail_on_delete() -> None:
        raise RuntimeError("connection reset by peer")

    connection.commit = _fail_on_delete  # type: ignore[method-assign]

    with pytest.raises(tool.TileInvalidationPartialMutation) as excinfo:
        _run(connection, cache_dir, receipt_path, execute=True)

    assert excinfo.value.reason == "db_commit_uncertain_after_file_unlink"
    assert excinfo.value.receipt_written is True
    receipt = excinfo.value.receipt
    assert receipt["outcome"] == "failed_partial_mutation"
    assert receipt["unlinked_file_cache_paths"] == [str(unlinked_path)]
    assert receipt["failure"]["reason"] == "db_commit_uncertain_after_file_unlink"
    # Three-way: a commit that never answered did not "delete 0 rows" -- nobody
    # knows how many it deleted (round-2 C2-5).
    assert receipt["totals"]["deleted_rows"] is None
    # the file really is gone and the row really did survive
    assert not unlinked_path.exists()
    assert _key(1) in connection.cache_keys()
    assert connection.rolled_back is True
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["outcome"] == "failed_partial_mutation"


class _DeleteFailingConnection(_FakeConnection):
    """The DELETE statement itself fails: the transaction never reached commit."""

    def cursor(self) -> _FakeCursor:
        connection = self

        class _Cursor(_FakeCursor):
            def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> None:
                if statement == tool.DELETE_SQL:
                    raise RuntimeError("server closed the connection unexpectedly")
                super().execute(statement, params)

        return _Cursor(connection)


def test_a_delete_failure_after_file_unlinks_reports_zero_deleted_rows(tmp_path: Path) -> None:
    """The complement of the commit case: an un-issued delete really did delete 0."""

    _connection, cache_dir, receipt_path = _site(tmp_path)
    connection = _DeleteFailingConnection(_connection.rows)
    unlinked_path = cache_dir / _key(1)[:2] / f"{_key(1)}.pbf"

    with pytest.raises(tool.TileInvalidationPartialMutation) as excinfo:
        _run(connection, cache_dir, receipt_path, execute=True)

    assert excinfo.value.reason == "db_delete_failed_after_file_unlink"
    receipt = excinfo.value.receipt
    assert receipt["failure"]["reason"] == "db_delete_failed_after_file_unlink"
    assert receipt["totals"]["deleted_rows"] == 0
    assert receipt["unlinked_file_cache_paths"] == [str(unlinked_path)]
    assert not unlinked_path.exists()
    assert connection.committed is False
    assert connection.rolled_back is True
    assert _key(1) in connection.cache_keys()


def test_a_commit_failure_with_no_file_cache_still_leaves_a_receipt(tmp_path: Path) -> None:
    """C3-3: a deployment with no file cache has nothing to unlink.

    ``--no-file-cache`` reaches the commit arm with ``unlinked_paths`` empty, so
    the old ``if not unlinked_paths: raise`` re-raised the driver's exception and
    the DELETE-issued-but-uncommitted state left no receipt at all -- exactly the
    case where the operator most needs to know the DB outcome is unknown.
    """

    connection, _cache_dir, receipt_path = _site(tmp_path)

    def _fail_on_commit() -> None:
        raise RuntimeError("connection reset by peer")

    connection.commit = _fail_on_commit  # type: ignore[method-assign]

    with pytest.raises(tool.TileInvalidationPartialMutation) as excinfo:
        _run(connection, None, receipt_path, execute=True)

    # the reason names what actually happened: no file was unlinked
    assert excinfo.value.reason == "db_commit_uncertain"
    assert excinfo.value.receipt_written is True
    receipt = excinfo.value.receipt
    assert receipt["outcome"] == "failed_partial_mutation"
    assert receipt["unlinked_file_cache_paths"] == []
    assert receipt["failure"]["reason"] == "db_commit_uncertain"
    # the commit was issued and never answered: nobody knows how many rows went
    assert receipt["totals"]["deleted_rows"] is None
    assert connection.rolled_back is True
    on_disk = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert on_disk["failure"]["reason"] == "db_commit_uncertain"
    assert on_disk["totals"]["deleted_rows"] is None


def test_a_delete_failure_with_no_file_cache_still_leaves_a_receipt(tmp_path: Path) -> None:
    """The delete arm's half of C3-3: the scope was issued, the rows survived."""

    _connection, _cache_dir, receipt_path = _site(tmp_path)
    connection = _DeleteFailingConnection(_connection.rows)

    with pytest.raises(tool.TileInvalidationPartialMutation) as excinfo:
        _run(connection, None, receipt_path, execute=True)

    assert excinfo.value.reason == "db_delete_failed"
    receipt = excinfo.value.receipt
    assert receipt["failure"]["reason"] == "db_delete_failed"
    assert receipt["unlinked_file_cache_paths"] == []
    # the DELETE never committed, so zero is a fact rather than a guess
    assert receipt["totals"]["deleted_rows"] == 0
    assert connection.committed is False
    assert connection.rolled_back is True
    assert _key(1) in connection.cache_keys()


class _CandidateQueryFailingConnection(_FakeConnection):
    """The candidate SELECT fails: nothing was staged, let alone mutated."""

    def cursor(self) -> _FakeCursor:
        connection = self

        class _Cursor(_FakeCursor):
            def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> None:
                if statement == tool.CANDIDATE_SQL:
                    raise RuntimeError("server closed the connection unexpectedly")
                super().execute(statement, params)

        return _Cursor(connection)


def test_a_failure_before_any_delete_scope_is_never_dressed_up_as_a_mutation(tmp_path: Path) -> None:
    """The widened guard must not turn a read-side failure into a mutation claim."""

    _connection, _cache_dir, receipt_path = _site(tmp_path)
    connection = _CandidateQueryFailingConnection(_connection.rows)

    with pytest.raises(RuntimeError, match="server closed the connection"):
        _run(connection, None, receipt_path, execute=True)

    assert not receipt_path.exists()
    assert connection.rolled_back is True


def test_a_dry_run_failure_never_claims_a_partial_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dry run stages a delete scope for the report and issues nothing.

    The widened guard reads that scope, so it must also read ``execute``:
    otherwise a read-only rehearsal that trips over a broken probe files a
    ``failed_partial_mutation`` receipt for a mutation that cannot have happened.
    """

    connection, cache_dir, receipt_path = _two_in_scope_site(tmp_path)
    real_probe = tool.probe_file_cache
    calls: list[str] = []

    def _probe_then_fail(root: Path | None, cache_key: str) -> dict[str, Any]:
        calls.append(cache_key)
        if len(calls) > 1:
            raise RuntimeError("stat storm")
        return real_probe(root, cache_key)

    monkeypatch.setattr(tool, "probe_file_cache", _probe_then_fail)

    # the first key is already in the delete scope when the second probe blows
    # up -- but this is a dry run, so nothing was issued and nothing was unlinked
    with pytest.raises(RuntimeError, match="stat storm"):
        _run(connection, cache_dir, receipt_path, execute=False)

    assert not receipt_path.exists()
    assert connection.rolled_back is True
    assert connection.committed is False
    assert all((cache_dir / _key(seed)[:2] / f"{_key(seed)}.pbf").is_file() for seed in (1, 5))


def _two_in_scope_site(tmp_path: Path) -> tuple[_FakeConnection, Path, Path]:
    rows = [
        _row(cache_key=_key(1), source_id=IN_SCOPE_SOURCE, valid_time=WINDOW_START + timedelta(hours=6)),
        _row(cache_key=_key(5), source_id=IN_SCOPE_SOURCE, valid_time=WINDOW_START + timedelta(hours=18)),
    ]
    cache_dir = tmp_path / "tile-file-cache"
    for seed in (1, 5):
        key = _key(seed)
        path = cache_dir / key[:2] / f"{key}.pbf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"tile-{seed}".encode())
    return _FakeConnection(rows), cache_dir, tmp_path / "receipt.json"


def test_an_interrupt_mid_unlink_still_leaves_a_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    """C2-6: ``KeyboardInterrupt`` escapes every ``except Exception``.

    The first entry is already unlinked when the operator hits Ctrl-C; without a
    ``BaseException`` arm the only record of that deletion is a traceback.
    """

    connection, cache_dir, receipt_path = _two_in_scope_site(tmp_path)
    first_path = cache_dir / _key(1)[:2] / f"{_key(1)}.pbf"
    real_unlink = tool._unlink_file_cache
    calls: list[Path] = []

    def _unlink_then_interrupt(path: Path) -> tuple[bool, str | None]:
        calls.append(path)
        if len(calls) > 1:
            raise KeyboardInterrupt
        return real_unlink(path)

    monkeypatch.setattr(tool, "_unlink_file_cache", _unlink_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        _run(connection, cache_dir, receipt_path, execute=True)

    assert not first_path.exists()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "failed_partial_mutation"
    assert receipt["failure"]["reason"] == "interrupted_after_file_unlink"
    assert receipt["unlinked_file_cache_paths"] == [str(first_path)]
    assert receipt["totals"]["deleted_rows"] is None
    assert connection.committed is False
    assert connection.rolled_back is True
    # the same record is echoed for an operator watching the terminal
    assert json.loads(capsys.readouterr().out.strip())["failure"]["reason"] == "interrupted_after_file_unlink"


class _DeleteInterruptedConnection(_FakeConnection):
    """The DELETE is staged into the transaction, then the operator hits Ctrl-C."""

    def cursor(self) -> _FakeCursor:
        connection = self

        class _Cursor(_FakeCursor):
            def execute(self, statement: str, params: Mapping[str, Any] | None = None) -> None:
                if statement == tool.DELETE_SQL:
                    raise KeyboardInterrupt
                super().execute(statement, params)

        return _Cursor(connection)


def test_an_interrupt_with_a_staged_delete_and_no_unlinks_still_leaves_a_receipt(
    tmp_path: Path,
    capsys: Any,
) -> None:
    """C4-2 rider: the no-file-cache exit-4 shape the runbook table names.

    ``--no-file-cache`` means nothing is ever unlinked, so an interrupt with the
    DELETE scope already in the transaction leaves an EMPTY
    ``unlinked_file_cache_paths`` -- and the DB's fate is still unknown.  The
    reason must not claim an unlink, and ``deleted_rows`` must stay ``null``
    rather than being reported as a verified zero.
    """

    rows = _site(tmp_path)[0].rows
    connection = _DeleteInterruptedConnection(rows)
    receipt_path = tmp_path / "interrupted-receipt.json"

    with pytest.raises(KeyboardInterrupt):
        _run(connection, None, receipt_path, execute=True)

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["outcome"] == "failed_partial_mutation"
    assert receipt["failure"]["reason"] == "interrupted_delete_scope_uncertain"
    assert receipt["unlinked_file_cache_paths"] == []
    assert receipt["totals"]["deleted_rows"] is None
    assert connection.committed is False
    assert connection.rolled_back is True
    # the same record is echoed for an operator watching the terminal
    echoed = json.loads(capsys.readouterr().out.strip())
    assert echoed["failure"]["reason"] == "interrupted_delete_scope_uncertain"
    assert echoed["totals"]["deleted_rows"] is None


def test_every_bounded_receipt_list_declares_whether_it_was_cut(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2-3: the runbook treats the unlinked-path list as authoritative."""

    connection, cache_dir, receipt_path = _two_in_scope_site(tmp_path)
    # key 5's cache entry is a directory: an undeterminable probe blocks that key
    blocked_entry = cache_dir / _key(5)[:2] / f"{_key(5)}.pbf"
    blocked_entry.unlink()
    blocked_entry.mkdir()
    monkeypatch.setattr(tool, "MAX_RECEIPT_ENTRIES", 0)

    with pytest.raises(TileInvalidationIncomplete) as excinfo:
        _run(connection, cache_dir, receipt_path, execute=True)

    receipt = excinfo.value.receipt
    assert receipt["totals"]["file_cache_deleted"] == 1
    assert receipt["totals"]["blocked_keys"] == 1
    assert receipt["entries"] == []
    assert receipt["entries_truncated"] is True
    assert receipt["unlinked_file_cache_paths"] == []
    assert receipt["unlinked_file_cache_paths_truncated"] is True
    assert receipt["blocked_cache_keys"] == []
    assert receipt["blocked_cache_keys_truncated"] is True


def test_untruncated_receipt_lists_say_so(tmp_path: Path) -> None:
    connection, cache_dir, receipt_path = _two_in_scope_site(tmp_path)

    receipt = _run(connection, cache_dir, receipt_path, execute=True)

    assert receipt["unlinked_file_cache_paths_truncated"] is False
    assert receipt["blocked_cache_keys_truncated"] is False
    assert receipt["entries_truncated"] is False


def test_a_receipt_write_failure_after_commit_is_a_declared_exit_not_a_traceback(tmp_path: Path) -> None:
    connection, cache_dir, receipt_path = _site(tmp_path)
    # the receipt path is unwritable: its parent is a regular file
    blocked_receipt = tmp_path / "blocked" / "receipt.json"
    blocked_receipt.parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(tool.TileInvalidationPartialMutation) as excinfo:
        _run(connection, cache_dir, blocked_receipt, execute=True)

    assert excinfo.value.reason == "receipt_write_failed_after_commit"
    assert excinfo.value.receipt_written is False
    assert excinfo.value.receipt["unlinked_file_cache_paths"] == [
        str(cache_dir / _key(1)[:2] / f"{_key(1)}.pbf")
    ]
    # the mutation did happen; the receipt travels on the exception instead
    assert connection.committed is True
    assert _key(1) not in connection.cache_keys()


def test_a_receipt_write_failure_without_any_mutation_is_a_refusal(tmp_path: Path) -> None:
    connection, cache_dir, _receipt_path = _site(tmp_path)
    blocked_receipt = tmp_path / "blocked" / "receipt.json"
    blocked_receipt.parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(TileInvalidationRefused) as excinfo:
        _run(connection, cache_dir, blocked_receipt, execute=False)

    assert excinfo.value.reason == "receipt_write_failed"
    assert connection.cache_keys() == {_key(1), _key(2), _key(3), _key(4)}


def test_the_cli_exit_codes_are_declared_for_every_outcome(tmp_path: Path, capsys: Any) -> None:
    """0 / 2 / 3 / 4 are the whole contract; nothing exits on a traceback."""

    connection, cache_dir, receipt_path = _site(tmp_path)

    def _fail_on_commit() -> None:
        raise RuntimeError("connection reset by peer")

    connection.commit = _fail_on_commit  # type: ignore[method-assign]
    monkeypatched_connect = lambda _url: connection  # noqa: E731 - one-line boundary double

    original_connect = tool._connect_default
    tool._connect_default = monkeypatched_connect  # type: ignore[assignment]
    tool.os.environ["DATABASE_URL"] = "postgresql://fake/nhms"
    try:
        code = tool.main(
            [
                "--source-id",
                IN_SCOPE_SOURCE,
                "--window-start",
                "2026-07-05T00:00Z",
                "--window-end",
                "2026-07-21T12:00Z",
                "--receipt-path",
                str(receipt_path),
                "--file-cache-dir",
                str(cache_dir),
                "--execute",
            ]
        )
    finally:
        tool._connect_default = original_connect  # type: ignore[assignment]
        tool.os.environ.pop("DATABASE_URL", None)

    assert code == 4
    captured = capsys.readouterr()
    emitted = json.loads(captured.out.strip().splitlines()[0])
    assert emitted["outcome"] == "failed_partial_mutation"
    assert emitted["unlinked_file_cache_paths"]
    assert json.loads(captured.err.strip().splitlines()[-1])["failure_reason"] == (
        "db_commit_uncertain_after_file_unlink"
    )
    assert "4" in tool.__doc__ and "failed AFTER" in tool.__doc__


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
