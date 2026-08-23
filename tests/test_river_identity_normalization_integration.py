"""Real-database integration tests for river identity normalization (#1339).

Run with the repo's standard opt-in against a throwaway database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_river_identity_normalization_integration.py

``throwaway_database_url`` (tests/conftest.py) creates and drops a
uniquely-named database per TEST, so nothing here can touch a live one and the
schema-mutating cutover tests cannot leak into each other.

ORACLE WARNING — read before trusting a green run of the compression-semantics
subset below. CI's ``real-db-integration`` job runs ``timescale/timescaledb:
pg15-latest``. Production node-27 runs **TimescaleDB 2.10.2 / PG 15.2**, and
every behaviour the cutover depends on is 2.10-specific:

  * ``compress = true`` rejecting all unique DDL (2.10 only),
  * ``ADD CONSTRAINT ... PRIMARY KEY USING INDEX`` being unsupported,
  * foreign-key columns being required in segmentby.

A newer TimescaleDB relaxes several of these. Tests marked
``timescaledb_210`` therefore prove nothing on CI: their oracle is a node-27
throwaway database. Point ``NHMS_INTEGRATION_DATABASE_URL`` at one to get real
evidence, exactly as recorded in
``openspec/changes/river-identity-normalization-backfill/probe-1339-throwaway.md``.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

import scripts.node27_autopipeline as autopipe
from tests.integration_helpers import apply_migrations_from_zero

pytestmark = pytest.mark.integration

NORMALIZED_COLUMNS = (
    "run_key",
    "river_network_version_key",
    "basin_version_key",
    "river_segment_key",
    "variable_e",
    "unit_e",
    "quality_flag_e",
)

_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)
_SEGMENTS = 60
_HOURS = 48


def _connect(database_url: str) -> Any:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    return connection


def _scalar(connection: Any, sql: str, params: Any = None) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    return None if row is None else next(iter(row.values()))


def _seed_authority(connection: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO core.basin VALUES ('b1', 'B1', NULL, NULL, now())
            ON CONFLICT DO NOTHING;
            """
        )
        cursor.execute(
            """
            INSERT INTO core.basin_version
                (basin_version_id, basin_id, version_label, geom, active_flag)
            VALUES ('bv1', 'b1', 'v1',
                    ST_SetSRID(ST_GeomFromText('MULTIPOLYGON(((0 0,0 1,1 1,0 0)))'), 4490), true)
            ON CONFLICT DO NOTHING;
            """
        )
        cursor.execute(
            """
            INSERT INTO core.river_network_version
                (river_network_version_id, basin_version_id, version_label, segment_count)
            VALUES ('rnv1', 'bv1', 'v1', %s) ON CONFLICT DO NOTHING;
            """,
            (_SEGMENTS,),
        )
        cursor.execute(
            """
            INSERT INTO core.river_segment (river_segment_id, river_network_version_id, segment_order)
            SELECT 'seg-' || g, 'rnv1', g FROM generate_series(1, %s) g
            ON CONFLICT DO NOTHING;
            """,
            (_SEGMENTS,),
        )
        cursor.execute(
            """
            INSERT INTO core.model_instance
                (model_id, basin_version_id, river_network_version_id, mesh_version_id,
                 calibration_version_id, shud_code_version, model_package_uri)
            VALUES ('m1', 'bv1', 'rnv1', 'mv1', 'cal1', '1.0', 's3://x')
            ON CONFLICT DO NOTHING;
            """
        )
        cursor.execute(
            """
            INSERT INTO hydro.hydro_run
                (run_id, run_type, scenario_id, model_id, basin_version_id,
                 start_time, end_time, status, run_manifest_uri)
            VALUES ('run1', 'forecast', 'sc', 'm1', 'bv1', now(), now(), 'parsed', 's3://m')
            ON CONFLICT DO NOTHING;
            """
        )


