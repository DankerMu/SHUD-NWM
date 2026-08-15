"""Unit tests for the node-27 river identity backfill runner (issue #1339).

The database is faked at the CURSOR boundary — the same seam
``tests/test_node27_timeseries_compression.py`` uses — so batch planning,
re-entrancy, the duration wall, the two chunk-level skips, the fail-closed
shortfall split, receipt schema conformance and the flock mutex are all
exercised without a live TimescaleDB.

What this module deliberately does NOT claim: anything about TimescaleDB
compression semantics. Those are pinned by the integration module against
node-27's throwaway database, because CI runs ``pg15-latest`` and the
production oracle is 2.10.2.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from scripts import node27_river_identity_backfill as backfill

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_PATH = _ROOT / "schemas/river_identity_backfill_receipt.schema.json"
_RUNNER_SOURCE_PATH = _ROOT / "scripts/node27_river_identity_backfill.py"
_MIGRATION_PATH = _ROOT / "db/migrations/000050_river_identity_normalization.sql"

_NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
_LAG = 604_800  # 7 days, the compression lane's default


def _load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "enforce": False,
        "probe": False,
        "final_sweep": False,
        "receipt_path": None,
        "lock_path": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _env(tmp_path: Path, **overrides: str | None) -> dict[str, str]:
    env: dict[str, str] = {
        "DATABASE_URL": "postgresql://user:secretpw@127.0.0.1:55432/nhms",
        "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": str(_LAG),
        "NODE27_RIVER_IDENTITY_BACKFILL_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_RIVER_IDENTITY_BACKFILL_LOCK_PATH": str(tmp_path / "runner.lock"),
    }
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _config(tmp_path: Path, **overrides: Any) -> backfill.BackfillConfig:
    base = backfill.config_from_args(
        _args(**{k: v for k, v in overrides.items() if k in {"enforce", "probe", "final_sweep"}}),
        _env(tmp_path),
    )
    replacements = {k: v for k, v in overrides.items() if k not in {"enforce", "probe", "final_sweep"}}
    if not replacements:
        return base
    import dataclasses

    return dataclasses.replace(base, **replacements)


def _chunk(
    name: str,
    *,
    days_old: float,
    compressed: bool = False,
    pages: int = 10,
    total_bytes: int = 8192 * 10,
) -> backfill.ChunkRow:
    end = _NOW - timedelta(days=days_old)
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


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _QueryCancelled(Exception):
    """Stand-in for psycopg2's QueryCanceled (SQLSTATE 57014)."""

    pgcode = "57014"

    def __str__(self) -> str:  # pragma: no cover - message shape only
        return "canceling statement due to statement timeout"


class _FakeCursor:
    """Cursor that answers by SQL-substring match and records every call."""

    def __init__(self, connection: "_FakeConnection") -> None:
        self.connection = connection
        self._result: Any = None
        self._rows: list[Any] = []
        self.rowcount = -1

    def __enter__(self) -> "_FakeCursor":
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


class _FakeConnection:
    def __init__(self, handlers: list[tuple[str, Any]]) -> None:
        self.handlers = handlers
        self.executions: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _statements(connection: _FakeConnection, needle: str) -> list[tuple[str, Any]]:
    return [call for call in connection.executions if needle in call[0]]


# ---------------------------------------------------------------------------
# 1. Batch planner (ctid block ranges)
# ---------------------------------------------------------------------------


def test_plan_batches_covers_every_page_exactly_once_with_half_open_ranges() -> None:
    plan = backfill.plan_batches(10, 4)

    assert plan == [(0, 4), (4, 8), (8, 10)]
    covered = [page for start, end in plan for page in range(start, end)]
    assert covered == list(range(10))


def test_plan_batches_resumes_from_a_persisted_cursor_without_rescanning_the_prefix() -> None:
    assert backfill.plan_batches(10, 4, first_page=8) == [(8, 10)]
    # Cursor already past the end: nothing to do, and emphatically not a rescan.
    assert backfill.plan_batches(10, 4, first_page=10) == []
    assert backfill.plan_batches(0, 4) == []


def test_plan_batches_rejects_a_zero_width_batch_rather_than_looping_forever() -> None:
    with pytest.raises(backfill.BackfillConfigError):
        backfill.plan_batches(10, 0)


# ---------------------------------------------------------------------------
# 2. Dual-safety re-entrancy: NULL sentinel + block cursor
# ---------------------------------------------------------------------------


