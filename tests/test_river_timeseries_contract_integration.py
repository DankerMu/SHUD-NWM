"""Real-database coverage of the river contract migration 000060 (#1988, task 6.2).

Run with the repo's standard opt-in against a throwaway database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_river_timeseries_contract_integration.py

``throwaway_database_url`` (tests/conftest.py) creates and drops a
uniquely-named database per TEST. That is mandatory here, not a preference:
every case below mutates SCHEMA (it drops a table, two functions and a column),
so a session-scoped database would leak the contracted catalog into every later
test in the run.

Oracle note. Every case but one asserts through plain PostgreSQL DDL,
``to_regclass`` and the PUBLIC ``timescaledb_information`` views, all of which
any PG 15 / TimescaleDB server proves, so they stay in CI's ordinary
integration lane. The one exception is
``test_contract_drops_a_legacy_hypertable_carrying_compressed_chunks``: a
compressed chunk's ``_timescaledb_internal.compress_hyper_*`` relation is not
exposed by ``timescaledb_information``, so naming it means reading
``_timescaledb_catalog.chunk.compressed_chunk_id`` — TimescaleDB-internal
catalog shape, version-coupled rather than PostgreSQL DDL. That case therefore
carries ``timescaledb_210`` and routes to a node-27 throwaway DB (TimescaleDB
2.10.2 / PG 15.2); CI deselects it with
``-m "integration and not timescaledb_210"``. Keep that split: an unmarked case
reaching into ``_timescaledb_catalog`` would silently assert 2.10.2-shaped
internals against whatever version CI's ``pg15-latest`` happens to be.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from tests.integration_helpers import (
    FORECAST_RUN_ID,
    HINDCAST_RUN_ID,
    apply_migrations_from_zero,
    seed_issue_126_data,
)

pytestmark = pytest.mark.integration

#: The file 000060 ships as, i.e. the ledger key `record_migration` writes.
CONTRACT_MIGRATION = "000060_river_timeseries_contract.sql"

#: `spec.md:62`'s "final three-index shape" on the narrow hypertable. Names, not
#: a count: a migration that swapped one index for another would keep the count.
NARROW_INDEXES = (
    "river_timeseries_narrow_pkey",
    "river_ts_run_discovery_key_idx",
    "river_ts_segment_time_key_idx",
)

#: 000060 pins its retention floor at 21 days (`GREATEST(21, GUC)`). The tests
#: place runs relative to that floor rather than restating the SQL's arithmetic.
RETENTION_FLOOR_DAYS = 21

#: Day offsets that place copies of the seeded narrow facts into the legacy
#: hypertable. Its `chunk_time_interval` is 3 days (000058), so 30-day steps
#: cannot collapse two offsets into one chunk: three offsets, three chunks.
#: Without this the legacy table 000060 drops is EMPTY — zero chunks, zero rows
#: — and `spec.md:54`'s "regardless of its remaining chunk count" has no
#: execution evidence at all. `seed_issue_126_data` writes only the narrow
#: table (`tests/integration_helpers.py:insert_river_timeseries_dual_written`).
LEGACY_HISTORY_DAY_OFFSETS = (0, 30, 60)

#: Legacy rows the seed below writes: one per seeded narrow row per offset.
LEGACY_HISTORY_ROWS = 4 * len(LEGACY_HISTORY_DAY_OFFSETS)

_SEED_LEGACY_HISTORY = """
    INSERT INTO hydro.river_timeseries_legacy (
        run_id, basin_version_id, river_network_version_id, river_segment_id,
        valid_time, lead_time_hours, variable, value, unit, quality_flag,
        run_key, basin_version_key, river_network_version_key, river_segment_key,
        variable_e, unit_e, quality_flag_e, created_at
    )
    SELECT h.run_id, h.basin_version_id, rs.river_network_version_id, rs.river_segment_id,
           rt.valid_time - make_interval(days => o.offset_days),
           rt.lead_time_hours, rt.variable_e::text, rt.value,
           rt.unit_e::text, rt.quality_flag_e::text,
           rt.run_key, rt.basin_version_key, rt.river_network_version_key,
           rt.river_segment_key, rt.variable_e, rt.unit_e, rt.quality_flag_e, rt.created_at
    FROM hydro.river_timeseries rt
    JOIN hydro.hydro_run h ON h.run_key = rt.run_key
    JOIN core.river_segment rs ON rs.river_segment_key = rt.river_segment_key
    CROSS JOIN unnest(%s::integer[]) AS o(offset_days)