def _seed_facts(connection: Any, *, normalized: bool) -> None:
    """Insert fact rows with the seven columns either filled or left NULL."""
    filled = """
        (SELECT run_key FROM hydro.hydro_run WHERE run_id = 'run1'),
        (SELECT river_network_version_key FROM core.river_network_version
          WHERE river_network_version_id = 'rnv1'),
        (SELECT basin_version_key FROM core.basin_version WHERE basin_version_id = 'bv1'),
        (SELECT river_segment_key FROM core.river_segment
          WHERE river_segment_id = 'seg-' || s AND river_network_version_id = 'rnv1'),
        'q_down', 'm3/s', 'ok'
    """
    empty = "NULL, NULL, NULL, NULL, NULL, NULL, NULL"
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO hydro.river_timeseries
                (run_id, basin_version_id, river_network_version_id, river_segment_id,
                 valid_time, variable, value, unit, quality_flag,
                 run_key, river_network_version_key, basin_version_key, river_segment_key,
                 variable_e, unit_e, quality_flag_e)
            SELECT 'run1', 'bv1', 'rnv1', 'seg-' || s,
                   %s::timestamptz + (h * INTERVAL '1 hour'),
                   'q_down', random() * 10, 'm3/s', 'ok',
                   {filled if normalized else empty}
            FROM generate_series(1, %s) s, generate_series(0, %s) h;
            """,
            (_BASE_TIME, _SEGMENTS, _HOURS - 1),
        )


_MIGRATION_000050 = "000050_river_identity_normalization.sql"


def _forget_migration(database_url: str, version: str) -> None:
    """Drop one row from the applied-migrations ledger so it replays for real."""
    connection = psycopg2.connect(database_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM public.schema_migrations WHERE version = %s", (version,))
            assert cursor.rowcount == 1, f"{version} was never recorded as applied"
    finally:
        connection.close()


@pytest.fixture()
def migrated(throwaway_database_url: str) -> Any:
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# Migration shape (portable across TimescaleDB versions)
# ---------------------------------------------------------------------------


def test_migration_chain_replays_idempotently_and_creates_each_object_once(
    throwaway_database_url: str,
) -> None:
    """000050's SQL is executed TWICE, not skipped the second time.

    ``apply_migrations_from_zero`` consults ``schema_migrations`` first, so a
    plain second call re-executes nothing and would prove idempotency by
    running zero statements. Forgetting the ledger entry for 000050 is what
    makes the replay real.
    """
    apply_migrations_from_zero(throwaway_database_url)
    _forget_migration(throwaway_database_url, _MIGRATION_000050)
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        assert _scalar(
            connection,
            """
            SELECT count(*) FROM pg_attribute a, pg_class c, pg_namespace n
            WHERE c.oid = a.attrelid AND n.oid = c.relnamespace
              AND n.nspname = 'hydro' AND c.relname = 'river_timeseries'
              AND NOT a.attisdropped AND a.attname = ANY(%s)
            """,
            (list(NORMALIZED_COLUMNS),),
        ) == len(NORMALIZED_COLUMNS)

        assert _scalar(
            connection,
            """
            SELECT count(*) FROM pg_type t, pg_namespace n
            WHERE n.oid = t.typnamespace AND n.nspname = 'hydro'
              AND t.typname IN ('river_variable', 'river_unit', 'river_quality_flag')
            """,
        ) == 3

        # Four authority surrogate keys, each GENERATED ALWAYS AS IDENTITY
        # (attidentity = 'a') and each UNIQUE.
        assert _scalar(
            connection,
            """
            SELECT count(*) FROM pg_attribute a, pg_class c
            WHERE c.oid = a.attrelid AND a.attidentity = 'a'
              AND (c.relname, a.attname) IN (
                ('hydro_run', 'run_key'), ('basin_version', 'basin_version_key'),
                ('river_network_version', 'river_network_version_key'),
                ('river_segment', 'river_segment_key'))
            """,
        ) == 4
    finally:
        connection.close()


def test_seven_fact_columns_have_no_stored_default_so_no_rewrite_happened(migrated: Any) -> None:
    """``atthasmissing = false`` is the mechanically checkable pin of the
    no-default choice. A constant default would flip it to true (measured),
    and would also destroy the NULL sentinel the backfill depends on."""
    with migrated.cursor() as cursor:
        cursor.execute(
            """
            SELECT a.attname, a.atthasmissing, a.atthasdef, a.attnotnull
            FROM pg_attribute a, pg_class c, pg_namespace n
            WHERE c.oid = a.attrelid AND n.oid = c.relnamespace
              AND n.nspname = 'hydro' AND c.relname = 'river_timeseries'
              AND a.attname = ANY(%s)
            """,
            (list(NORMALIZED_COLUMNS),),
        )
        rows = {row["attname"]: row for row in cursor.fetchall()}

    assert set(rows) == set(NORMALIZED_COLUMNS)
    for name, row in rows.items():
        assert row["atthasmissing"] is False, f"{name} carries a stored default"
        assert row["atthasdef"] is False, f"{name} carries a default expression"
        assert row["attnotnull"] is False, f"{name} must stay nullable until cutover"


# The one index the migration chain is allowed to put on the normalized fact
# columns, and its exact column tuple. Kept next to the test that enforces it so
# a future migration cannot quietly widen the allowance by editing only the
# assertion. Ordering is the index's own (`indkey`) order; the `valid_time DESC`
# direction is pinned separately, against the migration text, in
# tests/test_migrations.py.
EXPECTED_NORMALIZED_COLUMN_INDEXES = {
    "river_ts_selected_identity_key_valid_time_idx": [
        "run_key",
        "basin_version_key",
        "river_network_version_key",
        "variable_e",
        "valid_time",
    ],
}


def test_exactly_one_discovery_index_and_no_foreign_key_on_the_new_columns(migrated: Any) -> None:
    """FK half: still zero. Index half: re-pinned from zero to exactly one.

    Pin evolution — this assertion has been correct twice and means different
    things each time:

    * **#1339 era (zero-index).** 000050 added the seven columns and
      deliberately added no index at all; its header (000050:79-84) defers the
      whole index design to the read-path switch. "No index touches these
      columns" was the mechanically checkable form of that deferral.
    * **#1341 era (exactly-one-index), current.** 000051 delivers the deferred
      design: one integer discovery index on ``(run_key, basin_version_key,
      river_network_version_key, variable_e, valid_time DESC)``. A count of
      zero is now the failure, so the pin moves to an exact enumeration rather
      than being deleted — the property worth protecting was never "no
      indexes", it was "no index nobody designed".
    * **#1342 will revisit.** Dropping the text columns retires the retained
      text indexes and may add or reshape key-side ones; whoever does that
      updates ``EXPECTED_NORMALIZED_COLUMN_INDEXES`` and this note, and the
      diff shows the decision.

    The FK half is unchanged and must stay zero for a different reason: a
    foreign key on ``basin_version_key`` would make the cutover's compression
    ALTER fail outright, because TimescaleDB 2.10 requires FK columns to be
    covered by segmentby and ``basin_version_key`` is not in the target
    segmentby.
    """
    assert _scalar(
        migrated,
        """
        SELECT count(*) FROM pg_constraint con, pg_attribute a
        WHERE con.conrelid = 'hydro.river_timeseries'::regclass AND con.contype = 'f'
          AND a.attrelid = con.conrelid AND a.attnum = ANY(con.conkey)
          AND a.attname = ANY(%s)
        """,
        (list(NORMALIZED_COLUMNS),),
    ) == 0

    # Every index on the parent hypertable that references at least one
    # normalized column, with its full column list. `indrelid = ...regclass`
    # keeps per-chunk indexes out; `HAVING bool_or(...)` is what restricts the
    # result to indexes this pin is about, while `array_agg` still reports the
    # index's other columns so a tuple change is visible.
    with migrated.cursor() as cursor:
        cursor.execute(
            """
            SELECT ic.relname AS index_name,
                   array_agg(a.attname ORDER BY k.ord) AS columns
            FROM pg_index i
            JOIN pg_class ic ON ic.oid = i.indexrelid
            CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
            WHERE i.indrelid = 'hydro.river_timeseries'::regclass
            GROUP BY ic.relname
            HAVING bool_or(a.attname = ANY(%s))
            """,
            (list(NORMALIZED_COLUMNS),),
        )
        found = {row["index_name"]: list(row["columns"]) for row in cursor.fetchall()}

    # Dict equality, not a membership check: a missing 000051 (empty result), a
    # reordered or truncated column tuple, and a second index someone adds on
    # these columns are each red, and the diff names which.
    assert found == EXPECTED_NORMALIZED_COLUMN_INDEXES


def test_enum_value_sets_cover_every_writer_and_seed_literal(migrated: Any) -> None:
    """The union the migration claims, asserted against the actual writers.

    ``m3 s-1`` is deliberately absent: it existed only in a test fixture, which
    was corrected rather than widening a production type.
    """
    from db.seeds.seed_demo import RIVER_VARIABLES
    from workers.output_parser.parser import UNIT_M3S, VARIABLE_Q_DOWN

    with migrated.cursor() as cursor:
        cursor.execute(
            """
            SELECT t.typname, e.enumlabel FROM pg_type t, pg_enum e, pg_namespace n
            WHERE e.enumtypid = t.oid AND n.oid = t.typnamespace AND n.nspname = 'hydro'
              AND t.typname IN ('river_variable', 'river_unit', 'river_quality_flag')
            """
        )
        labels: dict[str, set[str]] = {}
        for row in cursor.fetchall():
            labels.setdefault(row["typname"], set()).add(row["enumlabel"])

    assert {VARIABLE_Q_DOWN, *RIVER_VARIABLES} <= labels["river_variable"]
    assert {UNIT_M3S, "m"} <= labels["river_unit"]
    # parser.py writes 'ok' by default and 'qc_warning' on a failed QC batch.
    assert {"ok", "qc_warning"} <= labels["river_quality_flag"]
    assert "m3 s-1" not in labels["river_unit"]


def test_migration_chain_never_invokes_verify_or_cutover(migrated: Any) -> None:
    """Auto-applying the cutover on an empty database would give CI a primary
    key and compression settings production does not have."""
    assert _scalar(
        migrated,
        """
        SELECT count(*) FROM pg_proc p, pg_namespace n
        WHERE n.oid = p.pronamespace AND n.nspname = 'hydro'
          AND p.proname IN ('verify_river_identity_normalization',
                            'cutover_river_identity_normalization')
        """,
    ) == 2

    # The chain ran to completion above, so if it had called cutover the pkey
    # would be the integer form. It must still be the original text form.
    assert _scalar(
        migrated,
        """
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid = 'hydro.river_timeseries'::regclass AND contype = 'p'
        """,
    ) == "PRIMARY KEY (run_id, river_network_version_id, river_segment_id, variable, valid_time)"


# ---------------------------------------------------------------------------
# Backfill end-to-end, including re-entry
# ---------------------------------------------------------------------------


def _run_backfill(database_url: str, tmp_path: Any, **env_extra: str) -> dict[str, Any]:
    import json
    import os

    from scripts import node27_river_identity_backfill as backfill

    env = {
        "DATABASE_URL": database_url,
        "NODE27_TIMESERIES_COMPRESSION_LAG_SECONDS": "1",
        "NODE27_RIVER_IDENTITY_BACKFILL_RECEIPT_PATH": str(tmp_path / "receipt.json"),
        "NODE27_RIVER_IDENTITY_BACKFILL_LOCK_PATH": str(tmp_path / "runner.lock"),
        "NODE27_RIVER_IDENTITY_BACKFILL_BATCH_PAGES": "4",
        "NODE27_RIVER_IDENTITY_BACKFILL_BATCH_SLEEP_MS": "0",
        **env_extra,
    }
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        exit_code = backfill.main(
            ["--enforce"],
            now_utc=datetime.now(UTC) + timedelta(days=3650),
        )
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    receipt = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    receipt["_exit_code"] = exit_code
    return receipt


def test_backfill_fills_every_row_and_a_second_pass_changes_nothing(
    throwaway_database_url: str, tmp_path: Any
) -> None:
    """Re-entrancy, proven by running the whole thing twice.

    ``now_utc`` is pushed far into the future so every chunk is terminal by the
    shared lag criterion; otherwise the toy data (dated 2026-01-01) would be
    processed or skipped depending on when the suite runs.
    """
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_facts(connection, normalized=False)
        total = _scalar(connection, "SELECT count(*) FROM hydro.river_timeseries")
        assert total == _SEGMENTS * _HOURS

        first = _run_backfill(throwaway_database_url, tmp_path)
        assert first["outcome"] == "clean", first
        assert first["totals"]["updated_rows"] == total
        assert first["totals"]["pending_rows"] == 0

        with connection.cursor() as cursor:
            cursor.execute("SELECT * FROM hydro.verify_river_identity_normalization()")
            verified = cursor.fetchone()
        for column in NORMALIZED_COLUMNS:
            assert verified[f"null_{column}"] == 0
        assert verified["equality_audit_divergent"] == 0

        second = _run_backfill(throwaway_database_url, tmp_path)
        assert second["outcome"] == "clean", second
        assert second["totals"]["updated_rows"] == 0
        assert second["totals"]["candidate_rows"] == 0
    finally:
        connection.close()


def test_backfill_resumes_after_interruption_with_the_cursor_discarded(
    throwaway_database_url: str, tmp_path: Any
) -> None:
    """Interrupt via the batch budget, then resume with the cursor thrown away.

    Losing the persisted cursor must degrade to a full rescan with an identical
    result, because the NULL sentinel — not the cursor — is what makes the
    UPDATE idempotent. The second invocation writes to a different receipt
    path, so it genuinely starts with no cursor at all.
    """
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_facts(connection, normalized=False)
        total = _scalar(connection, "SELECT count(*) FROM hydro.river_timeseries")

        partial = _run_backfill(
            throwaway_database_url,
            tmp_path,
            NODE27_RIVER_IDENTITY_BACKFILL_MAX_BATCHES="1",
        )
        done_after_first = partial["totals"]["updated_rows"]
        assert 0 < done_after_first < total, partial

        cursorless = tmp_path / "no-cursor"
        cursorless.mkdir()
        resumed = _run_backfill(throwaway_database_url, cursorless)
        assert resumed["outcome"] == "clean"
        assert resumed["chunks"][0]["resumed_from_page"] == 0
        assert _scalar(
            connection, "SELECT count(*) FROM hydro.river_timeseries WHERE run_key IS NULL"
        ) == 0
        # It only touched what was still NULL — no double-application.
        assert resumed["totals"]["updated_rows"] == total - done_after_first
    finally:
        connection.close()


def test_a_budget_capped_backfill_makes_progress_on_every_invocation(
    throwaway_database_url: str, tmp_path: Any
) -> None:
    """The cursor must be CONSUMED, not just published.

    Every invocation here runs with a budget of one batch and shares one
    receipt path, which is how the runner is actually scheduled. If the cursor
    is write-only, invocation 2 onwards re-scans the finished prefix, spends
    its single batch on zero candidates, and the run stalls forever at the
    first batch's worth of rows — reporting ``clean``/``deferred``/exit 0 the
    whole time.
    """
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_facts(connection, normalized=False)
        total = _scalar(connection, "SELECT count(*) FROM hydro.river_timeseries")

        remaining = total
        progress: list[int] = []
        cursors: list[dict[str, Any]] = []
        for _ in range(60):
            receipt = _run_backfill(
                throwaway_database_url,
                tmp_path,
                NODE27_RIVER_IDENTITY_BACKFILL_MAX_BATCHES="1",
            )
            assert receipt["outcome"] == "clean", receipt
            progress.append(receipt["totals"]["updated_rows"])
            cursors.append(receipt["cursor"])
            remaining = _scalar(
                connection,
                "SELECT count(*) FROM hydro.river_timeseries WHERE run_key IS NULL",
            )
            if remaining == 0:
                break

        assert remaining == 0, (
            f"stalled at {remaining} NULL rows after {len(progress)} single-batch "
            f"invocations; per-invocation updates={progress}, cursors={cursors[-3:]}"
        )
        assert len(progress) > 1, "the fixture is too small to exercise re-entry"
        # No row was ever written twice: the per-invocation totals partition
        # the table exactly.
        assert sum(progress) == total
    finally:
        connection.close()


def test_backfill_stops_fail_closed_and_names_the_unresolvable_rows(
    throwaway_database_url: str, tmp_path: Any
) -> None:
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_facts(connection, normalized=False)
        # An orphan run_id: no authority row can ever resolve it.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE hydro.river_timeseries SET run_id = 'run-that-does-not-exist'
                WHERE river_segment_id = 'seg-3'
                """
            )

        receipt = _run_backfill(throwaway_database_url, tmp_path)

        assert receipt["outcome"] == "stopped"
        assert receipt["stop"]["stage"] == "shortfall"
        assert receipt["stop"]["unmatched_rows"] > 0
        assert receipt["stop"]["unmappable_rows"] == 0
        assert receipt["_exit_code"] == 1
        # Nothing in the offending batch was recorded as progress.
        assert _scalar(
            connection,
            "SELECT count(*) FROM hydro.river_timeseries WHERE run_id = 'run-that-does-not-exist' "
            "AND run_key IS NOT NULL",
        ) == 0
        # totals.updated_rows is a rows-in-the-table claim, checked against the
        # table itself: the rolled-back batch must not appear in it.
        persisted = _scalar(
            connection, "SELECT count(*) FROM hydro.river_timeseries WHERE run_key IS NOT NULL"
        )
        assert receipt["totals"]["updated_rows"] == persisted
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Compression-semantics subset — node-27 throwaway DB is the ONLY valid oracle
# ---------------------------------------------------------------------------