def test_candidate_count_and_update_share_the_identical_range_and_sentinel_predicate() -> None:
    """A drift between these two turns the shortfall detector into a liar.

    The shortfall check subtracts the UPDATE's rowcount from the candidate
    count. If the two statements select different row sets, the difference
    stops meaning "rows the join could not resolve" and the runner would halt
    on phantom shortfalls (or, worse, miss real ones).
    """
    for sql in (backfill._CANDIDATE_COUNT_SQL, backfill._BATCH_UPDATE_SQL):
        assert "t.ctid >= %(first_page_tid)s::tid" in sql
        assert "t.ctid < %(last_page_tid)s::tid" in sql
        assert "t.run_key IS NULL" in sql


def test_update_repeats_the_null_sentinel_so_re_entry_cannot_double_apply() -> None:
    """The sentinel is the correctness mechanism; the cursor is only a speed-up."""
    assert f"t.{backfill.SENTINEL_COLUMN} IS NULL" in backfill._BATCH_UPDATE_SQL


def test_update_writes_out_every_join_predicate_explicitly() -> None:
    sql = backfill._BATCH_UPDATE_SQL
    for predicate in (
        "hr.run_id = t.run_id",
        "rnv.river_network_version_id = t.river_network_version_id",
        "bv.basin_version_id = t.basin_version_id",
        "rs.river_segment_id = t.river_segment_id",
        "rs.river_network_version_id = t.river_network_version_id",
        "ve.label::text = t.variable",
        "ue.label::text = t.unit",
        "qe.label::text = t.quality_flag",
    ):
        assert predicate in sql, f"missing explicit join predicate: {predicate}"


def test_update_joins_enum_labels_instead_of_casting_the_text_column() -> None:
    """A direct cast raises on an unmappable value and destroys the batch.

    Joining the label set leaves the offending row untouched, where the
    shortfall detector finds it and reports it as unmappable.
    """
    sql = backfill._BATCH_UPDATE_SQL
    assert "enum_range(NULL::hydro.river_variable)" in sql
    assert "::hydro.river_variable" not in sql.replace("enum_range(NULL::hydro.river_variable)", "")


def test_second_complete_pass_reports_zero_changed_rows() -> None:
    chunk = _chunk("c1", days_old=30, pages=4)
    connection = _FakeConnection(
        [
            ("timescaledb_information.chunks", lambda s, p: {"fetchone": (False,)}),
            (backfill._PENDING_COUNT_SQL.strip()[:40], lambda s, p: {"fetchone": (0,)}),
            ("SELECT count(*)\nFROM ONLY", lambda s, p: {"fetchone": (0,)}),
        ]
    )
    config = _config(Path("/tmp"), enforce=True)
    budget = [config.max_batches]

    descriptor = backfill.process_chunk(connection, chunk, config, cursor_budget=budget)

    assert descriptor["state"] == "skipped_no_pending"
    assert descriptor["updated_rows"] == 0
    assert _statements(connection, "UPDATE ONLY") == []


# ---------------------------------------------------------------------------
# 3. Duration wall: one halved-range retry, then fail closed
# ---------------------------------------------------------------------------


def test_duration_wall_is_enforced_by_the_server_not_a_client_stopwatch() -> None:
    """A client-side timer cannot stop a statement that already holds row locks."""
    chunk = _chunk("c1", days_old=30)
    connection = _FakeConnection(
        [
            ("SELECT count(*)", lambda s, p: {"fetchone": (5,)}),
            ("UPDATE ONLY", lambda s, p: {"rowcount": 5}),
        ]
    )
    with connection.cursor() as cursor:
        backfill.execute_batch(cursor, chunk, first_page=0, last_page=4, duration_wall_ms=7777)

    assert ("SET LOCAL statement_timeout = 7777", None) in connection.executions


def test_cancelled_batch_is_retried_once_at_half_the_range() -> None:
    chunk = _chunk("c1", days_old=30)
    attempts: list[tuple[str, str]] = []

    def update_handler(sql: str, params: Any) -> Any:
        attempts.append((params["first_page_tid"], params["last_page_tid"]))
        if len(attempts) == 1:
            return _QueryCancelled()
        return {"rowcount": 3}

    connection = _FakeConnection(
        [
            ("SELECT count(*)", lambda s, p: {"fetchone": (3,)}),
            ("UPDATE ONLY", update_handler),
        ]
    )
    config = _config(Path("/tmp"), enforce=True)

    outcome, halved = backfill._run_one_batch_with_retry(connection, chunk, config, 0, 100)

    assert halved == 1
    assert attempts == [("(0,0)", "(100,0)"), ("(0,0)", "(50,0)")]
    assert outcome.last_page == 50
    assert connection.rollbacks == 1


