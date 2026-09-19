"""Real-database coverage of the river contract migration 000060 (#1988, task 6.2).

Run with the repo's standard opt-in against a throwaway database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_river_timeseries_contract_integration.py

``throwaway_database_url`` (tests/conftest.py) creates and drops a
uniquely-named database per TEST. That is mandatory here, not a preference:
every case below mutates SCHEMA (it drops a table, two functions and a column),
so a session-scoped database would leak the contracted catalog into every later
test in the run.

Oracle note: the catalog effects asserted here are plain PostgreSQL DDL, so any
PG 15 server proves them. The version that matters for the surrounding suite is
node-27's TimescaleDB 2.10.2 / PG 15.2; nothing below depends on a
TimescaleDB-version-specific behaviour, so these cases are not marked
``timescaledb_210``.
"""

from __future__ import annotations

from typing import Any

import psycopg2
import pytest
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
    """The refusal branch, and the atomicity claim that makes a retry safe.

    `packages/common/migrate.py` applies migrations with `autocommit = True` and
    splits the file into statements, so a multi-statement migration that failed
    halfway would leave the earlier drops committed with no ledger row. 000060
    is one `DO` block for exactly that reason, and the assertion that all four
    objects survive the exception is what proves the block, not the intent.
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


def test_contract_drops_the_transitional_surface_and_pins_three_narrow_indexes(
    throwaway_database_url: str,
) -> None:
    """The clean path: an out-of-window legacy run does NOT block the contract.

    The production shape, not an empty one. node-27 carries thousands of runs
    still routed `legacy` whose facts retention already removed; they are
    outside the window by construction and the contract must take them. Routing
    the seeded run `legacy` with an old `end_time` is therefore the case that
    distinguishes "the window predicate works" from "no legacy row existed".
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


def test_contract_replays_as_a_no_op_once_the_routing_column_is_gone(throwaway_database_url: str) -> None:
    """000060's guard branch, exercised for real.

    A second `apply_migrations_from_zero` proves nothing: it is ledger-gated and
    would execute zero statements. Forgetting the ledger row is what makes the
    replay real, and the replay is the only way to reach the `IF EXISTS
    (... column ...)` guard with the column already absent — the state every
    re-run on a contracted production database is in.
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