@pytest.mark.timescaledb_210
def test_cutover_refuses_while_any_chunk_is_compressed(
    throwaway_database_url: str,
) -> None:
    """Oracle: node-27 throwaway (TimescaleDB 2.10.2). CI's pg15-latest does
    not prove this."""
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_facts(connection, normalized=True)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT compress_chunk(format('%I.%I', chunk_schema, chunk_name)::regclass)
                FROM timescaledb_information.chunks
                WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries'
                ORDER BY range_start LIMIT 1
                """
            )
        assert _scalar(
            connection,
            "SELECT compressed_chunk_count FROM hydro.verify_river_identity_normalization()",
        ) == 1

        with pytest.raises(psycopg2.errors.RaiseException, match="cutover refused"):
            with connection.cursor() as cursor:
                cursor.execute("SELECT hydro.cutover_river_identity_normalization()")
    finally:
        connection.close()


@pytest.mark.timescaledb_210
def test_cutover_with_a_null_left_raises_and_changes_absolutely_nothing(
    throwaway_database_url: str,
) -> None:
    """The mandatory negative path (design D4 / tasks 2.2).

    "Single transaction" is the load-bearing claim of the cutover function.
    This is the test that makes it observable: leave one NULL, call the
    function, and assert the three things a partial application would have
    already changed by the time VALIDATE raises — compression still enabled,
    the text foreign key still present, the old primary key still in place.
    """
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_facts(connection, normalized=True)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE hydro.river_timeseries SET basin_version_key = NULL
                WHERE river_segment_id = 'seg-7' AND valid_time = %s
                """,
                (_BASE_TIME,),
            )

        before = _catalog_snapshot(connection)
        assert before["compression_enabled"] is True
        assert before["text_fk_count"] == 1
        assert before["pkey"].startswith("PRIMARY KEY (run_id,")

        with pytest.raises(psycopg2.Error) as excinfo:
            with connection.cursor() as cursor:
                cursor.execute("SELECT hydro.cutover_river_identity_normalization()")
        assert "is violated by some row" in str(excinfo.value)

        after = _catalog_snapshot(connection)
        assert after == before, "cutover was not atomic: catalog changed despite the abort"
        # And no half-built CHECK constraints were left behind.
        assert _scalar(
            connection,
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'hydro.river_timeseries'::regclass AND contype = 'c'",
        ) == 0
    finally:
        connection.close()