def test_second_cancellation_stops_fail_closed_instead_of_retrying_forever() -> None:
    chunk = _chunk("c1", days_old=30)
    connection = _FakeConnection(
        [
            ("SELECT count(*)", lambda s, p: {"fetchone": (3,)}),
            ("UPDATE ONLY", lambda s, p: _QueryCancelled()),
        ]
    )
    config = _config(Path("/tmp"), enforce=True)

    with pytest.raises(backfill.BackfillStop) as excinfo:
        backfill._run_one_batch_with_retry(connection, chunk, config, 0, 100)

    assert excinfo.value.stage == "duration_wall"
    assert excinfo.value.detail["first_page"] == 0
    assert excinfo.value.detail["last_page"] == 50


def test_single_page_range_cannot_be_halved_and_stops_immediately() -> None:
    chunk = _chunk("c1", days_old=30)
    connection = _FakeConnection(
        [
            ("SELECT count(*)", lambda s, p: {"fetchone": (3,)}),
            ("UPDATE ONLY", lambda s, p: _QueryCancelled()),
        ]
    )
    config = _config(Path("/tmp"), enforce=True)

    with pytest.raises(backfill.BackfillStop) as excinfo:
        backfill._run_one_batch_with_retry(connection, chunk, config, 7, 8)

    assert excinfo.value.stage == "duration_wall"
    assert "cannot be halved further" in excinfo.value.reason


# ---------------------------------------------------------------------------
# 4. Chunk-level skips: compressed and active
# ---------------------------------------------------------------------------


def test_compressed_chunks_are_skipped_and_active_chunks_are_skipped_for_different_reasons() -> None:
    compressed = _chunk("compressed", days_old=30, compressed=True)
    terminal = _chunk("terminal", days_old=30)
    active = _chunk("active", days_old=0)

    eligible, skipped = backfill.classify_chunks(
        [compressed, terminal, active], now_utc=_NOW, lag_seconds=_LAG, final_sweep=False
    )

    assert eligible == [terminal]
    assert [(c.chunk_name, reason) for c, reason in skipped] == [
        ("compressed", "compressed"),
        ("active", "active"),
    ]


def test_final_sweep_relaxes_only_the_active_rule_never_the_compressed_one() -> None:
    """No flag makes DML against compressed storage legal on TimescaleDB 2.10."""
    compressed = _chunk("compressed", days_old=0, compressed=True)
    active = _chunk("active", days_old=0)

    eligible, skipped = backfill.classify_chunks(
        [compressed, active], now_utc=_NOW, lag_seconds=_LAG, final_sweep=True
    )

    assert eligible == [active]
    assert [(c.chunk_name, reason) for c, reason in skipped] == [("compressed", "compressed")]


def test_active_criterion_is_the_compression_lanes_range_end_lag_rule() -> None:
    lagged = _chunk("terminal", days_old=8)
    inside = _chunk("active", days_old=6)

    assert not backfill.is_active_chunk(lagged, now_utc=_NOW, lag_seconds=_LAG)
    assert backfill.is_active_chunk(inside, now_utc=_NOW, lag_seconds=_LAG)


def test_lag_falls_back_to_the_compression_lane_variable_so_the_lanes_cannot_drift(
    tmp_path: Path,
) -> None:
    config = backfill.config_from_args(_args(), _env(tmp_path))
    assert config.lag_seconds == _LAG

    overridden = backfill.config_from_args(
        _args(),
        _env(tmp_path, NODE27_RIVER_IDENTITY_BACKFILL_LAG_SECONDS="60"),
    )
    assert overridden.lag_seconds == 60


def test_missing_lag_configuration_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(backfill.BackfillConfigError, match="lag seconds must be set"):
        backfill.config_from_args(
            _args(), _env(tmp_path, NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS=None)
        )


