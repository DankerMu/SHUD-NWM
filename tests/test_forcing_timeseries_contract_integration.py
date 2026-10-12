"""Real-database coverage of the forcing contract migration 000066 (#1993, task 8.2).

The forcing mirror of ``tests/test_river_timeseries_contract_integration.py``;
read that file's module docstring for the reasoning shared by both. Run with the
repo's standard opt-in against a throwaway database:

    NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=... uv run pytest -q \
        tests/test_forcing_timeseries_contract_integration.py

``throwaway_database_url`` creates and drops a uniquely-named database per TEST.
That is mandatory here: every case mutates SCHEMA (it drops a table and a
column), so a session-scoped database would leak the contracted catalog into
every later test in the run.

Where forcing differs from river, and so has cases river does not:

* The refusal window is measured on ``met.forcing_version.cycle_time``, a choice
  the spec keeps revisitable. ``end_time`` is the obvious alternative and on
  node-27 the two disagree (2026-10-11: 0 legacy-routed versions by
  ``cycle_time``, 684 by ``end_time``), so one case pins which column decides.
* ``cycle_time`` is NULLABLE (``db/migrations/000005_met.sql:78``); river's
  ``end_time`` is not. A legacy-routed version with no ``cycle_time`` must count
  as inside the window.
* ``met`` has no cutover / verify functions, so the atomicity case cannot hang
  its failure on a function between two drops; it hangs it on the LAST drop.

Oracle note, as for river: every case but the compressed-chunk one asserts
through plain PostgreSQL DDL, ``to_regclass`` and the PUBLIC
``timescaledb_information`` views. The compressed-chunk case reads
``_timescaledb_catalog`` and therefore carries ``timescaledb_210``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from tests.integration_helpers import (
    BASIN_VERSION_ID,
    FORCING_VERSION_ID,
    ISSUE_126_PREFIX,
    SOURCE_ID,
    apply_migrations_from_zero,
    seed_issue_126_data,
)

pytestmark = pytest.mark.integration

#: The file 000066 ships as, i.e. the ledger key `record_migration` writes.
CONTRACT_MIGRATION = "000066_forcing_timeseries_contract.sql"

#: The last catalog that still has everything 000066 drops.
PRE_CONTRACT = "000065"
CONTRACT = "000066"

LEGACY_TABLE = "met.forcing_station_timeseries_legacy"
NARROW_TABLE = "met.forcing_station_timeseries"

#: The narrow hypertable's final index shape (000061): its primary key and the
#: one key-shaped successor of the QHH latest-window index. Names, not a count.
NARROW_INDEXES = (
    "forcing_station_timeseries_narrow_pkey",
    "forcing_ts_version_variable_time_key_idx",
)

NARROW_FOREIGN_KEYS = [
    "FOREIGN KEY (forcing_version_key) REFERENCES met.forcing_version(forcing_version_key)",
    "FOREIGN KEY (station_key) REFERENCES met.met_station(station_key)",
]

FORCING_ENUMS = ("met.forcing_variable", "met.forcing_unit", "met.forcing_quality_flag")

#: 000066 pins its retention floor at 21 days (`GREATEST(21, GUC)`).
RETENTION_FLOOR_DAYS = 21

STATION_ID = f"{ISSUE_126_PREFIX}_contract_station"

#: A second forcing version, always routed `narrow`, so "the contract drops a
#: column, not rows" has more than one row to be wrong about.
NARROW_VERSION_ID = f"{ISSUE_126_PREFIX}_forcing_contract_narrow"

#: Day offsets of the legacy fact rows. The legacy hypertable's
#: `chunk_time_interval` is 3 days (000058, carried through 000061's rename), so
#: 30-day steps cannot collapse two offsets into one chunk: three offsets, three
#: chunks. Without them the table 000066 drops is EMPTY and "the chunks go with
#: the table" has no execution evidence.
LEGACY_HISTORY_DAY_OFFSETS = (0, 30, 60)
LEGACY_HISTORY_ROWS = len(LEGACY_HISTORY_DAY_OFFSETS)
NARROW_FACT_ROWS = 2

_SEED_STATION = """
    INSERT INTO met.met_station (station_id, basin_version_id, station_name, geom, elevation_m)
    VALUES (%s, %s, 'Issue 1993 contract station', ST_SetSRID(ST_MakePoint(110.5, 30.5), 4490), 420.0)