@pytest.mark.timescaledb_210
def test_cutover_positive_path_then_compression_round_trip_preserves_every_row(
    throwaway_database_url: str,
) -> None:
    """Oracle: node-27 throwaway. Mirrors probe log step d-6 end to end."""
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_facts(connection, normalized=True)
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE before_rt AS SELECT * FROM hydro.river_timeseries")

        with connection.cursor() as cursor:
            cursor.execute("SELECT hydro.cutover_river_identity_normalization()")

        after = _catalog_snapshot(connection)
        assert after["pkey"] == (
            "PRIMARY KEY (run_key, river_network_version_key, river_segment_key, "
            "variable_e, valid_time)"
        )
        assert after["text_fk_count"] == 0
        assert after["compression_enabled"] is True
        assert after["segmentby"] == "run_key,river_network_version_key,river_segment_key"
        assert after["orderby"] == "variable_e,valid_time"
        assert after["not_null_count"] == len(NORMALIZED_COLUMNS)

        # Idempotent: a second call is a no-op, not a confusing mid-chain error.
        with connection.cursor() as cursor:
            cursor.execute("SELECT hydro.cutover_river_identity_normalization()")
        assert _catalog_snapshot(connection) == after

        # AC-4 round trip under the new settings.
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT compress_chunk(format('%I.%I', chunk_schema, chunk_name)::regclass)
                FROM timescaledb_information.chunks
                WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries'
                """
            )
            cursor.execute(
                """
                SELECT decompress_chunk(format('%I.%I', chunk_schema, chunk_name)::regclass)
                FROM timescaledb_information.chunks
                WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries'
                  AND is_compressed
                """
            )

        assert _scalar(
            connection,
            "SELECT count(*) FROM (SELECT * FROM before_rt EXCEPT "
            "SELECT * FROM hydro.river_timeseries) x",
        ) == 0
        assert _scalar(
            connection,
            "SELECT count(*) FROM (SELECT * FROM hydro.river_timeseries EXCEPT "
            "SELECT * FROM before_rt) y",
        ) == 0
    finally:
        connection.close()


@pytest.mark.timescaledb_210
def test_backfill_skips_a_compressed_chunk_and_lists_it_in_the_receipt(
    throwaway_database_url: str, tmp_path: Any
) -> None:
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_facts(connection, normalized=False)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT compress_chunk(format('%I.%I', chunk_schema, chunk_name)::regclass)
                FROM timescaledb_information.chunks
                WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries'
                ORDER BY range_start LIMIT 1
                """
            )

        receipt = _run_backfill(throwaway_database_url, tmp_path)

        assert receipt["totals"]["chunks_skipped_compressed"] >= 1
        skipped = [c for c in receipt["chunks"] if c["state"] == "skipped_compressed"]
        assert skipped, receipt["chunks"]
        assert "no DML against compressed storage" in skipped[0]["skip_reason"]
    finally:
        connection.close()