def test_process_chunk_consults_the_shared_write_guard_before_any_update() -> None:
    """Guard first, always — a stale inventory is exactly how a compressed
    chunk sneaks into a batch (the compression timer may have fired between
    discovery and now)."""
    chunk = _chunk("c1", days_old=30, pages=4)
    connection = _FakeConnection(
        [
            ("timescaledb_information.chunks", lambda s, p: {"fetchone": (True,)}),
        ]
    )
    config = _config(Path("/tmp"), enforce=True)

    with pytest.raises(backfill.CompressedChunkWriteError):
        backfill.process_chunk(connection, chunk, config, cursor_budget=[10])

    assert _statements(connection, "UPDATE ONLY") == []


def test_runner_imports_the_chunk_guard_from_the_shared_module_not_a_local_copy() -> None:
    """Design D5 (#851): the fourth production writer is held to the same rule
    as the three batch-window writers — no per-path guard reimplementation."""
    from packages.common.timescale_write_guard import assert_chunk_uncompressed as canonical

    assert backfill.assert_chunk_uncompressed is canonical

    tree = ast.parse(_RUNNER_SOURCE_PATH.read_text(encoding="utf-8"))
    imported_from_guard = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "packages.common.timescale_write_guard"
        for alias in node.names
    }
    assert "assert_chunk_uncompressed" in imported_from_guard

    # The runner reads is_compressed once, for the receipt inventory. It must
    # never make the pre-write DECISION from its own predicate: the only
    # `is_compressed = ...` filter in the repo belongs to the shared guard.
    source = _RUNNER_SOURCE_PATH.read_text(encoding="utf-8")
    assert "is_compressed = true" not in source.lower()


# ---------------------------------------------------------------------------
# 5. Fail-closed shortfall, split into unmatched vs unmappable
# ---------------------------------------------------------------------------


def _shortfall_connection(*, candidates: int, updated: int, unmatched: int, unmappable: int):
    return _FakeConnection(
        [
            ("timescaledb_information.chunks", lambda s, p: {"fetchone": (False,)}),
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            (
                "NOT EXISTS (SELECT 1 FROM hydro.hydro_run",
                lambda s, p: {"fetchone": (unmatched,)},
            ),
            ("NOT IN (SELECT unnest", lambda s, p: {"fetchone": (unmappable,)}),
            ("UPDATE ONLY", lambda s, p: {"rowcount": updated}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (candidates,)}),
        ]
    )


def test_shortfall_stops_the_run_and_names_both_causes_separately() -> None:
    chunk = _chunk("c1", days_old=30, pages=4)
    connection = _shortfall_connection(candidates=100, updated=91, unmatched=6, unmappable=3)
    config = _config(Path("/tmp"), enforce=True)

    with pytest.raises(backfill.BackfillStop) as excinfo:
        backfill.process_chunk(connection, chunk, config, cursor_budget=[10])

    stop = excinfo.value
    assert stop.stage == "shortfall"
    assert stop.detail["unmatched_rows"] == 6
    assert stop.detail["unmappable_rows"] == 3
    assert stop.detail["candidate_rows"] == 100
    assert stop.detail["updated_rows"] == 91
    # The partial batch must NOT be recorded as progress.
    assert connection.commits == 0


def test_no_shortfall_skips_the_diagnostic_queries_entirely() -> None:
    chunk = _chunk("c1", days_old=30)
    connection = _shortfall_connection(candidates=10, updated=10, unmatched=0, unmappable=0)

    with connection.cursor() as cursor:
        outcome = backfill.execute_batch(
            cursor, chunk, first_page=0, last_page=4, duration_wall_ms=30_000
        )

    assert outcome.shortfall == 0
    assert _statements(connection, "NOT EXISTS (SELECT 1 FROM hydro.hydro_run") == []


def test_equality_audit_uses_the_same_predicate_as_the_verify_function() -> None:
    """The runner receipt and the SQL verify function must agree, or an
    operator gets two different answers to the same pre-cutover question."""
    migration = _MIGRATION_PATH.read_text(encoding="utf-8")
    for leg in (
        "t.variable_e::text IS DISTINCT FROM t.variable",
        "t.unit_e::text IS DISTINCT FROM t.unit",
        "t.quality_flag_e::text IS DISTINCT FROM t.quality_flag",
    ):
        assert leg in backfill._EQUALITY_AUDIT_SQL
        assert leg in migration


# ---------------------------------------------------------------------------
# 6. --final-sweep quiescence assertion
# ---------------------------------------------------------------------------