"""

_SEED_NARROW_VERSION = """
    INSERT INTO met.forcing_version (
        forcing_version_id, model_id, source_id, cycle_time,
        start_time, end_time, station_count, forcing_package_uri, timeseries_store
    )
    SELECT %s, model_id, source_id, cycle_time, start_time, end_time, station_count,
           forcing_package_uri || '-contract-narrow', 'narrow'
    FROM met.forcing_version WHERE forcing_version_id = %s
"""

_SEED_LEGACY_HISTORY = """
    INSERT INTO met.forcing_station_timeseries_legacy (
        forcing_version_id, basin_version_id, station_id, valid_time,
        source_id, variable, value, unit, native_resolution, quality_flag
    )
    SELECT %s, %s, %s, timestamptz '2026-05-03 01:00+00' - make_interval(days => o.offset_days),
           %s, 'PRCP', 1.5, 'mm/day', '1h', 'ok'
    FROM unnest(%s::integer[]) AS o(offset_days)
"""

_SEED_NARROW_FACTS = """
    INSERT INTO met.forcing_station_timeseries (
        forcing_version_key, station_key, valid_time, variable_e, value, unit_e,
        quality_flag_e, native_resolution
    )
    SELECT fv.forcing_version_key, ms.station_key,
           timestamptz '2026-05-03 01:00+00' + make_interval(hours => h.hour),
           'PRCP', 2.5, 'mm/day', 'ok', '1h'
    FROM met.forcing_version fv, met.met_station ms, generate_series(0, %s - 1) AS h(hour)
    WHERE fv.forcing_version_id = %s AND ms.station_id = %s
"""

_LEGACY_CHUNK_RELATIONS = """
    SELECT quote_ident(chunk_schema) || '.' || quote_ident(chunk_name) AS relname
    FROM timescaledb_information.chunks
    WHERE hypertable_schema = 'met' AND hypertable_name = 'forcing_station_timeseries_legacy'
    ORDER BY 1
"""

#: The `_timescaledb_internal.compress_hyper_*` relations holding a compressed
#: chunk's batches. `timescaledb_information` does not expose them, so this is
#: the internal catalog — the sole reason the case that calls it is marked
#: `timescaledb_210`.
_LEGACY_COMPRESSED_RELATIONS = """
    SELECT quote_ident(cc.schema_name) || '.' || quote_ident(cc.table_name) AS relname
    FROM _timescaledb_catalog.chunk c
    JOIN _timescaledb_catalog.hypertable h ON h.id = c.hypertable_id
    JOIN _timescaledb_catalog.chunk cc ON cc.id = c.compressed_chunk_id
    WHERE h.schema_name = 'met' AND h.table_name = 'forcing_station_timeseries_legacy'
    ORDER BY 1
"""


def _connect(database_url: str) -> Any:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    # Autocommit: this connection only observes the catalog, and an open
    # transaction of its own would sit on `met.forcing_version` while the
    # migration asks for ACCESS EXCLUSIVE on it.
    connection.autocommit = True
    return connection


def _scalar(connection: Any, statement: str, params: tuple[Any, ...] | None = None) -> Any:
    with connection.cursor() as cursor:
        cursor.execute(statement, params)
        row = cursor.fetchone()
    assert row is not None
    return next(iter(row.values()))


def _contract_surface(connection: Any) -> dict[str, bool]:
    """Presence of each object 000066 drops, read from the catalog."""
    return {
        "legacy_table": _scalar(connection, "SELECT to_regclass(%s) IS NOT NULL AS present", (LEGACY_TABLE,)),
        "routing_column": _scalar(
            connection,
            "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'met' AND table_name = 'forcing_version' "
            "AND column_name = 'timeseries_store') AS present",
        ),
        "routing_check": _routing_checks(connection) == 1,
    }


def _routing_checks(connection: Any) -> int:
    """CHECK constraints on `met.forcing_version` that mention the routing column."""
    return int(
        _scalar(
            connection,
            "SELECT count(*) AS n FROM pg_constraint "
            "WHERE conrelid = 'met.forcing_version'::regclass AND contype = 'c' "
            "AND pg_get_constraintdef(oid) LIKE '%%timeseries_store%%'",
        )
    )


def _legacy_chunk_relations(connection: Any) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(_LEGACY_CHUNK_RELATIONS)
        return [row["relname"] for row in cursor.fetchall()]


def _legacy_compressed_relations(connection: Any) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(_LEGACY_COMPRESSED_RELATIONS)
        return [row["relname"] for row in cursor.fetchall()]


def _legacy_chunk_counts(connection: Any) -> tuple[int, int]:
    """``(chunks, compressed_chunks)`` on the legacy hypertable."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) AS total, count(*) FILTER (WHERE is_compressed) AS compressed "
            "FROM timescaledb_information.chunks "
            "WHERE hypertable_schema = 'met' AND hypertable_name = 'forcing_station_timeseries_legacy'"
        )
        row = cursor.fetchone()
    assert row is not None
    return int(row["total"]), int(row["compressed"])