def _catalog_snapshot(connection: Any) -> dict[str, Any]:
    return {
        "compression_enabled": _scalar(
            connection,
            "SELECT compression_enabled FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries'",
        ),
        "segmentby": _scalar(
            connection,
            "SELECT string_agg(attname, ',' ORDER BY segmentby_column_index) "
            "FROM timescaledb_information.compression_settings "
            "WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries' "
            "AND segmentby_column_index IS NOT NULL",
        ),
        "orderby": _scalar(
            connection,
            "SELECT string_agg(attname, ',' ORDER BY orderby_column_index) "
            "FROM timescaledb_information.compression_settings "
            "WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries' "
            "AND orderby_column_index IS NOT NULL",
        ),
        "text_fk_count": _scalar(
            connection,
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'hydro.river_timeseries'::regclass AND contype = 'f'",
        ),
        "pkey": _scalar(
            connection,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'hydro.river_timeseries'::regclass AND contype = 'p'",
        ),
        "not_null_count": _scalar(
            connection,
            "SELECT count(*) FROM pg_attribute "
            "WHERE attrelid = 'hydro.river_timeseries'::regclass AND attnotnull "
            "AND attname = ANY(%s)",
            (list(NORMALIZED_COLUMNS),),
        ),
    }


# ---------------------------------------------------------------------------
# #1674 — the autopipeline completeness predicate, on a real database
# ---------------------------------------------------------------------------
#
# `_already_ingested_runs` decides, once per tick, which runs may be skipped.
# Getting it wrong in either direction is expensive and invisible to unit tests:
# too strict re-sends the per-cycle forcing handoff for hundreds of finished
# runs (the #1674 production storm), too loose leaves a genuinely incomplete run
# un-ingested. The predicate is pure SQL over two real tables with a real
# GENERATED IDENTITY key and real NULLs, so the oracle is a database.

_SUPERSEDED_RUN = "run-superseded"


def _seed_run(
    connection: Any,
    run_id: str,
    *,
    status: str,
    init_state_id: str | None = None,
) -> None:
    """One `hydro.hydro_run` row at ``status``.

    ``run_key`` is `GENERATED ALWAYS AS IDENTITY` (migration 000050) and so must
    never appear in the INSERT list; the database assigns it, and the fact rows
    below read it back. ``_seed_authority`` covers the FK targets (model
    instance, basin version) and hardcodes a single run at 'parsed', which is
    why the multi-run, multi-status seeding here is its own helper.

    ``init_state_id`` is nullable in the schema but has to be settable for the
    recompute-detection scenario: `_ingested_run_is_current` compares it to the
    manifest, and NULL on both sides would make that comparison vacuous.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO hydro.hydro_run
                (run_id, run_type, scenario_id, model_id, basin_version_id,
                 start_time, end_time, status, run_manifest_uri, init_state_id)
            VALUES (%s, 'forecast', 'sc', 'm1', 'bv1', now(), now(), %s, 's3://m', %s);
            """,
            (run_id, status, init_state_id),
        )