def test_final_sweep_refuses_while_write_counters_are_still_moving() -> None:
    chunk = _chunk("active", days_old=0)
    samples = iter([1000, 1007])
    connection = _FakeConnection(
        [("pg_stat_all_tables", lambda s, p: {"fetchone": (next(samples),)})]
    )

    with connection.cursor() as cursor:
        with pytest.raises(backfill.BackfillStop) as excinfo:
            backfill.assert_ingest_quiescent(
                cursor, chunk, quiescence_seconds=1, sleep=lambda _s: None
            )

    assert excinfo.value.stage == "ingest_not_quiescent"
    assert "7 write(s)" in excinfo.value.reason


def test_final_sweep_proceeds_when_the_write_counters_are_frozen() -> None:
    chunk = _chunk("active", days_old=0)
    connection = _FakeConnection([("pg_stat_all_tables", lambda s, p: {"fetchone": (42,)})])

    with connection.cursor() as cursor:
        backfill.assert_ingest_quiescent(cursor, chunk, quiescence_seconds=1, sleep=lambda _s: None)


# ---------------------------------------------------------------------------
# 7. Modes: dry-run and probe never persist
# ---------------------------------------------------------------------------


def test_dry_run_plans_batches_but_issues_no_update() -> None:
    chunk = _chunk("c1", days_old=30, pages=9)
    connection = _FakeConnection(
        [
            ("timescaledb_information.chunks", lambda s, p: {"fetchone": (False,)}),
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (700,)}),
        ]
    )
    config = _config(Path("/tmp"), enforce=False)

    descriptor = backfill.process_chunk(connection, chunk, config, cursor_budget=[10])

    assert descriptor["state"] == "planned"
    assert descriptor["pending_rows"] == 700
    assert descriptor["batches_planned"] == 1
    assert _statements(connection, "UPDATE ONLY") == []
    assert connection.commits == 0


def test_probe_runs_a_real_update_then_always_rolls_back() -> None:
    chunk = _chunk("c1", days_old=30, pages=4)
    connection = _FakeConnection(
        [
            ("timescaledb_information.chunks", lambda s, p: {"fetchone": (False,)}),
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("pg_locks", lambda s, p: {"fetchall": [("relation", "RowExclusiveLock", True, "c1")]}),
            ("UPDATE ONLY", lambda s, p: {"rowcount": 55}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (55,)}),
        ]
    )
    config = _config(Path("/tmp"), probe=True)

    descriptor = backfill.process_chunk(connection, chunk, config, cursor_budget=[10])

    assert _statements(connection, "UPDATE ONLY")
    assert descriptor["probe"]["rolled_back"] is True
    assert descriptor["probe"]["updated_rows"] == 55
    assert descriptor["probe"]["lock_waits"] == [
        {"locktype": "relation", "mode": "RowExclusiveLock", "granted": True, "relation": "c1"}
    ]
    assert connection.commits == 0
    assert connection.rollbacks >= 1


def test_probe_rolls_back_even_when_the_batch_raises() -> None:
    chunk = _chunk("c1", days_old=30, pages=4)
    connection = _FakeConnection(
        [
            ("timescaledb_information.chunks", lambda s, p: {"fetchone": (False,)}),
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("UPDATE ONLY", lambda s, p: RuntimeError("boom")),
            ("SELECT count(*)", lambda s, p: {"fetchone": (55,)}),
        ]
    )
    config = _config(Path("/tmp"), probe=True)

    with pytest.raises(RuntimeError):
        backfill.process_chunk(connection, chunk, config, cursor_budget=[10])

    assert connection.rollbacks >= 1
    assert connection.commits == 0


def test_enforce_and_probe_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(backfill.BackfillConfigError, match="mutually exclusive"):
        backfill.config_from_args(_args(enforce=True, probe=True), _env(tmp_path))


# ---------------------------------------------------------------------------
# 8. Receipt: schema conformance and bounded budget
# ---------------------------------------------------------------------------


def _build_receipt(tmp_path: Path, chunks: list[backfill.ChunkRow], **cfg: Any) -> dict[str, Any]:
    connection = _FakeConnection(
        [
            (
                # Unique to _CHUNK_INVENTORY_SQL; must be matched before the
                # guard's own timescaledb_information.chunks lookup below.
                "pg_total_relation_size",
                lambda s, p: {"fetchall": [_inventory_row(c) for c in chunks]},
            ),
            ("timescaledb_information.chunks", lambda s, p: {"fetchone": (False,)}),
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (0,)}),
        ]
    )
    return backfill.build_receipt(
        _config(tmp_path, **cfg),
        now_utc=_NOW,
        connect=lambda _url: connection,
        head_sha="0" * 40,
        timer_state={"observed": True, "is_active": "inactive", "is_enabled": "masked",
                     "unavailable_reason": None},
    )