"""

#: The legacy hypertable's chunk relations, from the PUBLIC information view.
#: Every unmarked case here reads this one and `to_regclass`, which is what lets
#: them stay in CI's lane; the internal catalog below is the marked case's.
_LEGACY_CHUNK_RELATIONS = """
    SELECT quote_ident(chunk_schema) || '.' || quote_ident(chunk_name) AS relname
    FROM timescaledb_information.chunks
    WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries_legacy'
    ORDER BY 1
"""

#: The `_timescaledb_internal.compress_hyper_*` relations holding a compressed
#: chunk's batches. `timescaledb_information` does not expose them, so this is
#: the internal catalog — and the sole reason the case that calls it is marked
#: `timescaledb_210`.
_LEGACY_COMPRESSED_RELATIONS = """
    SELECT quote_ident(cc.schema_name) || '.' || quote_ident(cc.table_name) AS relname
    FROM _timescaledb_catalog.chunk c
    JOIN _timescaledb_catalog.hypertable h ON h.id = c.hypertable_id
    JOIN _timescaledb_catalog.chunk cc ON cc.id = c.compressed_chunk_id
    WHERE h.schema_name = 'hydro' AND h.table_name = 'river_timeseries_legacy'
    ORDER BY 1
"""


def _seed_legacy_history(connection: Any, *, compress_chunks: int = 0) -> None:
    """Give the legacy hypertable real chunks, optionally compressing the oldest.

    Production drops four chunks, two of them compressed. An empty table is a
    different code path: dropping a COMPRESSED chunk runs TimescaleDB's
    `sql_drop` event trigger over the internal `compress_hyper_*` relation too,
    and that has to happen inside the same transaction as everything else.
    """
    with connection.cursor() as cursor:
        cursor.execute(_SEED_LEGACY_HISTORY, (list(LEGACY_HISTORY_DAY_OFFSETS),))
        assert cursor.rowcount == LEGACY_HISTORY_ROWS, (
            f"legacy history seed wrote {cursor.rowcount} of {LEGACY_HISTORY_ROWS} rows"
        )
        if compress_chunks:
            cursor.execute(
                "SELECT compress_chunk("
                "  (quote_ident(chunk_schema) || '.' || quote_ident(chunk_name))::regclass) "
                "FROM (SELECT chunk_schema, chunk_name FROM timescaledb_information.chunks "
                "      WHERE hypertable_schema = 'hydro' "
                "        AND hypertable_name = 'river_timeseries_legacy' "
                "      ORDER BY range_start LIMIT %s) AS oldest",
                (compress_chunks,),
            )
            assert cursor.rowcount == compress_chunks, (
                f"compressed {cursor.rowcount} of {compress_chunks} requested legacy chunks"
            )


def _legacy_chunk_counts(connection: Any) -> tuple[int, int]:
    """``(chunks, compressed_chunks)`` on the legacy hypertable."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) AS total, count(*) FILTER (WHERE is_compressed) AS compressed "
            "FROM timescaledb_information.chunks "
            "WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries_legacy'"
        )
        row = cursor.fetchone()
    assert row is not None
    return int(row["total"]), int(row["compressed"])


def _legacy_chunk_relations(connection: Any) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(_LEGACY_CHUNK_RELATIONS)
        return [row["relname"] for row in cursor.fetchall()]


def _legacy_compressed_relations(connection: Any) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(_LEGACY_COMPRESSED_RELATIONS)
        return [row["relname"] for row in cursor.fetchall()]


def _legacy_hypertable_rows(connection: Any) -> int:
    """Whether TimescaleDB still knows the legacy table, via the public view."""
    return int(
        _scalar(
            connection,
            "SELECT count(*) AS n FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries_legacy'",
        )
    )


def _surviving(connection: Any, relations: list[str]) -> list[str]:
    """Which of `relations` the catalog still resolves.

    Named individually rather than counted globally: the narrow hypertable is
    compression-enabled too, so a `compress_hyper_%` census would start lying
    the day it grows a compressed chunk of its own.
    """
    return [
        relation
        for relation in relations
        if _scalar(connection, "SELECT to_regclass(%s) IS NOT NULL AS present", (relation,))
    ]