def _seed_run_facts(connection: Any, run_id: str, *, normalized: bool) -> None:
    """A handful of `river_timeseries` rows for ``run_id``.

    ``normalized=False`` reproduces the legacy population: text identity filled,
    all seven surrogate/enum columns NULL, exactly like the rows the 000051
    backfill runner had to skip inside already-compressed chunks. A few rows are
    enough — the predicate is an existence test, not a volume test.
    """
    filled = """
        (SELECT run_key FROM hydro.hydro_run WHERE run_id = %(run_id)s),
        (SELECT river_network_version_key FROM core.river_network_version
          WHERE river_network_version_id = 'rnv1'),
        (SELECT basin_version_key FROM core.basin_version WHERE basin_version_id = 'bv1'),
        (SELECT river_segment_key FROM core.river_segment
          WHERE river_segment_id = 'seg-' || s AND river_network_version_id = 'rnv1'),
        'q_down', 'm3/s', 'ok'
    """
    empty = "NULL, NULL, NULL, NULL, NULL, NULL, NULL"
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO hydro.river_timeseries
                (run_id, basin_version_id, river_network_version_id, river_segment_id,
                 valid_time, variable, value, unit, quality_flag,
                 run_key, river_network_version_key, basin_version_key, river_segment_key,
                 variable_e, unit_e, quality_flag_e)
            SELECT %(run_id)s, 'bv1', 'rnv1', 'seg-' || s,
                   %(base_time)s::timestamptz + (h * INTERVAL '1 hour'),
                   'q_down', random() * 10, 'm3/s', 'ok',
                   {filled if normalized else empty}
            FROM generate_series(1, 3) s, generate_series(0, 2) h;
            """,
            {"run_id": run_id, "base_time": _BASE_TIME},
        )


def _compress_all_river_chunks(connection: Any) -> int:
    """Compress every `river_timeseries` chunk; return how many were compressed."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT compress_chunk(format('%I.%I', chunk_schema, chunk_name)::regclass)
            FROM timescaledb_information.chunks
            WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries'
              AND NOT is_compressed
            """
        )
        return cursor.rowcount


def test_already_ingested_counts_a_published_run_with_null_key_rows_in_a_compressed_chunk(
    throwaway_database_url: str,
) -> None:
    """#1674 (i): the production failure condition, reproduced end to end.

    Compression is fidelity to how the rows got this way, NOT what makes the
    predicate fail: a NULL `run_key` misses a key join whether its chunk is
    compressed or not. Compressed storage is why the 000051 backfill could not
    fill those keys and why they are permanently NULL, which is the state this
    test asserts against.

    Before the fix this run was absent from the result — the inner join dropped
    it — so every tick re-sent its per-cycle forcing handoff into a compressed
    `met` chunk and was correctly rejected, 544 times per tick.
    """
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_run(connection, "legacy-published", status="published")
        _seed_run(connection, _SUPERSEDED_RUN, status="superseded")
        _seed_run_facts(connection, "legacy-published", normalized=False)
        assert _compress_all_river_chunks(connection) >= 1
        assert (
            _scalar(
                connection,
                "SELECT count(*) FROM hydro.river_timeseries "
                "WHERE run_id = 'legacy-published' AND run_key IS NOT NULL",
            )
            == 0
        )

        ingested = autopipe._already_ingested_runs(
            throwaway_database_url,
            ["legacy-published", _SUPERSEDED_RUN],
            object_store_root=None,
        )

        assert "legacy-published" in ingested
        # The superseded run is retired by the first statement, unconditionally.
        assert _SUPERSEDED_RUN in ingested
    finally:
        connection.close()


def test_already_ingested_excludes_a_parsed_run_whose_only_rows_are_null_key(
    throwaway_database_url: str,
) -> None:
    """#1674 (ii): authority state relaxes 'published' only.

    A 'parsed' run with no key-visible row means the parser chain did not
    finish; keeping it incomplete is what makes the pipeline retry it.
    """
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_run(connection, "half-parsed", status="parsed")
        _seed_run(connection, _SUPERSEDED_RUN, status="superseded")
        _seed_run_facts(connection, "half-parsed", normalized=False)

        ingested = autopipe._already_ingested_runs(
            throwaway_database_url,
            ["half-parsed", _SUPERSEDED_RUN],
            object_store_root=None,
        )

        assert "half-parsed" not in ingested
        assert _SUPERSEDED_RUN in ingested
    finally:
        connection.close()


def test_already_ingested_counts_a_parsed_run_with_key_matched_rows(
    throwaway_database_url: str,
) -> None:
    """#1674 (iii): the post-dual-write normal path is unchanged."""
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_run(connection, "fresh-parsed", status="parsed")
        _seed_run(connection, _SUPERSEDED_RUN, status="superseded")
        _seed_run_facts(connection, "fresh-parsed", normalized=True)
        assert (
            _scalar(
                connection,
                "SELECT count(*) FROM hydro.river_timeseries rt "
                "JOIN hydro.hydro_run h ON h.run_key = rt.run_key "
                "WHERE h.run_id = 'fresh-parsed'",
            )
            > 0
        )

        ingested = autopipe._already_ingested_runs(
            throwaway_database_url,
            ["fresh-parsed", _SUPERSEDED_RUN],
            object_store_root=None,
        )

        assert "fresh-parsed" in ingested
        assert _SUPERSEDED_RUN in ingested
    finally:
        connection.close()