def _inventory_row(chunk: backfill.ChunkRow) -> dict[str, Any]:
    return {
        "chunk_schema": chunk.chunk_schema,
        "chunk_name": chunk.chunk_name,
        "range_start": chunk.range_start,
        "range_end": chunk.range_end,
        "is_compressed": chunk.is_compressed,
        "total_bytes": chunk.total_bytes,
        "relpages": chunk.relpages,
        "pages_planned": chunk.pages_planned,
        "approx_rows": chunk.approx_rows,
    }


def test_dry_run_receipt_validates_against_the_published_schema(tmp_path: Path) -> None:
    receipt = _build_receipt(
        tmp_path,
        [_chunk("terminal", days_old=30), _chunk("frozen", days_old=30, compressed=True),
         _chunk("active", days_old=0)],
    )

    jsonschema.validate(receipt, _load_schema())
    assert receipt["outcome"] == "clean"
    assert receipt["mode"] == "dry-run"
    assert receipt["totals"]["updated_rows"] == 0
    assert receipt["totals"]["chunks_skipped_compressed"] == 1
    assert receipt["totals"]["chunks_skipped_active"] == 1


def test_receipt_records_the_compression_timer_state(tmp_path: Path) -> None:
    """D6: an enforce window that left the timer running is a recoverable-only-
    by-decompressing-200GB mistake. The receipt has to show which it was."""
    receipt = _build_receipt(tmp_path, [_chunk("terminal", days_old=30)])

    assert receipt["compression_timer"]["is_enabled"] == "masked"


def test_receipt_reports_bloat_against_measured_disk_headroom(tmp_path: Path) -> None:
    receipt = _build_receipt(tmp_path, [_chunk("terminal", days_old=30, total_bytes=4096)])

    disk = receipt["disk"]
    assert disk["estimated_bloat_bytes"] == 4096
    assert disk["mount"] == "/home"
    assert disk["headroom_sufficient"] in {True, False, None}


def test_refused_lock_receipt_carries_no_chunk_inventory(tmp_path: Path) -> None:
    receipt = backfill.build_refused_lock_receipt(
        _config(tmp_path), now_utc=_NOW, head_sha="a" * 40
    )

    jsonschema.validate(receipt, _load_schema())
    assert receipt["outcome"] == "refused_lock"
    assert receipt["chunks"] == []
    assert receipt["cursor"] == {}


def test_failed_receipt_without_provenance_omits_head_sha(tmp_path: Path) -> None:
    receipt = backfill.build_failed_receipt(
        _config(tmp_path), now_utc=_NOW, stage="runner", head_sha=None
    )

    jsonschema.validate(receipt, _load_schema())
    assert receipt["provenance_state"] == "unavailable"
    assert "head_sha" not in receipt


def test_schema_rejects_a_stopped_receipt_that_hides_why_it_stopped() -> None:
    """A halted run that looks like a finished run is the failure this schema
    rule exists to prevent."""
    bad = {
        "schema_version": "1.0",
        "generated_at": "2026-08-15T12:00:00Z",
        "mode": "enforce",
        "outcome": "stopped",
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, _load_schema())


def test_schema_rejects_a_dry_run_that_claims_to_have_updated_rows() -> None:
    bad = {
        "schema_version": "1.0",
        "head_sha": "0" * 40,
        "generated_at": "2026-08-15T12:00:00Z",
        "now_utc": "2026-08-15T12:00:00Z",
        "hypertable": "hydro.river_timeseries",
        "mode": "dry-run",
        "outcome": "clean",
        "bounds": {
            "batch_pages": 1,
            "duration_wall_ms": 1000,
            "batch_sleep_ms": 0,
            "max_batches": 1,
            "lag_seconds": 1,
        },
        "totals": {
            "candidate_rows": 5,
            "updated_rows": 5,
            "pending_rows": 0,
            "batches_run": 1,
            "batches_halved": 0,
            "unmatched_rows": 0,
            "unmappable_rows": 0,
            "chunks_skipped_compressed": 0,
            "chunks_skipped_active": 0,
        },
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, _load_schema())