def _connect(database_url: str) -> Any:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    # Autocommit: this connection only observes the catalog, and an open
    # transaction of its own would sit on `hydro.hydro_run` while the migration
    # asks for the ACCESS EXCLUSIVE lock the final ALTER needs.
    connection.autocommit = True
    return connection


def _scalar(connection: Any, sql: str, params: tuple[Any, ...] | None = None) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        row = cursor.fetchone()
    assert row is not None
    return next(iter(row.values()))


def _contract_surface(connection: Any) -> dict[str, bool]:
    """Presence of each object 000060 drops, read from the catalog.

    Catalog probes, not `SELECT ... FROM <object>`: a dropped object has to be
    reported as absent, and a query against it would raise instead of answering.
    """
    return {
        "legacy_table": _scalar(
            connection, "SELECT to_regclass('hydro.river_timeseries_legacy') IS NOT NULL AS present"
        ),
        "routing_column": _scalar(
            connection,
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'hydro' AND table_name = 'hydro_run' "
            "AND column_name = 'timeseries_store') AS present",
        ),
        "cutover_function": _function_exists(connection, "cutover_river_identity_normalization"),
        "verify_function": _function_exists(connection, "verify_river_identity_normalization"),
    }


def _function_exists(connection: Any, proname: str) -> bool:
    return bool(
        _scalar(
            connection,
            "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'hydro' AND p.proname = %s) AS present",
            (proname,),
        )
    )


def _narrow_indexes(connection: Any) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'hydro' AND tablename = 'river_timeseries' ORDER BY indexname"
        )
        return [row["indexname"] for row in cursor.fetchall()]


def _ledger_rows(connection: Any, version: str) -> int:
    return int(
        _scalar(
            connection,
            "SELECT count(*) AS n FROM public.schema_migrations WHERE version = %s",
            (version,),
        )
    )


def _in_window_legacy_runs(connection: Any) -> int:
    """The refusal predicate, restated against the migration's own floor."""
    return int(
        _scalar(
            connection,
            "SELECT count(*) AS n FROM hydro.hydro_run "
            "WHERE timeseries_store = 'legacy' AND end_time > now() - make_interval(days => %s)",
            (RETENTION_FLOOR_DAYS,),
        )
    )


def _route_run(connection: Any, run_id: str, store: str, *, end_time_sql: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE hydro.hydro_run SET timeseries_store = %s, end_time = {end_time_sql} WHERE run_id = %s",
            (store, run_id),
        )
        assert cursor.rowcount == 1, f"{run_id} was not seeded"


def _expanded_and_seeded(database_url: str) -> Any:
    """A catalog at 000059 — expand applied, contract not — carrying real facts.

    000059 is where the transitional surface exists, so it is the only state
    from which 000060 can be exercised at all.
    """
    apply_migrations_from_zero(database_url, through="000059")
    seed_issue_126_data(database_url)
    connection = _connect(database_url)
    assert all(_contract_surface(connection).values()), (
        "the fixture must start from a catalog that still has every object 000060 drops"
    )
    return connection