def test_already_ingested_counts_a_published_run_with_no_fact_rows_at_all(
    throwaway_database_url: str,
) -> None:
    """#1674 (iv): a retention-emptied published run is not re-ingested.

    This is the deliberate behaviour change against pre-#1674 code, which would
    have re-run the whole handoff for a run whose chunks retention dropped on
    purpose. 'published' is the authority fact that the data was there.
    """
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_run(connection, "emptied-published", status="published")
        _seed_run(connection, _SUPERSEDED_RUN, status="superseded")

        ingested = autopipe._already_ingested_runs(
            throwaway_database_url,
            ["emptied-published", _SUPERSEDED_RUN],
            object_store_root=None,
        )

        assert "emptied-published" in ingested
        assert _SUPERSEDED_RUN in ingested
    finally:
        connection.close()


def _write_manifest(object_store_root: Path, run_id: str, state_id: str) -> None:
    path = object_store_root / "runs" / run_id / "input" / "manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"initial_state": {"state_id": state_id}}), encoding="utf-8")


def test_already_ingested_recompute_detection_on_a_legacy_run_is_init_state_only(
    throwaway_database_url: str, tmp_path: Path
) -> None:
    """#1674 (v): the recorded residual of design D1, pinned so it cannot drift.

    On a legacy NULL-key run `MAX(rt.created_at)` aggregates zero rows, so
    `parsed_at` is NULL and the product-mtime comparison cannot run. What
    survives is the init_state comparison: a warm-start recompute that changes
    the initial state is still detected and re-ingested; a rewrite that keeps
    the same initial state is not. That gap is bounded (the cohort ages out with
    retention) and deliberate — `hydro_run.updated_at` is not an acceptable
    stand-in for a parse timestamp, since every tick's register upsert bumps it.
    """
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_run(connection, "legacy-published", status="published", init_state_id="state-a")
        _seed_run_facts(connection, "legacy-published", normalized=False)
        assert _compress_all_river_chunks(connection) >= 1

        # Manifest disagrees with the DB: a different initial state means the
        # object store now holds a different run. Not complete, re-ingest it.
        _write_manifest(tmp_path, "legacy-published", "state-b")
        assert (
            autopipe._already_ingested_runs(
                throwaway_database_url,
                ["legacy-published"],
                object_store_root=tmp_path,
            )
            == set()
        )

        # Manifest agrees, and the products were rewritten after any plausible
        # parse time. With parsed_at NULL there is nothing to compare the mtime
        # against, so the run stays complete: the residual, made visible.
        _write_manifest(tmp_path, "legacy-published", "state-a")
        product = tmp_path / "runs" / "legacy-published" / "output" / "rivqdown.csv"
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_text("time,q\n", encoding="utf-8")
        future = datetime.now(tz=UTC).timestamp() + 3600
        os.utime(product, (future, future))
        assert autopipe._run_product_mtime(tmp_path, "legacy-published") >= future

        assert autopipe._already_ingested_runs(
            throwaway_database_url,
            ["legacy-published"],
            object_store_root=tmp_path,
        ) == {"legacy-published"}
    finally:
        connection.close()


def test_already_ingested_recompute_detection_compares_product_mtime_to_parsed_at(
    throwaway_database_url: str, tmp_path: Path
) -> None:
    """#1674 (vi): the non-NULL `parsed_at` plumbing that (i)-(v) cannot observe.

    Scenarios (i)-(iv) pass ``object_store_root=None``, so `_ingested_run_is_current`
    returns before `parsed_at` is ever read, and (v) is a NULL-`parsed_at` cohort by
    construction. Outside the SQL string pin nothing holds `MAX(rt.created_at)` to a
    real timestamp on the post-cutover population — the population that actually gets
    recomputed. Under the pre-#1674 inner join a row-bearing run structurally
    guaranteed a non-NULL aggregate; the #1674 LEFT JOIN makes NULL a legal output
    state, so a refactor could keep the literal `MAX(rt.created_at) AS parsed_at` and
    still hand every key-visible run a NULL, silently disabling recompute detection.

    That is exactly what this test bites on: with `parsed_at` NULL,
    `_ingested_run_is_current` short-circuits to True, the run comes back complete,
    and the first assertion below (products rewritten AFTER the parse must not be
    skipped) fails.
    """
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_run(connection, "keyed-published", status="published", init_state_id="state-a")
        _seed_run(connection, _SUPERSEDED_RUN, status="superseded")
        _seed_run_facts(connection, "keyed-published", normalized=True)
        assert (
            _scalar(
                connection,
                "SELECT count(*) FROM hydro.river_timeseries "
                "WHERE run_id = 'keyed-published' AND run_key IS NOT NULL",
            )
            > 0
        )
        assert (
            _scalar(
                connection,
                "SELECT max(rt.created_at) FROM hydro.river_timeseries rt "
                "JOIN hydro.hydro_run h ON h.run_key = rt.run_key "
                "WHERE h.run_id = 'keyed-published'",
            )
            is not None
        )

        # Manifest AGREES with the DB initial state, so the init_state comparison
        # passes and detection falls through to the mtime branch — the one branch
        # only a non-NULL `parsed_at` can reach.
        _write_manifest(tmp_path, "keyed-published", "state-a")
        manifest = tmp_path / "runs" / "keyed-published" / "input" / "manifest.json"
        product = tmp_path / "runs" / "keyed-published" / "output" / "rivqdown.csv"
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_text("time,q\n", encoding="utf-8")
        now = datetime.now(tz=UTC).timestamp()

        # Products rewritten after the parse: the warm-start recompute path. Not
        # complete, re-ingest it.
        os.utime(product, (now + 3600, now + 3600))
        assert autopipe._run_product_mtime(tmp_path, "keyed-published") >= now + 3600
        assert autopipe._already_ingested_runs(
            throwaway_database_url,
            ["keyed-published", _SUPERSEDED_RUN],
            object_store_root=tmp_path,
        ) == {_SUPERSEDED_RUN}

        # Products older than the parse: nothing was recomputed, stay skipped. The
        # manifest is back-dated too because `_run_product_mtime` maxes over it as
        # well; leaving it at write time (~now, i.e. after the fact rows were
        # inserted) would race the +1s tolerance against the database clock.
        os.utime(product, (now - 3600, now - 3600))
        os.utime(manifest, (now - 3600, now - 3600))
        assert autopipe._already_ingested_runs(
            throwaway_database_url,
            ["keyed-published", _SUPERSEDED_RUN],
            object_store_root=tmp_path,
        ) == {"keyed-published", _SUPERSEDED_RUN}
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# #1781 — the decline record as a status-independent exclusion
# ---------------------------------------------------------------------------
#
# The oracle has to be a real database for two reasons unit tests cannot cover:
# the suppression must hold for a run at `succeeded`, which the completeness
# statement never returns at all (28 of the 88 blocked runs on node-27 are in
# that state), and `product_mtime` has to survive a DOUBLE PRECISION round trip
# through PostgreSQL bit-for-bit or the key stops matching and the retry loop
# comes back.


