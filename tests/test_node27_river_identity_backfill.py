"""Unit tests for the node-27 river identity backfill runner (issue #1339).

Batch planning, cursor re-entrancy, the duration wall, the two chunk-level
skips, the per-batch write guard and the fail-closed shortfall split, all
driven through the cursor-level fakes in ``tests/river_identity_backfill_fakes``.
Receipt/schema/lock/config coverage lives in
``tests/test_node27_river_identity_backfill_receipt.py``.

What this module deliberately does NOT claim: anything about TimescaleDB
compression semantics. Those are pinned by the integration module against
node-27's throwaway database, because CI runs ``pg15-latest`` and the
production oracle is 2.10.2.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_river_identity_backfill as backfill
from tests.river_identity_backfill_fakes import (
    LAG,
    MIGRATION_PATH,
    NOW,
    RUNNER_SOURCE_PATH,
    UNCOMPRESSED_GUARD,
    FakeConnection,
    QueryCancelled,
    chunk,
    config,
    env,
    inventory_row,
    statements,
)
from tests.river_identity_backfill_fakes import (
    args as _args,
)

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
    target = chunk("c1", days_old=30, pages=4)
    connection = FakeConnection(
        [
            UNCOMPRESSED_GUARD,
            (backfill._PENDING_COUNT_SQL.strip()[:40], lambda s, p: {"fetchone": (0,)}),
            ("SELECT count(*)\nFROM ONLY", lambda s, p: {"fetchone": (0,)}),
        ]
    )
    settings = config(Path("/tmp"), enforce=True)

    descriptor = backfill.process_chunk(
        connection, target, settings, cursor_budget=[settings.max_batches]
    )

    assert descriptor["state"] == "skipped_no_pending"
    assert descriptor["updated_rows"] == 0
    assert statements(connection, "UPDATE ONLY") == []


# ---------------------------------------------------------------------------
# 2b. The persisted block cursor is READ BACK, not just written
# ---------------------------------------------------------------------------


def _fresh_connection(*, candidates: int = 4, updated: int = 4) -> FakeConnection:
    return FakeConnection(
        [
            UNCOMPRESSED_GUARD,
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("UPDATE ONLY", lambda s, p: {"rowcount": updated}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (candidates,)}),
        ]
    )


def _first_update_range(connection: FakeConnection) -> tuple[str, str]:
    sql, params = statements(connection, "UPDATE ONLY")[0]
    return params["first_page_tid"], params["last_page_tid"]


def test_enforce_resumes_from_the_page_the_previous_receipt_recorded() -> None:
    """The production path must CONSUME the cursor, not merely publish it.

    A write-only cursor makes the per-invocation budget a permanent stall: each
    run re-scans the finished prefix with zero-candidate batches, burns the
    whole budget on them, and still reports a clean deferral.
    """
    target = chunk("c1", days_old=30, pages=100)
    connection = _fresh_connection()
    settings = config(Path("/tmp"), enforce=True, batch_pages=10, max_batches=1, batch_sleep_ms=0)

    descriptor = backfill.process_chunk(
        connection, target, settings, cursor_budget=[1], first_page=60
    )

    assert descriptor["resumed_from_page"] == 60
    assert _first_update_range(connection) == ("(60,0)", "(70,0)")
    # Only the remaining pages were planned, not all ten batches.
    assert descriptor["batches_planned"] == 4


def test_a_stale_cursor_that_plans_nothing_is_discarded_in_favour_of_a_rescan() -> None:
    """The sentinel outranks the cursor. A cursor past the end of a chunk that
    still holds NULL rows would strand them forever."""
    target = chunk("c1", days_old=30, pages=8)
    connection = _fresh_connection()
    settings = config(Path("/tmp"), enforce=True, batch_pages=4, max_batches=10, batch_sleep_ms=0)

    descriptor = backfill.process_chunk(
        connection, target, settings, cursor_budget=[10], first_page=999
    )

    assert descriptor["resumed_from_page"] == 0
    assert _first_update_range(connection) == ("(0,0)", "(4,0)")


def test_build_receipt_feeds_the_previous_receipts_cursor_into_the_plan(tmp_path: Path) -> None:
    target = chunk("c1", days_old=30, pages=100)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema_version": backfill.SCHEMA_VERSION,
                "cursor": {target.key: 80},
            }
        ),
        encoding="utf-8",
    )
    connection = FakeConnection(
        [
            ("pg_total_relation_size", lambda s, p: {"fetchall": [inventory_row(target)]}),
            UNCOMPRESSED_GUARD,
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("UPDATE ONLY", lambda s, p: {"rowcount": 4}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (4,)}),
        ]
    )

    receipt = backfill.build_receipt(
        config(tmp_path, enforce=True, batch_pages=10, max_batches=1, batch_sleep_ms=0),
        now_utc=NOW,
        connect=lambda _url: connection,
        head_sha="0" * 40,
        timer_state={"observed": True, "is_active": "inactive", "is_enabled": "masked",
                     "unavailable_reason": None},
    )

    assert receipt["chunks"][0]["resumed_from_page"] == 80
    assert _first_update_range(connection) == ("(80,0)", "(90,0)")
    assert receipt["cursor"][target.key] == 90


def test_persisted_cursor_is_discarded_when_the_receipt_is_unusable(tmp_path: Path) -> None:
    """Anything unrecognised degrades to a full rescan (slow, still correct)
    rather than to a guess (fast, possibly stranding rows)."""
    path = tmp_path / "receipt.json"

    assert backfill.load_persisted_cursor(path) == {}

    path.write_text("{not json", encoding="utf-8")
    assert backfill.load_persisted_cursor(path) == {}

    path.write_text(json.dumps({"schema_version": "9.9", "cursor": {"a.b": 5}}), encoding="utf-8")
    assert backfill.load_persisted_cursor(path) == {}

    path.write_text(
        json.dumps(
            {
                "schema_version": backfill.SCHEMA_VERSION,
                "cursor": {"a.b": 5, "c.d": -1, "e.f": "12", "g.h": True},
            }
        ),
        encoding="utf-8",
    )
    assert backfill.load_persisted_cursor(path) == {"a.b": 5}


# ---------------------------------------------------------------------------
# 3. Duration wall: one halved-range retry, then fail closed
# ---------------------------------------------------------------------------


def test_duration_wall_is_enforced_by_the_server_not_a_client_stopwatch() -> None:
    """A client-side timer cannot stop a statement that already holds row locks."""
    target = chunk("c1", days_old=30)
    connection = FakeConnection(
        [
            ("SELECT count(*)", lambda s, p: {"fetchone": (5,)}),
            ("UPDATE ONLY", lambda s, p: {"rowcount": 5}),
        ]
    )
    with connection.cursor() as cursor:
        backfill.execute_batch(cursor, target, first_page=0, last_page=4, duration_wall_ms=7777)

    assert ("SET LOCAL statement_timeout = 7777", None) in connection.executions


def test_cancelled_batch_is_retried_once_at_half_the_range_and_returns_the_remainder() -> None:
    target = chunk("c1", days_old=30)
    attempts: list[tuple[str, str]] = []

    def update_handler(sql: str, params: Any) -> Any:
        attempts.append((params["first_page_tid"], params["last_page_tid"]))
        if len(attempts) == 1:
            return QueryCancelled()
        return {"rowcount": 3}

    connection = FakeConnection(
        [
            UNCOMPRESSED_GUARD,
            ("SELECT count(*)", lambda s, p: {"fetchone": (3,)}),
            ("UPDATE ONLY", update_handler),
        ]
    )
    settings = config(Path("/tmp"), enforce=True)

    outcome, halved, remainder = backfill._run_one_batch_with_retry(
        connection, target, settings, 0, 100
    )

    assert halved == 1
    assert attempts == [("(0,0)", "(100,0)"), ("(0,0)", "(50,0)")]
    assert outcome.last_page == 50
    # The half that was NOT run must come back for re-queueing. Returning only
    # the first half silently abandons pages [50,100) and still reports clean.
    assert remainder == (50, 100)
    assert connection.rollbacks == 1


def test_the_unrun_half_of_a_halved_batch_is_actually_executed_afterwards() -> None:
    """R3 in the shape that matters: the range hole must be closed by the loop,
    not merely reported by the helper."""
    target = chunk("c1", days_old=30, pages=8)
    ranges: list[tuple[str, str]] = []

    def update_handler(sql: str, params: Any) -> Any:
        ranges.append((params["first_page_tid"], params["last_page_tid"]))
        if len(ranges) == 1:
            return QueryCancelled()
        return {"rowcount": 1}

    connection = FakeConnection(
        [
            UNCOMPRESSED_GUARD,
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("UPDATE ONLY", update_handler),
            ("SELECT count(*)", lambda s, p: {"fetchone": (1,)}),
        ]
    )
    settings = config(Path("/tmp"), enforce=True, batch_pages=8, max_batches=10, batch_sleep_ms=0)

    descriptor = backfill.process_chunk(connection, target, settings, cursor_budget=[10])

    assert ranges == [("(0,0)", "(8,0)"), ("(0,0)", "(4,0)"), ("(4,0)", "(8,0)")]
    assert descriptor["next_page"] == 8
    assert descriptor["state"] == "processed"


def test_second_cancellation_stops_fail_closed_instead_of_retrying_forever() -> None:
    target = chunk("c1", days_old=30)
    connection = FakeConnection(
        [
            UNCOMPRESSED_GUARD,
            ("SELECT count(*)", lambda s, p: {"fetchone": (3,)}),
            ("UPDATE ONLY", lambda s, p: QueryCancelled()),
        ]
    )
    settings = config(Path("/tmp"), enforce=True)

    with pytest.raises(backfill.BackfillStop) as excinfo:
        backfill._run_one_batch_with_retry(connection, target, settings, 0, 100)

    assert excinfo.value.stage == "duration_wall"
    assert excinfo.value.detail["first_page"] == 0
    assert excinfo.value.detail["last_page"] == 50


def test_single_page_range_cannot_be_halved_and_stops_immediately() -> None:
    target = chunk("c1", days_old=30)
    connection = FakeConnection(
        [
            UNCOMPRESSED_GUARD,
            ("SELECT count(*)", lambda s, p: {"fetchone": (3,)}),
            ("UPDATE ONLY", lambda s, p: QueryCancelled()),
        ]
    )
    settings = config(Path("/tmp"), enforce=True)

    with pytest.raises(backfill.BackfillStop) as excinfo:
        backfill._run_one_batch_with_retry(connection, target, settings, 7, 8)

    assert excinfo.value.stage == "duration_wall"
    assert "cannot be halved further" in excinfo.value.reason


# ---------------------------------------------------------------------------
# 4. Chunk-level skips: compressed and active
# ---------------------------------------------------------------------------


def test_compressed_chunks_are_skipped_and_active_chunks_are_skipped_for_different_reasons() -> None:
    compressed = chunk("compressed", days_old=30, compressed=True)
    terminal = chunk("terminal", days_old=30)
    active = chunk("active", days_old=0)

    eligible, skipped = backfill.classify_chunks(
        [compressed, terminal, active], now_utc=NOW, lag_seconds=LAG, final_sweep=False
    )

    assert eligible == [terminal]
    assert [(c.chunk_name, reason) for c, reason in skipped] == [
        ("compressed", "compressed"),
        ("active", "active"),
    ]


def test_final_sweep_relaxes_only_the_active_rule_never_the_compressed_one() -> None:
    """No flag makes DML against compressed storage legal on TimescaleDB 2.10."""
    compressed = chunk("compressed", days_old=0, compressed=True)
    active = chunk("active", days_old=0)

    eligible, skipped = backfill.classify_chunks(
        [compressed, active], now_utc=NOW, lag_seconds=LAG, final_sweep=True
    )

    assert eligible == [active]
    assert [(c.chunk_name, reason) for c, reason in skipped] == [("compressed", "compressed")]


def test_active_criterion_is_the_compression_lanes_range_end_lag_rule() -> None:
    lagged = chunk("terminal", days_old=8)
    inside = chunk("active", days_old=6)

    assert not backfill.is_active_chunk(lagged, now_utc=NOW, lag_seconds=LAG)
    assert backfill.is_active_chunk(inside, now_utc=NOW, lag_seconds=LAG)


def test_lag_falls_back_to_the_compression_lane_variable_so_the_lanes_cannot_drift(
    tmp_path: Path,
) -> None:
    settings = backfill.config_from_args(_args(), env(tmp_path))
    assert settings.lag_seconds == LAG

    overridden = backfill.config_from_args(
        _args(),
        env(tmp_path, NODE27_RIVER_IDENTITY_BACKFILL_LAG_SECONDS="60"),
    )
    assert overridden.lag_seconds == 60


def test_missing_lag_configuration_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(backfill.BackfillConfigError, match="lag seconds must be set"):
        backfill.config_from_args(
            _args(), env(tmp_path, NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS=None)
        )


def test_process_chunk_consults_the_shared_write_guard_before_any_update() -> None:
    """Guard first, always — a stale inventory is exactly how a compressed
    chunk sneaks into a batch (the compression timer may have fired between
    discovery and now)."""
    target = chunk("c1", days_old=30, pages=4)
    connection = FakeConnection(
        [
            ("SELECT is_compressed FROM timescaledb_information.chunks",
             lambda s, p: {"fetchone": (True,)}),
        ]
    )
    settings = config(Path("/tmp"), enforce=True)

    with pytest.raises(backfill.CompressedChunkWriteError):
        backfill.process_chunk(connection, target, settings, cursor_budget=[10])

    assert statements(connection, "UPDATE ONLY") == []


def test_the_write_guard_runs_inside_every_batch_transaction_before_the_update() -> None:
    """The guard's contract requires it to share the UPDATE's transaction.

    A single pre-loop check that is then rolled back satisfies "we called the
    guard" and nothing else: the compression timer can fire between that check
    and any later batch, and the batch cursor would never notice.
    """
    target = chunk("c1", days_old=30, pages=12)
    connection = FakeConnection(
        [
            UNCOMPRESSED_GUARD,
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("UPDATE ONLY", lambda s, p: {"rowcount": 2}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (2,)}),
        ]
    )
    settings = config(Path("/tmp"), enforce=True, batch_pages=4, max_batches=10, batch_sleep_ms=0)

    backfill.process_chunk(connection, target, settings, cursor_budget=[10])

    order = [
        "guard" if "SELECT is_compressed FROM timescaledb_information.chunks" in sql
        else "update"
        for sql, _params in connection.executions
        if "SELECT is_compressed FROM timescaledb_information.chunks" in sql
        or "UPDATE ONLY" in sql
    ]
    # One pre-loop planning guard, then guard-then-update for each of the
    # three batches — never an UPDATE that no guard immediately preceded.
    assert order == ["guard", "guard", "update", "guard", "update", "guard", "update"]


def test_a_guard_error_between_batches_becomes_a_stop_that_keeps_its_accounting() -> None:
    """The per-batch guard exists precisely to catch the compression timer
    firing mid-chunk — i.e. AFTER batches have committed. Letting it out as a
    bare exception discards the descriptor and stamps the run
    "failed_before_mutation", which is false the moment one batch commits.
    """
    target = chunk("c1", days_old=30, pages=12)
    guard_calls: list[int] = []

    def guard_handler(sql: str, params: Any) -> Any:
        guard_calls.append(len(guard_calls))
        # Pre-loop check + batch 1 pass; the timer "fires" before batch 2.
        return {"fetchone": (len(guard_calls) > 2,)}

    connection = FakeConnection(
        [
            ("SELECT is_compressed FROM timescaledb_information.chunks", guard_handler),
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("UPDATE ONLY", lambda s, p: {"rowcount": 3}),
            ("SELECT count(*) FROM ONLY", lambda s, p: {"fetchone": (9,)}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (3,)}),
        ]
    )
    settings = config(Path("/tmp"), enforce=True, batch_pages=4, max_batches=10, batch_sleep_ms=0)

    with pytest.raises(backfill.BackfillStop) as excinfo:
        backfill.process_chunk(connection, target, settings, cursor_budget=[10])

    stop = excinfo.value
    assert stop.stage == "compressed_chunk_guard"
    assert isinstance(stop.__cause__, backfill.CompressedChunkWriteError)
    descriptor = stop.descriptor
    assert descriptor is not None
    assert descriptor["state"] == "stopped"
    assert descriptor["updated_rows"] == 3  # batch 1 committed and stays counted
    assert descriptor["pending_rows"] == 9
    assert connection.commits == 1


def test_runner_imports_the_chunk_guard_from_the_shared_module_not_a_local_copy() -> None:
    """Design D5 (#851): the fourth production writer is held to the same rule
    as the three batch-window writers — no per-path guard reimplementation."""
    from packages.common.timescale_write_guard import assert_chunk_uncompressed as canonical

    assert backfill.assert_chunk_uncompressed is canonical

    tree = ast.parse(RUNNER_SOURCE_PATH.read_text(encoding="utf-8"))
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
    source = RUNNER_SOURCE_PATH.read_text(encoding="utf-8")
    assert "is_compressed = true" not in source.lower()


# ---------------------------------------------------------------------------
# 5. Fail-closed shortfall, split into unmatched vs unmappable
# ---------------------------------------------------------------------------


def _shortfall_connection(*, candidates: int, updated: int, unmatched: int, unmappable: int):
    return FakeConnection(
        [
            UNCOMPRESSED_GUARD,
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
    target = chunk("c1", days_old=30, pages=4)
    connection = _shortfall_connection(candidates=100, updated=91, unmatched=6, unmappable=3)
    settings = config(Path("/tmp"), enforce=True)

    with pytest.raises(backfill.BackfillStop) as excinfo:
        backfill.process_chunk(connection, target, settings, cursor_budget=[10])

    stop = excinfo.value
    assert stop.stage == "shortfall"
    assert stop.detail["unmatched_rows"] == 6
    assert stop.detail["unmappable_rows"] == 3
    assert stop.detail["candidate_rows"] == 100
    assert stop.detail["updated_rows"] == 91
    # The partial batch must NOT be recorded as progress.
    assert connection.commits == 0


def test_a_stop_mid_chunk_keeps_the_committed_batches_accounting() -> None:
    """Batches 1..N-1 really did commit. Reporting the chunk as a blank
    'stopped' descriptor — zero batches, zero rows, pending_rows 0 — would hide
    both the work done and the work left.

    The mirror-image error is counting the ROLLED-BACK batch: ``updated_rows``
    is the rows-persisted contract the schema and the integration test's
    per-invocation partition both rely on, so the failed batch contributes
    nothing to it and is reported in ``stop.detail`` instead.
    """
    target = chunk("c1", days_old=30, pages=12)
    calls: list[int] = []

    def update_handler(sql: str, params: Any) -> Any:
        calls.append(len(calls))
        # Third batch under-updates: 2 candidates, 1 written.
        return {"rowcount": 1 if len(calls) == 3 else 2}

    connection = FakeConnection(
        [
            UNCOMPRESSED_GUARD,
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("NOT EXISTS (SELECT 1 FROM hydro.hydro_run", lambda s, p: {"fetchone": (1,)}),
            ("NOT IN (SELECT unnest", lambda s, p: {"fetchone": (0,)}),
            ("UPDATE ONLY", update_handler),
            # Single-line form => the pending recount; the multi-line
            # "SELECT count(*)\nFROM ONLY" form => a per-batch candidate count.
            ("SELECT count(*) FROM ONLY", lambda s, p: {"fetchone": (7,)}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (2,)}),
        ]
    )
    settings = config(Path("/tmp"), enforce=True, batch_pages=4, max_batches=10, batch_sleep_ms=0)

    with pytest.raises(backfill.BackfillStop) as excinfo:
        backfill.process_chunk(connection, target, settings, cursor_budget=[10])

    descriptor = excinfo.value.descriptor
    assert descriptor is not None
    assert descriptor["state"] == "stopped"
    # Attempts, so it matches the budget consumed: 2 committed + 1 rolled back.
    assert descriptor["batches_run"] == 3
    # Rows PERSISTED: 2 + 2. The third batch's single row was rolled back.
    assert descriptor["updated_rows"] == 4
    assert descriptor["updated_rows"] == connection.commits * 2
    assert descriptor["candidate_rows"] == 4
    assert descriptor["unmatched_rows"] == 1
    assert descriptor["pending_rows"] == 7  # recounted, not hard-coded to zero
    assert descriptor["batches_planned"] == 3
    # The rolled-back batch's own arithmetic is not lost, just kept out of the
    # persisted-row counters.
    assert excinfo.value.detail["candidate_rows"] == 2
    assert excinfo.value.detail["updated_rows"] == 1
    # The rolled-back range must be retried on re-entry, not stepped over.
    assert descriptor["next_page"] == 8
    assert connection.commits == 2


def test_no_shortfall_skips_the_diagnostic_queries_entirely() -> None:
    target = chunk("c1", days_old=30)
    connection = _shortfall_connection(candidates=10, updated=10, unmatched=0, unmappable=0)

    with connection.cursor() as cursor:
        outcome = backfill.execute_batch(
            cursor, target, first_page=0, last_page=4, duration_wall_ms=30_000
        )

    assert outcome.shortfall == 0
    assert statements(connection, "NOT EXISTS (SELECT 1 FROM hydro.hydro_run") == []


def test_equality_audit_uses_the_same_predicate_as_the_verify_function() -> None:
    """The runner receipt and the SQL verify function must agree, or an
    operator gets two different answers to the same pre-cutover question."""
    migration = MIGRATION_PATH.read_text(encoding="utf-8")
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


def test_quiescence_discards_the_cached_stats_snapshot_between_the_two_samples() -> None:
    """Without this the gate is unconditionally true.

    PostgreSQL 15 caches ``pg_stat_*`` for the whole transaction
    (``stats_fetch_consistency = cache``), so two samples on one connection
    return identical numbers however busy the writer is. Asserting only on the
    arithmetic (feeding two different values into a fake) cannot catch that —
    the mechanism itself has to be pinned.
    """
    target = chunk("active", days_old=0)
    connection = FakeConnection([("pg_stat_all_tables", lambda s, p: {"fetchone": (1000,)})])

    with connection.cursor() as cursor:
        backfill.assert_ingest_quiescent(
            cursor, target, quiescence_seconds=1, sleep=lambda _s: None
        )

    executed = [sql for sql, _params in connection.executions]
    samples = [i for i, sql in enumerate(executed) if "pg_stat_all_tables" in sql]
    clears = [i for i, sql in enumerate(executed) if "pg_stat_clear_snapshot()" in sql]

    assert len(samples) == 2
    assert len(clears) == 1
    assert samples[0] < clears[0] < samples[1]


def test_final_sweep_refuses_while_write_counters_are_still_moving() -> None:
    target = chunk("active", days_old=0)
    samples = iter([1000, 1007])
    connection = FakeConnection(
        [("pg_stat_all_tables", lambda s, p: {"fetchone": (next(samples),)})]
    )

    with connection.cursor() as cursor:
        with pytest.raises(backfill.BackfillStop) as excinfo:
            backfill.assert_ingest_quiescent(
                cursor, target, quiescence_seconds=1, sleep=lambda _s: None
            )

    assert excinfo.value.stage == "ingest_not_quiescent"
    assert "7 write(s)" in excinfo.value.reason


def test_final_sweep_proceeds_when_the_write_counters_are_frozen() -> None:
    target = chunk("active", days_old=0)
    connection = FakeConnection([("pg_stat_all_tables", lambda s, p: {"fetchone": (42,)})])

    with connection.cursor() as cursor:
        backfill.assert_ingest_quiescent(
            cursor, target, quiescence_seconds=1, sleep=lambda _s: None
        )


# ---------------------------------------------------------------------------
# 7. Modes: dry-run and probe never persist
# ---------------------------------------------------------------------------


def test_dry_run_plans_batches_but_issues_no_update() -> None:
    target = chunk("c1", days_old=30, pages=9)
    connection = FakeConnection(
        [
            UNCOMPRESSED_GUARD,
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (700,)}),
        ]
    )
    settings = config(Path("/tmp"), enforce=False)

    descriptor = backfill.process_chunk(connection, target, settings, cursor_budget=[10])

    assert descriptor["state"] == "planned"
    assert descriptor["pending_rows"] == 700
    assert descriptor["batches_planned"] == 1
    assert statements(connection, "UPDATE ONLY") == []
    assert connection.commits == 0


def test_probe_runs_a_real_update_then_always_rolls_back() -> None:
    target = chunk("c1", days_old=30, pages=4)
    connection = FakeConnection(
        [
            UNCOMPRESSED_GUARD,
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("pg_locks", lambda s, p: {"fetchall": [("relation", "RowExclusiveLock", True, "c1")]}),
            ("UPDATE ONLY", lambda s, p: {"rowcount": 55}),
            ("SELECT count(*)", lambda s, p: {"fetchone": (55,)}),
        ]
    )
    settings = config(Path("/tmp"), probe=True)

    descriptor = backfill.process_chunk(connection, target, settings, cursor_budget=[10])

    assert statements(connection, "UPDATE ONLY")
    assert descriptor["probe"]["rolled_back"] is True
    assert descriptor["probe"]["updated_rows"] == 55
    assert descriptor["probe"]["lock_waits"] == [
        {"locktype": "relation", "mode": "RowExclusiveLock", "granted": True, "relation": "c1"}
    ]
    assert connection.commits == 0
    assert connection.rollbacks >= 1


def test_probe_rolls_back_even_when_the_batch_raises() -> None:
    target = chunk("c1", days_old=30, pages=4)
    connection = FakeConnection(
        [
            UNCOMPRESSED_GUARD,
            ("t.run_key IS NOT NULL", lambda s, p: {"fetchone": (0,)}),
            ("UPDATE ONLY", lambda s, p: RuntimeError("boom")),
            ("SELECT count(*)", lambda s, p: {"fetchone": (55,)}),
        ]
    )
    settings = config(Path("/tmp"), probe=True)

    with pytest.raises(RuntimeError):
        backfill.process_chunk(connection, target, settings, cursor_budget=[10])

    assert connection.rollbacks >= 1
    assert connection.commits == 0


def test_enforce_and_probe_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(backfill.BackfillConfigError, match="mutually exclusive"):
        backfill.config_from_args(_args(enforce=True, probe=True), env(tmp_path))