def test_contract_refuses_an_in_window_legacy_run_and_changes_nothing(throwaway_database_url: str) -> None:
    """The in-window refusal branch: it raises, and it leaves no ledger row.

    This case does NOT prove atomicity, and must not be read as doing so. The
    `RAISE EXCEPTION` sits lexically before every DROP in 000060, so "all four
    objects survive" here is a consequence of statement ORDER: a deliberately
    non-atomic rewrite (a DO block for the refusal plus four bare DDL
    statements) would pass it just as green. The all-or-nothing property is
    proved by `test_contract_rolls_back_the_drops_it_already_ran_when_a_later_one_fails`,
    which fails 000060 in the MIDDLE of its drops.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        before = _contract_surface(connection)
        _route_run(connection, FORECAST_RUN_ID, "legacy", end_time_sql="now()")
        # Non-vacuity: the guard has exactly one run to find. Without this the
        # test would pass on a migration whose predicate matched nothing.
        assert _in_window_legacy_runs(connection) == 1

        with pytest.raises(psycopg2.errors.RaiseException, match="contract refused"):
            apply_migrations_from_zero(throwaway_database_url, through="000060")

        assert _contract_surface(connection) == before
        # No ledger row, so `migration_has_been_applied` lets the operator retry
        # after the backfill instead of silently skipping the contract forever.
        assert _ledger_rows(connection, CONTRACT_MIGRATION) == 0
    finally:
        connection.close()


def test_contract_rolls_back_the_drops_it_already_ran_when_a_later_one_fails(
    throwaway_database_url: str,
) -> None:
    """The real atomicity oracle: fail 000060 between two of its own drops.

    `packages/common/migrate.py:327` applies migrations with `autocommit = True`
    and splits the file into statements, so a multi-statement migration that
    failed halfway would leave the earlier drops COMMITTED with no ledger row.
    000060 is one `DO` block for exactly that reason, and only a failure that
    lands after at least one successful DROP can tell the two apart.

    A view over `hydro.verify_river_identity_normalization()` produces exactly
    that. The function is dropped LAST but one; by the time PostgreSQL refuses
    to drop it (no CASCADE, so the dependency is fatal) the block has already
    executed `DROP TABLE hydro.river_timeseries_legacy` and
    `DROP FUNCTION hydro.cutover_river_identity_normalization()`. Finding those
    two objects alive afterwards is direct evidence that an already-EXECUTED
    drop was undone — not an artefact of statement order. (Committed is exactly
    what this proves those drops were not: under autocommit and a per-statement
    split they would have been.)

    It doubles as the fail-closed proof for the deliberate absence of CASCADE:
    an unknown dependency aborts the contract instead of silently eating the
    dependent object.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        # Out of window on purpose: an in-window run would refuse BEFORE the
        # first DROP and degrade this case into the refusal test above.
        _route_run(
            connection,
            FORECAST_RUN_ID,
            "legacy",
            end_time_sql=f"now() - make_interval(days => {RETENTION_FLOOR_DAYS + 1})",
        )
        assert _in_window_legacy_runs(connection) == 0
        _seed_legacy_history(connection)
        with connection.cursor() as cursor:
            cursor.execute("CREATE VIEW hydro._probe AS SELECT * FROM hydro.verify_river_identity_normalization()")
        before = _contract_surface(connection)
        relations_before = _legacy_chunk_relations(connection)
        rows_before = _scalar(connection, "SELECT count(*) AS n FROM hydro.river_timeseries_legacy")
        assert rows_before == LEGACY_HISTORY_ROWS

        # NOT RaiseException: this is PostgreSQL's own dependency refusal, which
        # is the point — the abort comes from a drop, not from 000060's guard.
        with pytest.raises(psycopg2.errors.DependentObjectsStillExist, match="hydro._probe"):
            apply_migrations_from_zero(throwaway_database_url, through="000060")

        assert _contract_surface(connection) == before
        assert before["legacy_table"] and before["cutover_function"], (
            "the two objects whose survival carries the proof must have been present to begin with"
        )
        # The chunks came back with the table, not just its name.
        assert _legacy_chunk_relations(connection) == relations_before
        assert _surviving(connection, relations_before) == relations_before
        assert _scalar(connection, "SELECT count(*) AS n FROM hydro.river_timeseries_legacy") == rows_before
        assert _ledger_rows(connection, CONTRACT_MIGRATION) == 0
    finally:
        connection.close()