def _legacy_hypertable_rows(connection: Any) -> int:
    return int(
        _scalar(
            connection,
            "SELECT count(*) AS n FROM timescaledb_information.hypertables "
            "WHERE hypertable_schema = 'met' AND hypertable_name = 'forcing_station_timeseries_legacy'",
        )
    )


def _surviving(connection: Any, relations: list[str]) -> list[str]:
    """Which of `relations` the catalog still resolves, named individually."""
    return [
        relation
        for relation in relations
        if _scalar(connection, "SELECT to_regclass(%s) IS NOT NULL AS present", (relation,))
    ]


def _narrow_indexes(connection: Any) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'met' AND tablename = 'forcing_station_timeseries' ORDER BY indexname"
        )
        return [row["indexname"] for row in cursor.fetchall()]


def _narrow_foreign_keys(connection: Any) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE conrelid = 'met.forcing_station_timeseries'::regclass AND contype = 'f' ORDER BY 1"
        )
        return [row["definition"] for row in cursor.fetchall()]


def _columns(connection: Any, table: str) -> list[tuple[str, str]]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_name, udt_name FROM information_schema.columns "
            "WHERE table_schema = 'met' AND table_name = %s ORDER BY ordinal_position",
            (table,),
        )
        return [(row["column_name"], row["udt_name"]) for row in cursor.fetchall()]


def _count(connection: Any, relation: str) -> int:
    return int(_scalar(connection, f"SELECT count(*) AS n FROM {relation}"))


def _ledger_rows(connection: Any, version: str) -> int:
    return int(
        _scalar(connection, "SELECT count(*) AS n FROM public.schema_migrations WHERE version = %s", (version,))
    )


def _legacy_versions_in_window(connection: Any, column: str) -> int:
    """Legacy-routed versions whose `column` is inside the floor (NULL excluded)."""
    return int(
        _scalar(
            connection,
            sql.SQL(
                "SELECT count(*) AS n FROM met.forcing_version "
                "WHERE timeseries_store = 'legacy' AND {} > now() - make_interval(days => %s)"
            ).format(sql.Identifier(column)),
            (RETENTION_FLOOR_DAYS,),
        )
    )


def _route_version(
    connection: Any,
    store: str,
    *,
    cycle_time_sql: str,
    end_time_sql: str | None = None,
    forcing_version_id: str = FORCING_VERSION_ID,
) -> None:
    """Route one seeded version and place its two candidate window columns.

    `end_time` defaults to `cycle_time`'s placement so a case that does not care
    about the difference cannot pass on the wrong column by accident; `start_time`
    is kept at or before both.
    """
    end_sql = end_time_sql or cycle_time_sql
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE met.forcing_version SET timeseries_store = %s, cycle_time = {cycle_time_sql}, "
            f"end_time = COALESCE({end_sql}, now()), start_time = now() - interval '400 days' "
            "WHERE forcing_version_id = %s",
            (store, forcing_version_id),
        )
        assert cursor.rowcount == 1, f"{forcing_version_id} was not seeded"


def _days_ago(days: int) -> str:
    return f"now() - make_interval(days => {days})"


def _seed_legacy_history(connection: Any, *, compress_chunks: int = 0) -> None:
    """Give the legacy hypertable real chunks, optionally compressing the oldest."""
    with connection.cursor() as cursor:
        cursor.execute(
            _SEED_LEGACY_HISTORY,
            (FORCING_VERSION_ID, BASIN_VERSION_ID, STATION_ID, SOURCE_ID, list(LEGACY_HISTORY_DAY_OFFSETS)),
        )
        assert cursor.rowcount == LEGACY_HISTORY_ROWS
        if compress_chunks:
            cursor.execute(
                "SELECT compress_chunk("
                "  (quote_ident(chunk_schema) || '.' || quote_ident(chunk_name))::regclass) "
                "FROM (SELECT chunk_schema, chunk_name FROM timescaledb_information.chunks "
                "      WHERE hypertable_schema = 'met' "
                "        AND hypertable_name = 'forcing_station_timeseries_legacy' "
                "      ORDER BY range_start LIMIT %s) AS oldest",
                (compress_chunks,),
            )
            assert cursor.rowcount == compress_chunks