def test_per_invocation_batch_budget_defers_the_remainder(tmp_path: Path) -> None:
    chunk = _chunk("c1", days_old=30, pages=100)
    connection = _FakeConnection(
        [
            ("timescaledb_information.chunks", lambda s, p: {"fetchone": (False,)}),
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("UPDATE ONLY", lambda s, p: {"rowcount": 4}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (4,)}),
        ]
    )
    config = _config(tmp_path, enforce=True, batch_pages=10, max_batches=2, batch_sleep_ms=0)

    descriptor = backfill.process_chunk(connection, chunk, config, cursor_budget=[2])

    assert descriptor["batches_run"] == 2
    assert descriptor["state"] == "deferred"
    assert descriptor["next_page"] == 20


# ---------------------------------------------------------------------------
# 9. flock single-instance mutex
# ---------------------------------------------------------------------------


def test_second_holder_is_refused_the_lock(tmp_path: Path) -> None:
    lock_path = tmp_path / "runner.lock"

    first = backfill.acquire_lock(lock_path)
    assert first is not None
    try:
        assert backfill.acquire_lock(lock_path) is None
    finally:
        os.close(first)

    # Released: a later invocation gets it.
    third = backfill.acquire_lock(lock_path)
    assert third is not None
    os.close(third)


def test_lock_file_is_created_mode_0600(tmp_path: Path) -> None:
    lock_path = tmp_path / "runner.lock"
    fd = backfill.acquire_lock(lock_path)
    try:
        assert oct(lock_path.stat().st_mode & 0o777) == "0o600"
    finally:
        os.close(fd)


def test_lock_path_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(backfill.BackfillConfigError):
        backfill.acquire_lock(Path("relative.lock"))


def test_contended_lock_publishes_a_refusal_receipt_and_touches_no_database(
    tmp_path: Path,
) -> None:
    env = _env(tmp_path)
    for key, value in env.items():
        os.environ[key] = value
    holder = backfill.acquire_lock(tmp_path / "runner.lock")
    assert holder is not None

    def _explode(_url: str) -> Any:  # pragma: no cover - must never run
        raise AssertionError("lock contention must be decided before any DB call")

    try:
        exit_code = backfill.main([], now_utc=_NOW, connect=_explode)
    finally:
        os.close(holder)
        for key in env:
            os.environ.pop(key, None)

    assert exit_code == 0
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    jsonschema.validate(receipt, _load_schema())
    assert receipt["outcome"] == "refused_lock"


# ---------------------------------------------------------------------------
# 10. Config fail-closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "override,expected",
    [
        ({"NODE27_RIVER_IDENTITY_BACKFILL_RECEIPT_PATH": None}, "receipt path must be set"),
        ({"NODE27_RIVER_IDENTITY_BACKFILL_RECEIPT_PATH": "relative.json"}, "must be absolute"),
        ({"NODE27_RIVER_IDENTITY_BACKFILL_LOCK_PATH": None}, "lock path must be set"),
        ({"DATABASE_URL": None}, "DATABASE_URL must be set"),
        ({"NODE27_RIVER_IDENTITY_BACKFILL_BATCH_PAGES": "0"}, "must be >= 1"),
        ({"NODE27_RIVER_IDENTITY_BACKFILL_DURATION_WALL_MS": "10"}, "must be >= 1000"),
    ],
)
def test_config_fails_closed_on_bad_shape(
    tmp_path: Path, override: dict[str, str | None], expected: str
) -> None:
    with pytest.raises(backfill.BackfillConfigError, match=expected):
        backfill.config_from_args(_args(), _env(tmp_path, **override))


def test_receipt_and_lock_paths_must_be_disjoint(tmp_path: Path) -> None:
    shared = str(tmp_path / "same.json")
    with pytest.raises(backfill.BackfillConfigError):
        backfill.config_from_args(
            _args(),
            _env(
                tmp_path,
                NODE27_RIVER_IDENTITY_BACKFILL_RECEIPT_PATH=shared,
                NODE27_RIVER_IDENTITY_BACKFILL_LOCK_PATH=shared,
            ),
        )


def test_stderr_diagnostics_never_leak_the_dsn_password(capsys: pytest.CaptureFixture[str]) -> None:
    backfill._emit("failed", "boom", dsn="postgresql://user:secretpw@127.0.0.1:55432/nhms")

    captured = capsys.readouterr().err
    assert "secretpw" not in captured
    assert "127.0.0.1:55432" in captured