def test_contract_drops_the_transitional_surface_and_pins_three_narrow_indexes(
    throwaway_database_url: str,
) -> None:
    """The clean path: an out-of-window legacy run does NOT block the contract.

    The production shape, not an empty one. node-27 carries thousands of runs
    still routed `legacy` whose facts retention already removed; they are
    outside the window by construction and the contract must take them. Routing
    the seeded run `legacy` with an old `end_time` is therefore the case that
    distinguishes "the window predicate works" from "no legacy row existed".

    The legacy hypertable is seeded with real chunks too: `spec.md:54` says the
    table goes "regardless of its remaining chunk count" and `spec.md:61` that
    "legacy chunks may remain", and `seed_issue_126_data` alone leaves it at
    zero chunks and zero rows, which asserts neither. Compression is the other
    half of the production shape and lives in the `timescaledb_210`-marked case
    below; the chunks here are uncompressed so this stays in CI's lane.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        _route_run(
            connection,
            FORECAST_RUN_ID,
            "legacy",
            end_time_sql=f"now() - make_interval(days => {RETENTION_FLOOR_DAYS + 1})",
        )
        assert _in_window_legacy_runs(connection) == 0
        _seed_legacy_history(connection)
        assert _legacy_chunk_counts(connection) == (len(LEGACY_HISTORY_DAY_OFFSETS), 0)
        legacy_relations = _legacy_chunk_relations(connection)
        assert len(legacy_relations) == len(LEGACY_HISTORY_DAY_OFFSETS)
        # `spec.md:54` and the #1446 overwrite guard: the contract must never
        # zero or delete a coverage row, not even for the legacy-routed run
        # whose facts it is about to drop.
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO hydro.run_display_coverage (run_id, segment_count, river_sample_count) "
                "VALUES (%s, 7, 4200)",
                (FORECAST_RUN_ID,),
            )
        facts_before = _scalar(connection, "SELECT count(*) AS n FROM hydro.river_timeseries")
        assert facts_before > 0, "the seed must leave narrow facts for the drop to be able to lose"

        apply_migrations_from_zero(throwaway_database_url, through="000060")

        assert _contract_surface(connection) == {
            "legacy_table": False,
            "routing_column": False,
            "cutover_function": False,
            "verify_function": False,
        }
        # The chunks went with the table; TimescaleDB's own catalog agrees.
        assert _surviving(connection, legacy_relations) == []
        assert _legacy_chunk_counts(connection) == (0, 0)
        assert _legacy_hypertable_rows(connection) == 0
        # The narrow store is what survives, with its facts and its final shape.
        assert _scalar(connection, "SELECT to_regclass('hydro.river_timeseries') IS NOT NULL AS present")
        assert _scalar(connection, "SELECT count(*) AS n FROM hydro.river_timeseries") == facts_before
        assert _narrow_indexes(connection) == sorted(NARROW_INDEXES)
        # The CHECK constraint rode out with its column; nothing else did.
        assert (
            _scalar(
                connection,
                "SELECT count(*) AS n FROM pg_constraint "
                "WHERE conrelid = 'hydro.hydro_run'::regclass AND conname = 'hydro_run_timeseries_store_check'",
            )
            == 0
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT run_id, segment_count, river_sample_count FROM hydro.run_display_coverage ORDER BY run_id"
            )
            assert [dict(row) for row in cursor.fetchall()] == [
                {"run_id": FORECAST_RUN_ID, "segment_count": 7, "river_sample_count": 4200}
            ]
        # Both seeded runs are still there: the contract drops a column, not rows.
        with connection.cursor() as cursor:
            cursor.execute("SELECT run_id FROM hydro.hydro_run ORDER BY run_id")
            assert [row["run_id"] for row in cursor.fetchall()] == sorted((FORECAST_RUN_ID, HINDCAST_RUN_ID))
        assert _ledger_rows(connection, CONTRACT_MIGRATION) == 1
    finally:
        connection.close()


@pytest.mark.timescaledb_210
def test_contract_drops_a_legacy_hypertable_carrying_compressed_chunks(
    throwaway_database_url: str,
) -> None:
    """The production shape: some legacy chunks are COMPRESSED when the drop lands.

    node-27's legacy table holds four chunks, two of them compressed, ~717 GB.
    Dropping a compressed chunk is not the same statement as dropping an empty
    table: TimescaleDB's `sql_drop` event trigger has to tear down the internal
    `_timescaledb_internal.compress_hyper_*` relation that holds the compressed
    batches, and it has to do it inside the single transaction 000060 is, next
    to the `ALTER TABLE hydro.hydro_run` that takes ACCESS EXCLUSIVE.

    Marked `timescaledb_210`, and it is the ONLY case here that is: it is the
    only one that reaches past `timescaledb_information` into
    `_timescaledb_catalog.chunk.compressed_chunk_id` to name the internal
    relations, which is TimescaleDB-version-coupled catalog shape. node-27
    (2.10.2) is the oracle; CI deselects this case. Every other case in this
    file stays on the public views plus `to_regclass`, so CI still proves them.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        _route_run(
            connection,
            FORECAST_RUN_ID,
            "legacy",
            end_time_sql=f"now() - make_interval(days => {RETENTION_FLOOR_DAYS + 1})",
        )
        assert _in_window_legacy_runs(connection) == 0
        _seed_legacy_history(connection, compress_chunks=2)
        # Fixture premise, stated as an assertion the way
        # `tests/test_river_ts_dual_write_integration.py:405` states its own:
        # a silently-uncompressed fixture would make this case a duplicate of
        # the uncompressed one above.
        assert _legacy_chunk_counts(connection) == (len(LEGACY_HISTORY_DAY_OFFSETS), 2)
        compressed_relations = _legacy_compressed_relations(connection)
        # One internal relation per compressed chunk, and nothing else names it:
        # if the drop leaked one it would survive with no hypertable to own it.
        assert len(compressed_relations) == 2, compressed_relations
        assert all("compress_hyper" in name for name in compressed_relations), compressed_relations
        legacy_relations = sorted(_legacy_chunk_relations(connection) + compressed_relations)
        assert len(legacy_relations) == len(LEGACY_HISTORY_DAY_OFFSETS) + 2
        assert _surviving(connection, legacy_relations) == legacy_relations

        apply_migrations_from_zero(throwaway_database_url, through="000060")

        assert _scalar(connection, "SELECT to_regclass('hydro.river_timeseries_legacy') IS NULL AS gone")
        # Every chunk AND every compressed counterpart went with it.
        assert _surviving(connection, legacy_relations) == []
        assert _legacy_chunk_counts(connection) == (0, 0)
        assert _legacy_hypertable_rows(connection) == 0
        assert _ledger_rows(connection, CONTRACT_MIGRATION) == 1
        # The narrow store is still a working, compression-enabled hypertable:
        # dropping the legacy one must not have taken its settings with it.
        assert _scalar(connection, "SELECT count(*) AS n FROM hydro.river_timeseries") > 0
        assert (
            _scalar(
                connection,
                "SELECT count(*) AS n FROM timescaledb_information.compression_settings "
                "WHERE hypertable_schema = 'hydro' AND hypertable_name = 'river_timeseries'",
            )
            > 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("guc_days", "run_age_days", "effective_window"),
    [
        # The floor wins. `nhms.retention_window_days = 10` would put a 15-day-old
        # run outside the window and let the contract drop facts retention has
        # NOT dropped yet. `GREATEST(21, GUC)` makes that structurally impossible.
        ("10", 15, RETENTION_FLOOR_DAYS),
        # Widening does take effect: 25 days is outside the 21-day floor, so a
        # migration that ignored the GUC would sail past this run.
        ("30", 25, 30),
    ],
    ids=["guc_below_the_floor_cannot_narrow_it", "guc_above_the_floor_widens_it"],
)
def test_contract_window_is_the_greater_of_the_floor_and_the_guc(
    throwaway_database_url: str, guc_days: str, run_age_days: int, effective_window: int
) -> None:
    """`GREATEST(21, nhms.retention_window_days)`, both directions.

    This is the load-bearing safety property of the whole design: the retention
    window has no in-database source of truth, so 000060 pins a floor and lets a
    GUC only WIDEN it. Narrowing is the direction that loses data. Nothing else
    in the repo sets this GUC, so without these two cases the `GREATEST` could
    be a plain `COALESCE` and every other test would stay green.
    """
    _set_database_guc(throwaway_database_url, "nhms.retention_window_days", guc_days)
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        # Non-vacuity, and the reason the connection is opened AFTER the ALTER
        # DATABASE: a per-database setting only reaches sessions started later.
        # Without this, the `10` case would pass on the floor alone even if the
        # ALTER had silently done nothing.
        assert _scalar(connection, "SELECT current_setting('nhms.retention_window_days', true) AS v") == guc_days
        before = _contract_surface(connection)
        _route_run(
            connection,
            FORECAST_RUN_ID,
            "legacy",
            end_time_sql=f"now() - make_interval(days => {run_age_days})",
        )
        # Where the run sits relative to the bare floor, so the two cases are
        # visibly different: 15 days is inside it, 25 days is outside it and is
        # only caught because the GUC widened the window.
        assert _in_window_legacy_runs(connection) == (1 if run_age_days < RETENTION_FLOOR_DAYS else 0)

        with pytest.raises(
            psycopg2.errors.RaiseException, match=rf"contract refused: 1 .*inside the {effective_window}-day"
        ):
            apply_migrations_from_zero(throwaway_database_url, through="000060")

        assert _contract_surface(connection) == before
        assert _ledger_rows(connection, CONTRACT_MIGRATION) == 0
    finally:
        connection.close()