def _expanded_and_seeded(database_url: str) -> Any:
    """A catalog at 000065 — expand applied, contract not — carrying real facts.

    Two forcing versions (the shared seed's, which the cases route, and one that
    stays `narrow`), one station, legacy facts in three chunks for the first
    version and narrow facts for the second.
    """
    apply_migrations_from_zero(database_url, through=PRE_CONTRACT)
    seed_issue_126_data(database_url)
    connection = _connect(database_url)
    with connection.cursor() as cursor:
        cursor.execute(_SEED_STATION, (STATION_ID, BASIN_VERSION_ID))
        cursor.execute(_SEED_NARROW_VERSION, (NARROW_VERSION_ID, FORCING_VERSION_ID))
        cursor.execute(_SEED_NARROW_FACTS, (NARROW_FACT_ROWS, NARROW_VERSION_ID, STATION_ID))
        assert cursor.rowcount == NARROW_FACT_ROWS
    # Out of the window on BOTH candidate columns: every case then moves exactly
    # the one it is about.
    _route_version(connection, "narrow", cycle_time_sql=_days_ago(400), forcing_version_id=NARROW_VERSION_ID)
    _route_version(connection, "legacy", cycle_time_sql=_days_ago(400))
    _seed_legacy_history(connection)
    assert all(_contract_surface(connection).values()), (
        "the fixture must start from a catalog that still has every object 000066 drops"
    )
    return connection


def _assert_nothing_changed(connection: Any, before: dict[str, bool], relations_before: list[str]) -> None:
    assert _contract_surface(connection) == before
    assert _legacy_chunk_relations(connection) == relations_before
    assert _surviving(connection, relations_before) == relations_before
    assert _count(connection, LEGACY_TABLE) == LEGACY_HISTORY_ROWS
    assert _count(connection, NARROW_TABLE) == NARROW_FACT_ROWS
    assert _count(connection, "met.forcing_version") == 2
    assert _ledger_rows(connection, CONTRACT_MIGRATION) == 0


def _assert_contracted(connection: Any, legacy_relations: list[str]) -> None:
    assert _contract_surface(connection) == {"legacy_table": False, "routing_column": False, "routing_check": False}
    assert _routing_checks(connection) == 0
    # The chunks went with the table; TimescaleDB's own catalog agrees.
    assert _surviving(connection, legacy_relations) == []
    assert _legacy_chunk_counts(connection) == (0, 0)
    assert _legacy_hypertable_rows(connection) == 0
    assert _ledger_rows(connection, CONTRACT_MIGRATION) == 1


def test_contract_refuses_an_in_window_legacy_version_and_changes_nothing(throwaway_database_url: str) -> None:
    """The in-window refusal: it raises, names the count and the window, records nothing.

    As for river, this does NOT prove atomicity — the RAISE sits before every
    DROP. `test_contract_rolls_back_the_table_drop_when_the_column_drop_fails`
    does.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        _route_version(connection, "legacy", cycle_time_sql="now()")
        # Non-vacuity: the guard has exactly one version to find.
        assert _legacy_versions_in_window(connection, "cycle_time") == 1
        before = _contract_surface(connection)
        relations_before = _legacy_chunk_relations(connection)
        assert len(relations_before) == len(LEGACY_HISTORY_DAY_OFFSETS)

        with pytest.raises(
            psycopg2.errors.RaiseException,
            match=rf"contract refused: 1 legacy-routed .*inside the {RETENTION_FLOOR_DAYS}-day .*nothing was changed",
        ):
            apply_migrations_from_zero(throwaway_database_url, through=CONTRACT)

        _assert_nothing_changed(connection, before, relations_before)
    finally:
        connection.close()


def test_contract_window_is_measured_on_cycle_time_not_end_time(throwaway_database_url: str) -> None:
    """A legacy version whose `end_time` is in the window but whose `cycle_time` is not.

    This is node-27's actual population on 2026-10-11 (684 such versions) and
    the spec's chosen column. A migration that measured `end_time` would refuse
    here; the contract must go through.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        _route_version(
            connection,
            "legacy",
            cycle_time_sql=_days_ago(RETENTION_FLOOR_DAYS + 1),
            end_time_sql=_days_ago(1),
        )
        assert _legacy_versions_in_window(connection, "end_time") == 1
        assert _legacy_versions_in_window(connection, "cycle_time") == 0
        legacy_relations = _legacy_chunk_relations(connection)

        apply_migrations_from_zero(throwaway_database_url, through=CONTRACT)

        _assert_contracted(connection, legacy_relations)
    finally:
        connection.close()