def _record_decline(connection: Any, run_id: str, init_state_id: str, product_mtime: float) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO ops.ingest_recompute_decline
                (run_id, init_state_id, product_mtime, reason_code)
            VALUES (%s, %s, %s, 'HANDOFF_APPLY_COMPRESSED_CHUNK_BLOCKED')
            ON CONFLICT DO NOTHING
            """,
            (run_id, init_state_id, product_mtime),
        )


def _write_recomputed_product(object_store_root: Path, run_id: str, state_id: str) -> float:
    """Manifest + a product file whose mtime is newer than any plausible parse.

    This is the production shape being pinned: without a decline record these
    runs are (correctly) detected as recomputed and re-sent every tick.
    """
    _write_manifest(object_store_root, run_id, state_id)
    product = object_store_root / "runs" / run_id / "output" / "rivqdown.csv"
    product.parent.mkdir(parents=True, exist_ok=True)
    product.write_text("time,q\n", encoding="utf-8")
    future = datetime.now(tz=UTC).timestamp() + 3600
    os.utime(product, (future, future))
    mtime = autopipe._run_product_mtime(object_store_root, run_id)
    assert mtime is not None
    return mtime


def test_matching_decline_record_suppresses_a_published_and_a_succeeded_run(
    throwaway_database_url: str, tmp_path: Path
) -> None:
    """Both cohorts, in one call, because they reach the exclusion by different
    routes: the `published` run is row-bearing and would be reopened by the
    mtime comparison, while the `succeeded` run never appears in the
    completeness statement's result at all — which is exactly why the
    suppression cannot live inside `_ingested_run_is_current`.
    """
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_run(connection, "declined-published", status="published", init_state_id="state-a")
        _seed_run(connection, "declined-succeeded", status="succeeded", init_state_id="state-a")
        # Only the published run gets fact rows: a 'succeeded' run has never
        # been parsed, so it has none, and that is the point.
        _seed_run_facts(connection, "declined-published", normalized=True)

        published_mtime = _write_recomputed_product(tmp_path, "declined-published", "state-a")
        succeeded_mtime = _write_recomputed_product(tmp_path, "declined-succeeded", "state-a")

        # Non-vacuity: without the records, neither run is skipped.
        assert (
            autopipe._already_ingested_runs(
                throwaway_database_url,
                ["declined-published", "declined-succeeded"],
                object_store_root=tmp_path,
            )
            == set()
        )

        _record_decline(connection, "declined-published", "state-a", published_mtime)
        _record_decline(connection, "declined-succeeded", "state-a", succeeded_mtime)

        assert autopipe._already_ingested_runs(
            throwaway_database_url,
            ["declined-published", "declined-succeeded"],
            object_store_root=tmp_path,
        ) == {"declined-published", "declined-succeeded"}
    finally:
        connection.close()


def test_a_newer_product_reopens_a_declined_run(throwaway_database_url: str, tmp_path: Path) -> None:
    """The key IS the reopen condition. An operator who decompresses the chunk
    and has the products regenerated must get the run back without touching the
    decline table; a terminal state that could not be reopened by new evidence
    would be a permanent data hole.
    """
    apply_migrations_from_zero(throwaway_database_url)
    connection = _connect(throwaway_database_url)
    try:
        _seed_authority(connection)
        _seed_run(connection, "reopened-published", status="published", init_state_id="state-a")
        _seed_run_facts(connection, "reopened-published", normalized=True)

        declined_mtime = _write_recomputed_product(tmp_path, "reopened-published", "state-a")
        _record_decline(connection, "reopened-published", "state-a", declined_mtime)
        assert autopipe._already_ingested_runs(
            throwaway_database_url,
            ["reopened-published"],
            object_store_root=tmp_path,
        ) == {"reopened-published"}

        # node-22 regenerates the products: same run, same initial state, newer
        # mtime. The stored record no longer describes what is on disk.
        product = tmp_path / "runs" / "reopened-published" / "output" / "rivqdown.csv"
        os.utime(product, (declined_mtime + 60.0, declined_mtime + 60.0))
        # Read the mtime back rather than assuming the value just written: the
        # stored st_mtim is nanoseconds and the float is what `stat()` derives
        # from it, which is exactly why the match is on the stat-derived value.
        later = autopipe._run_product_mtime(tmp_path, "reopened-published")
        assert later is not None and later > declined_mtime

        assert (
            autopipe._already_ingested_runs(
                throwaway_database_url,
                ["reopened-published"],
                object_store_root=tmp_path,
            )
            == set()
        )

        # And the newly blocked regeneration adds its own record rather than
        # replacing the old one — the table accumulates, so a float-equality
        # miss costs at most another blocked tick.
        _record_decline(connection, "reopened-published", "state-a", later)
        assert (
            _scalar(
                connection,
                "SELECT count(*) FROM ops.ingest_recompute_decline WHERE run_id = 'reopened-published'",
            )
            == 2
        )
        assert autopipe._already_ingested_runs(
            throwaway_database_url,
            ["reopened-published"],
            object_store_root=tmp_path,
        ) == {"reopened-published"}
    finally:
        connection.close()