def test_contract_refuses_when_the_routing_column_is_gone_but_the_legacy_table_is_not(
    throwaway_database_url: str,
) -> None:
    """The half-applied state an impatient operator creates by hand.

    Someone who hits the in-window refusal and "fixes" it by dropping
    `hydro_run.timeseries_store` themselves destroys the ONLY evidence 000060
    has of what is still routed legacy. Without the `ELSIF` the next run would
    fall straight through to an unconditional drop of the 717 GB table. It has
    to refuse instead, and leave everything — including the chunks — alone.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        _seed_legacy_history(connection)
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE hydro.hydro_run DROP COLUMN timeseries_store")
        before = _contract_surface(connection)
        assert before == {
            "legacy_table": True,
            "routing_column": False,
            "cutover_function": True,
            "verify_function": True,
        }
        relations_before = _legacy_chunk_relations(connection)
        assert relations_before, "the refusal must have real chunks to protect"

        with pytest.raises(psycopg2.errors.RaiseException, match="timeseries_store is already absent"):
            apply_migrations_from_zero(throwaway_database_url, through="000060")

        assert _contract_surface(connection) == before
        assert _surviving(connection, relations_before) == relations_before
        assert _scalar(connection, "SELECT count(*) AS n FROM hydro.river_timeseries_legacy") == LEGACY_HISTORY_ROWS
        assert _ledger_rows(connection, CONTRACT_MIGRATION) == 0
    finally:
        connection.close()


def _set_database_guc(database_url: str, name: str, value: str) -> None:
    """`ALTER DATABASE <this one> SET <name> = <value>`, from a short connection.

    Per-database rather than per-session because the session that reads the GUC
    is the one `apply_migrations_from_zero` opens for itself; the test cannot
    reach into it. Identifier quoting goes through `psycopg2.sql`: the database
    name is generated per test and cannot be a bound parameter here.
    """
    database_name = urlsplit(database_url).path.lstrip("/")
    connection = _connect(database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("ALTER DATABASE {} SET {} = %s").format(
                    sql.Identifier(database_name), sql.Identifier(*name.split(".", 1))
                ),
                (value,),
            )
    finally:
        connection.close()


def test_contract_replays_as_a_no_op_once_the_routing_column_is_gone(throwaway_database_url: str) -> None:
    """000060's guard branch, exercised for real.

    A second `apply_migrations_from_zero` proves nothing: it is ledger-gated and
    would execute zero statements. Forgetting the ledger row is what makes the
    replay real, and the replay is the only way to reach the `IF EXISTS
    (... column ...)` guard with the column already absent — the state every
    re-run on a contracted production database is in.

    It is also the `ELSIF`'s negative leg. The test above reaches that branch
    with the legacy table still present and gets a refusal; here BOTH objects
    are gone, which is what a successful contract leaves behind, so
    `to_regclass('hydro.river_timeseries_legacy')` is NULL and the whole block
    is a no-op. Replay after success stays idempotent; replay after a hand-drop
    does not.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        apply_migrations_from_zero(throwaway_database_url, through="000060")
        surface_before = _contract_surface(connection)
        indexes_before = _narrow_indexes(connection)
        facts_before = _scalar(connection, "SELECT count(*) AS n FROM hydro.river_timeseries")
        columns_before = _hydro_run_columns(connection)
        assert surface_before == dict.fromkeys(surface_before, False)

        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM public.schema_migrations WHERE version = %s", (CONTRACT_MIGRATION,))
            assert cursor.rowcount == 1, "000060 was never recorded as applied"

        apply_migrations_from_zero(throwaway_database_url, through="000060")

        assert _contract_surface(connection) == surface_before
        assert _narrow_indexes(connection) == indexes_before
        assert _scalar(connection, "SELECT count(*) AS n FROM hydro.river_timeseries") == facts_before
        assert _hydro_run_columns(connection) == columns_before
        assert _ledger_rows(connection, CONTRACT_MIGRATION) == 1
    finally:
        connection.close()


def _hydro_run_columns(connection: Any) -> list[tuple[str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name, udt_name FROM information_schema.columns "
            "WHERE table_schema = 'hydro' AND table_name = 'hydro_run' ORDER BY ordinal_position"
        )
        return [(row["column_name"], row["udt_name"]) for row in cursor.fetchall()]