def test_contract_refuses_a_legacy_version_with_no_cycle_time(throwaway_database_url: str) -> None:
    """NULL is fail-closed: it cannot be shown to be outside the window.

    `cycle_time > now() - window` is NULL for such a row, so a bare comparison
    would let it through — with `end_time` 400 days old nothing else here could
    refuse it.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        _route_version(connection, "legacy", cycle_time_sql="NULL", end_time_sql=_days_ago(400))
        assert _scalar(
            connection,
            "SELECT cycle_time IS NULL AND timeseries_store = 'legacy' AS premise "
            "FROM met.forcing_version WHERE forcing_version_id = %s",
            (FORCING_VERSION_ID,),
        )
        assert _legacy_versions_in_window(connection, "cycle_time") == 0
        assert _legacy_versions_in_window(connection, "end_time") == 0
        before = _contract_surface(connection)
        relations_before = _legacy_chunk_relations(connection)

        with pytest.raises(psycopg2.errors.RaiseException, match=r"contract refused: 1 legacy-routed"):
            apply_migrations_from_zero(throwaway_database_url, through=CONTRACT)

        _assert_nothing_changed(connection, before, relations_before)
    finally:
        connection.close()


def test_contract_does_not_refuse_a_narrow_version_inside_the_window(throwaway_database_url: str) -> None:
    """The `legacy` half of the predicate: a fresh narrow-routed version is the normal case.

    Every version the autopipe writes today is narrow-routed and in the window.
    A predicate that dropped the route test would refuse forever.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        _route_version(connection, "narrow", cycle_time_sql="now()", forcing_version_id=NARROW_VERSION_ID)
        assert (
            _scalar(
                connection,
                "SELECT count(*) AS n FROM met.forcing_version "
                "WHERE timeseries_store = 'narrow' AND cycle_time > now() - make_interval(days => %s)",
                (RETENTION_FLOOR_DAYS,),
            )
            == 1
        )
        assert _legacy_versions_in_window(connection, "cycle_time") == 0
        legacy_relations = _legacy_chunk_relations(connection)

        apply_migrations_from_zero(throwaway_database_url, through=CONTRACT)

        _assert_contracted(connection, legacy_relations)
    finally:
        connection.close()


def test_contract_refuses_when_the_routing_column_is_gone_but_the_legacy_table_is_not(
    throwaway_database_url: str,
) -> None:
    """The half-applied state: the column was dropped by hand, the table was not.

    The routing column is 000066's only evidence of what is still routed legacy.
    Without the `ELSIF` the next run would fall through to an unconditional drop.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE met.forcing_version DROP COLUMN timeseries_store")
        before = _contract_surface(connection)
        assert before == {"legacy_table": True, "routing_column": False, "routing_check": False}
        relations_before = _legacy_chunk_relations(connection)
        assert relations_before, "the refusal must have real chunks to protect"

        with pytest.raises(psycopg2.errors.RaiseException, match="timeseries_store is already absent"):
            apply_migrations_from_zero(throwaway_database_url, through=CONTRACT)

        _assert_nothing_changed(connection, before, relations_before)
    finally:
        connection.close()


def test_contract_rolls_back_the_table_drop_when_the_column_drop_fails(throwaway_database_url: str) -> None:
    """The atomicity oracle: fail 000066 AFTER its first drop has executed.

    The migrate lane applies files under autocommit, split into statements, so a
    multi-statement migration that failed on its second drop would leave the
    first one COMMITTED with no ledger row. 000066 is one `DO` block for that
    reason, and only a failure landing after a successful DROP can tell the two
    apart.

    A view over `met.forcing_version.timeseries_store` produces exactly that:
    `DROP TABLE ..._legacy` runs first and succeeds, then PostgreSQL refuses
    `DROP COLUMN` (no CASCADE, so the dependency is fatal). Finding the legacy
    table, its chunks and its rows alive afterwards is direct evidence that an
    already-EXECUTED drop was undone. A view over the legacy table itself would
    block the FIRST drop and prove nothing.

    It doubles as the fail-closed proof for the deliberate absence of CASCADE.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        # Out of window (the fixture default): an in-window version would refuse
        # BEFORE the first DROP and degrade this into the refusal case.
        assert _legacy_versions_in_window(connection, "cycle_time") == 0
        with connection.cursor() as cursor:
            cursor.execute("CREATE VIEW met._probe AS SELECT timeseries_store FROM met.forcing_version")
        before = _contract_surface(connection)
        relations_before = _legacy_chunk_relations(connection)
        assert before["legacy_table"] and relations_before

        # NOT RaiseException: this is PostgreSQL's own dependency refusal — the
        # abort comes from a drop, not from 000066's guard.
        with pytest.raises(psycopg2.errors.DependentObjectsStillExist, match="met._probe"):
            apply_migrations_from_zero(throwaway_database_url, through=CONTRACT)

        _assert_nothing_changed(connection, before, relations_before)
    finally:
        connection.close()


