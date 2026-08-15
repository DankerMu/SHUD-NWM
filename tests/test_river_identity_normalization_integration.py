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

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

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


def test_no_foreign_key_or_index_was_added_on_the_new_columns(migrated: Any) -> None:
    """A foreign key on basin_version_key would make the cutover's compression
    ALTER fail outright: TimescaleDB 2.10 requires FK columns to be covered by
    segmentby, and basin_version_key is not in the target segmentby."""
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

    assert _scalar(
        migrated,
        """
        SELECT count(*) FROM pg_index i, pg_attribute a
        WHERE i.indrelid = 'hydro.river_timeseries'::regclass
          AND a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
          AND a.attname = ANY(%s)
        """,
        (list(NORMALIZED_COLUMNS),),
    ) == 0


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