def test_contract_drops_the_transitional_surface_and_pins_the_narrow_store(throwaway_database_url: str) -> None:
    """The clean path: an out-of-window legacy version does NOT block the contract.

    The production shape — node-27 carries thousands of versions still routed
    `legacy` whose `cycle_time` is outside the window. The legacy hypertable
    holds real (uncompressed) chunks so "the chunks go with the table" is
    executed, and this stays in CI's lane.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        assert _legacy_versions_in_window(connection, "cycle_time") == 0
        assert _legacy_chunk_counts(connection) == (len(LEGACY_HISTORY_DAY_OFFSETS), 0)
        legacy_relations = _legacy_chunk_relations(connection)
        version_columns_before = _columns(connection, "forcing_version")
        assert ("timeseries_store", "text") in version_columns_before

        apply_migrations_from_zero(throwaway_database_url, through=CONTRACT)

        _assert_contracted(connection, legacy_relations)
        # The narrow store is what survives: facts, indexes, enforced FKs.
        assert _scalar(connection, "SELECT to_regclass(%s) IS NOT NULL AS present", (NARROW_TABLE,))
        assert _count(connection, NARROW_TABLE) == NARROW_FACT_ROWS
        assert _narrow_indexes(connection) == sorted(NARROW_INDEXES)
        assert _narrow_foreign_keys(connection) == NARROW_FOREIGN_KEYS
        # Exactly the routing column left met.forcing_version; the surrogate key
        # stayed, and so did met.met_station's.
        assert _columns(connection, "forcing_version") == [
            column for column in version_columns_before if column[0] != "timeseries_store"
        ]
        assert ("forcing_version_key", "int4") in _columns(connection, "forcing_version")
        assert ("station_key", "int4") in _columns(connection, "met_station")
        for enum in FORCING_ENUMS:
            assert _scalar(connection, "SELECT to_regtype(%s) IS NOT NULL AS present", (enum,)), enum
        # Both versions are still there: the contract drops a column, not rows.
        with connection.cursor() as cursor:
            cursor.execute("SELECT forcing_version_id FROM met.forcing_version ORDER BY 1")
            assert [row["forcing_version_id"] for row in cursor.fetchall()] == sorted(
                (FORCING_VERSION_ID, NARROW_VERSION_ID)
            )
    finally:
        connection.close()


@pytest.mark.timescaledb_210
def test_contract_drops_a_legacy_hypertable_carrying_compressed_chunks(throwaway_database_url: str) -> None:
    """Some legacy chunks are COMPRESSED when the drop lands.

    Dropping a compressed chunk makes TimescaleDB's `sql_drop` event trigger tear
    down the internal `compress_hyper_*` relation too, inside the single
    transaction 000066 is. node-27 measured 0 compressed legacy chunks on
    2026-10-11, but the compression timer enumerates the table from the catalog
    until the window, so the state is reachable. Marked `timescaledb_210`
    because naming the internal relations reads `_timescaledb_catalog`.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT compress_chunk(c::regclass) FROM unnest(%s::text[]) AS c",
                (_legacy_chunk_relations(connection)[:2],),
            )
        assert _legacy_chunk_counts(connection) == (len(LEGACY_HISTORY_DAY_OFFSETS), 2)
        compressed_relations = _legacy_compressed_relations(connection)
        assert len(compressed_relations) == 2, compressed_relations
        assert all("compress_hyper" in name for name in compressed_relations), compressed_relations
        legacy_relations = sorted(_legacy_chunk_relations(connection) + compressed_relations)
        assert _surviving(connection, legacy_relations) == legacy_relations

        apply_migrations_from_zero(throwaway_database_url, through=CONTRACT)

        _assert_contracted(connection, legacy_relations)
        # The narrow store is still a working, compression-enabled hypertable.
        assert _count(connection, NARROW_TABLE) == NARROW_FACT_ROWS
        assert (
            _scalar(
                connection,
                "SELECT count(*) AS n FROM timescaledb_information.compression_settings "
                "WHERE hypertable_schema = 'met' AND hypertable_name = 'forcing_station_timeseries'",
            )
            > 0
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("guc_days", "version_age_days", "effective_window"),
    [
        # The floor wins: `nhms.retention_window_days = 7` would put a 15-day-old
        # version outside the window. `GREATEST(21, GUC)` makes that impossible.
        ("7", 15, RETENTION_FLOOR_DAYS),
        # Widening does take effect: 25 days is outside the 21-day floor, so a
        # migration that ignored the GUC would sail past this version.
        ("30", 25, 30),
    ],
    ids=["guc_below_the_floor_cannot_narrow_it", "guc_above_the_floor_widens_it"],
)
def test_contract_window_is_the_greater_of_the_floor_and_the_guc(
    throwaway_database_url: str, guc_days: str, version_age_days: int, effective_window: int
) -> None:
    _set_database_guc(throwaway_database_url, "nhms.retention_window_days", guc_days)
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        # Non-vacuity: a per-database setting only reaches sessions started
        # after the ALTER DATABASE, which is why the connection is opened late.
        assert _scalar(connection, "SELECT current_setting('nhms.retention_window_days', true) AS v") == guc_days
        _route_version(connection, "legacy", cycle_time_sql=_days_ago(version_age_days))
        assert _legacy_versions_in_window(connection, "cycle_time") == (
            1 if version_age_days < RETENTION_FLOOR_DAYS else 0
        )
        before = _contract_surface(connection)
        relations_before = _legacy_chunk_relations(connection)

        with pytest.raises(
            psycopg2.errors.RaiseException, match=rf"contract refused: 1 .*inside the {effective_window}-day"
        ):
            apply_migrations_from_zero(throwaway_database_url, through=CONTRACT)

        _assert_nothing_changed(connection, before, relations_before)
    finally:
        connection.close()


def test_contract_replays_as_a_no_op_once_both_objects_are_gone(throwaway_database_url: str) -> None:
    """000066's guard branch, exercised for real.

    A second `apply_migrations_from_zero` is ledger-gated and would execute
    nothing. Forgetting the ledger row is what makes the replay real — the state
    every re-run on a contracted database is in. It is also the `ELSIF`'s
    negative leg: both objects gone, so no refusal.
    """
    connection = _expanded_and_seeded(throwaway_database_url)
    try:
        legacy_relations = _legacy_chunk_relations(connection)
        apply_migrations_from_zero(throwaway_database_url, through=CONTRACT)
        _assert_contracted(connection, legacy_relations)
        indexes_before = _narrow_indexes(connection)
        columns_before = _columns(connection, "forcing_version")

        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM public.schema_migrations WHERE version = %s", (CONTRACT_MIGRATION,))
            assert cursor.rowcount == 1, "000066 was never recorded as applied"

        apply_migrations_from_zero(throwaway_database_url, through=CONTRACT)

        _assert_contracted(connection, legacy_relations)
        assert _narrow_indexes(connection) == indexes_before
        assert _narrow_foreign_keys(connection) == NARROW_FOREIGN_KEYS
        assert _count(connection, NARROW_TABLE) == NARROW_FACT_ROWS
        assert _columns(connection, "forcing_version") == columns_before
    finally:
        connection.close()


def _set_database_guc(database_url: str, name: str, value: str) -> None:
    """`ALTER DATABASE <this one> SET <name> = <value>`, from a short connection.

    Per-database because the session that reads the GUC is the one
    `apply_migrations_from_zero` opens for itself.
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
