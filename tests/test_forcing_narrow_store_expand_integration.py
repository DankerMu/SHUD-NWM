"""Real-database coverage of the forcing expand migration 000061 (#1991, task 7.3).

Postgres lane. The schema is built by applying the REAL migrations from zero
(``apply_migrations_from_zero``), so everything asserted here is asserted about
the DDL that will run on node-27, not about a hand-built catalog.

Four things live here and nothing else:

1. **The catalog after 000061** — invariant I1 and spec ``forcing-narrow-store``
   :5,13. The narrow table's exact column set and nullability, its primary key,
   its ONE secondary index, its two enforced foreign keys, its key-based
   compression settings and its one-day chunk interval; and the legacy table
   still carrying its original shape, its indexes and its owner.

2. **The routing backfill** — invariant I2. Every forcing version with rows in
   the renamed legacy table resolves to ``timeseries_store = 'legacy'``, every
   other to ``narrow``. Row existence is the oracle because
   ``met.forcing_version`` has neither ``parsed_at`` nor ``status``, so river's
   predicate (``000059:78-79``) has nothing to map onto.

3. **The read path after #1993's first cut** — the readers render only the
   narrow template and never read the routing column, so the column's value
   changes nothing, rows in the renamed legacy table are invisible, and the
   narrow leg reports the station authority's basin. (During the expand window
   this section proved legacy↔narrow equality, spec scenario :36; that
   comparison has no legacy reader left to stand on.)

4. **The writer after #1993's first cut** (fixture ``I14-1993.md`` D3) — a
   version whose column still reads ``legacy`` is replaced like any other: the
   rows land in the narrow table, the legacy rows are untouched, nothing raises.

PINNED AT 000065. Every catalog here is the EXPAND-era one: the file reads and
writes ``met.forcing_station_timeseries_legacy`` and
``met.forcing_version.timeseries_store``, both of which the forcing contract
(migration 000066, #1993 task 8.2) drops. That transitional state still exists
inside every from-zero rebuild, so the pin keeps these oracles meaningful; the
post-contract catalog is ``tests/test_forcing_timeseries_contract_integration.py``'s.

SILENT-SKIP TRAP. ``tests/conftest.py``'s ``_integration_skip_reason`` skips this
whole module unless ``NHMS_RUN_INTEGRATION=1`` AND
``NHMS_INTEGRATION_DATABASE_URL`` are set; a generic ``DATABASE_URL`` is ignored.
A green run that skipped is not evidence. Assert the COLLECTED COUNT, not the
exit code.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor, execute_values

from packages.common import display_coverage, forecast_store
from packages.common.display_coverage import refresh_run_display_coverage
from packages.common.forcing_store_routing import FORCING_NARROW_INSERT_TEMPLATE
from tests.integration_helpers import (
    BASIN_ID,
    BASIN_VERSION_ID,
    CYCLE_TIME,
    FORCING_VERSION_ID,
    FORECAST_RUN_ID,
    ISSUE_126_PREFIX,
    MODEL_ID,
    SOURCE_ID,
    VALID_TIME_1,
    VALID_TIME_2,
    apply_migrations_from_zero,
    seed_issue_126_data,
)

pytestmark = pytest.mark.integration

NARROW_TABLE = "met.forcing_station_timeseries"
LEGACY_TABLE = "met.forcing_station_timeseries_legacy"

#: The last migration before the forcing contract (000066) drops the legacy
#: table and the routing column this file is about.
EXPAND_ERA = "000065"

#: Spec :5, in the migration's own declaration order. Nullability included: it is
#: half the claim, and ``native_resolution`` is the one column an implementer
#: drops by transcribing river's 000059.
EXPECTED_NARROW_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("forcing_version_key", "integer", False),
    ("station_key", "integer", False),
    ("valid_time", "timestamp with time zone", False),
    ("variable_e", "USER-DEFINED", False),
    ("value", "double precision", False),
    ("unit_e", "USER-DEFINED", False),
    ("quality_flag_e", "USER-DEFINED", False),
    ("native_resolution", "text", True),
)

#: The station this module owns. `seed_issue_126_data` seeds no
#: `met.met_station` / `met.interp_weight` / fact rows and must not start (it is
#: shared with a session-scoped database), so this module seeds its own on a
#: per-test throwaway.
STATION_ID = f"{ISSUE_126_PREFIX}_narrow_station"
GRID_ID = f"{ISSUE_126_PREFIX}_narrow_grid"
GRID_CELL_ID = f"{ISSUE_126_PREFIX}_narrow_cell"

#: The basin version the LEGACY fact rows claim, which is NOT the one
#: `met.met_station` declares for `STATION_ID`. The legacy fact row stores its
#: own `basin_version_id` with no foreign key to `met.met_station`
#: (`db/migrations/000005_met.sql:101`), so the two CAN disagree, and the narrow
#: table removes the column entirely and derives it from the station authority.
DIVERGENT_BASIN_VERSION_ID = f"{ISSUE_126_PREFIX}_divergent_basin_v"

#: A second forecast run, on the divergent basin version, sharing the SAME
#: forcing version. Only the legacy rows' own `basin_version_id` matches it, so
#: it is the candidate that would light up if anything still read the legacy
#: table.
DIVERGENT_RUN_ID = f"{ISSUE_126_PREFIX}_divergent_forecast_run"

VARIABLES: tuple[str, ...] = tuple(forecast_store.MVP_STATION_VARIABLES)

#: One unit per MVP variable, matching 000061's `met.forcing_unit` exactly --
#: the narrow INSERT casts to that enum, so an arbitrary string fails the write.
UNITS: dict[str, str] = {
    "PRCP": "mm/day",
    "TEMP": "degC",
    "RH": "0-1",
    "wind": "m/s",
    "Rn": "W/m2",
    "Press": "Pa",
}
# Checked at import so a change to MVP_STATION_VARIABLES fails at COLLECTION, on
# any machine, rather than as a KeyError inside a database-only test.
assert set(UNITS) == set(VARIABLES), "UNITS must cover exactly forecast_store.MVP_STATION_VARIABLES"


def _connect(database_url: str) -> Any:
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    connection.autocommit = True
    return connection


@pytest.fixture()
def expanded_database(throwaway_database_url: str) -> str:
    """A throwaway catalog at the end of the expand era: 000061 applied, 000066 not."""
    apply_migrations_from_zero(throwaway_database_url, through=EXPAND_ERA)
    return throwaway_database_url


# ---------------------------------------------------------------------------
# 1. Catalog shape (invariant I1, spec :5,13)
# ---------------------------------------------------------------------------


def test_narrow_table_has_exactly_the_specified_columns_and_no_text_identity(expanded_database: str) -> None:
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'met' AND table_name = 'forcing_station_timeseries'
                ORDER BY ordinal_position
                """
            )
            columns = tuple(
                (row["column_name"], row["data_type"], row["is_nullable"] == "YES")
                for row in cursor.fetchall()
            )
    finally:
        connection.close()

    assert columns == EXPECTED_NARROW_COLUMNS


def test_narrow_table_carries_the_primary_key_one_index_and_two_foreign_keys(expanded_database: str) -> None:
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT conname, contype, pg_get_constraintdef(oid) AS definition
                FROM pg_constraint
                WHERE conrelid = 'met.forcing_station_timeseries'::regclass
                ORDER BY conname
                """
            )
            constraints = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'met' AND tablename = 'forcing_station_timeseries'
                ORDER BY indexname
                """
            )
            indexes = {row["indexname"]: row["indexdef"] for row in cursor.fetchall()}
    finally:
        connection.close()

    primary_keys = [row for row in constraints if row["contype"] == "p"]
    assert [row["conname"] for row in primary_keys] == ["forcing_station_timeseries_narrow_pkey"]
    assert primary_keys[0]["definition"] == (
        "PRIMARY KEY (forcing_version_key, station_key, variable_e, valid_time)"
    )

    # BOTH key columns, ENFORCED -- not documented-only.
    foreign_keys = sorted(row["definition"] for row in constraints if row["contype"] == "f")
    assert foreign_keys == [
        "FOREIGN KEY (forcing_version_key) REFERENCES met.forcing_version(forcing_version_key)",
        "FOREIGN KEY (station_key) REFERENCES met.met_station(station_key)",
    ]

    # EXACTLY one secondary index. River creates two (000059:32-36); the second
    # has no forcing counterpart, and an extra index on a 259M-row hypertable is
    # not free.
    secondary = {name: definition for name, definition in indexes.items() if "narrow_pkey" not in name}
    assert list(secondary) == ["forcing_ts_version_variable_time_key_idx"]
    assert secondary["forcing_ts_version_variable_time_key_idx"].endswith(
        "(forcing_version_key, variable_e, valid_time DESC)"
    )


def test_narrow_table_compression_and_chunk_interval_match_the_supervisor_pin(expanded_database: str) -> None:
    """Spec :5 and E3. The settings must match
    `scripts/node27_timeseries_compression_supervisor.py:1748-1757` verbatim:
    a deviation does not fail loudly, it turns "the supervisor accepts the mixed
    catalog with no code change" into a silent false pass.
    """
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT attname, segmentby_column_index, orderby_column_index, orderby_asc, orderby_nullsfirst
                FROM timescaledb_information.compression_settings
                WHERE hypertable_schema = 'met' AND hypertable_name = 'forcing_station_timeseries'
                ORDER BY segmentby_column_index NULLS LAST, orderby_column_index NULLS LAST
                """
            )
            settings = [
                (
                    row["attname"],
                    row["segmentby_column_index"],
                    row["orderby_column_index"],
                    row["orderby_asc"],
                    row["orderby_nullsfirst"],
                )
                for row in cursor.fetchall()
            ]
            cursor.execute(
                """
                SELECT d.interval_length
                FROM _timescaledb_catalog.dimension d
                JOIN _timescaledb_catalog.hypertable h ON h.id = d.hypertable_id
                WHERE h.schema_name = 'met' AND h.table_name = 'forcing_station_timeseries'
                """
            )
            interval_length = cursor.fetchone()["interval_length"]
    finally:
        connection.close()

    assert settings == [
        ("forcing_version_key", 1, None, None, None),
        ("station_key", 2, None, None, None),
        ("variable_e", None, 1, True, False),
        ("valid_time", None, 2, True, False),
    ]
    # One day, in microseconds. 000058 set THREE days on the table 000061
    # renamed to `_legacy`; that setting travelled with the rename and this
    # table was created fresh at one day (spec :5,13). It reads like a
    # regression and is not.
    assert interval_length == int(timedelta(days=1).total_seconds()) * 1_000_000


def test_legacy_table_keeps_its_original_shape_and_its_qhh_index(expanded_database: str) -> None:
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'met' AND table_name = 'forcing_station_timeseries_legacy'
                ORDER BY ordinal_position
                """
            )
            columns = [row["column_name"] for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT indexname FROM pg_indexes
                WHERE schemaname = 'met' AND tablename = 'forcing_station_timeseries_legacy'
                """
            )
            indexes = {row["indexname"] for row in cursor.fetchall()}
    finally:
        connection.close()

    assert columns == [
        "forcing_version_id",
        "basin_version_id",
        "station_id",
        "valid_time",
        "source_id",
        "variable",
        "value",
        "unit",
        "native_resolution",
        "quality_flag",
    ]
    # No key columns were added to it (spec :13), and its indexes travelled with
    # the rename -- index names are not rewritten by ALTER TABLE ... RENAME.
    assert "forcing_version_key" not in columns and "station_key" not in columns
    assert "forcing_station_timeseries_qhh_latest_window_idx" in indexes
    assert "forcing_station_timeseries_pkey" in indexes


# ---------------------------------------------------------------------------
# 2. Routing backfill (invariant I2)
# ---------------------------------------------------------------------------


def test_routing_column_defaults_narrow_and_checks_its_domain(expanded_database: str) -> None:
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_default, is_nullable
                FROM information_schema.columns
                WHERE table_schema = 'met' AND table_name = 'forcing_version'
                  AND column_name = 'timeseries_store'
                """
            )
            column = cursor.fetchone()
            cursor.execute(
                """
                SELECT pg_get_constraintdef(oid) AS definition
                FROM pg_constraint
                WHERE conrelid = 'met.forcing_version'::regclass AND contype = 'c'
                """
            )
            checks = [row["definition"] for row in cursor.fetchall()]
    finally:
        connection.close()

    assert column is not None
    assert column["is_nullable"] == "NO"
    assert column["column_default"].startswith("'narrow'")
    assert any("timeseries_store" in definition for definition in checks)


def test_routing_backfill_classifies_by_row_existence_in_the_legacy_table(
    throwaway_database_url: str,
) -> None:
    """Invariant I2, against a catalog seeded BEFORE 000061 runs.

    The backfill's oracle is row existence, because `met.forcing_version` has no
    `parsed_at` and no `status` for river's predicate to map onto. Proving that
    needs rows in the pre-rename table, so this test stops at 000060, seeds two
    versions -- one with fact rows, one without -- and then applies 000061.
    """
    apply_migrations_from_zero(throwaway_database_url, through="000060")
    seed_issue_126_data(throwaway_database_url)
    empty_version_id = f"{ISSUE_126_PREFIX}_forcing_empty"

    connection = _connect(throwaway_database_url)
    try:
        with connection.cursor() as cursor:
            _seed_station(cursor, basin_version_id=BASIN_VERSION_ID)
            cursor.execute(
                """
                INSERT INTO met.forcing_version (
                    forcing_version_id, model_id, source_id, cycle_time,
                    start_time, end_time, station_count, forcing_package_uri
                )
                SELECT %s, model_id, source_id, cycle_time, start_time, end_time, station_count,
                       forcing_package_uri || '-empty'
                FROM met.forcing_version WHERE forcing_version_id = %s
                """,
                (empty_version_id, FORCING_VERSION_ID),
            )
            _seed_legacy_rows(cursor, FORCING_VERSION_ID, basin_version_id=BASIN_VERSION_ID)
    finally:
        connection.close()

    apply_migrations_from_zero(throwaway_database_url, through=EXPAND_ERA)

    connection = _connect(throwaway_database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT forcing_version_id, timeseries_store FROM met.forcing_version ORDER BY forcing_version_id"
            )
            stores = {row["forcing_version_id"]: row["timeseries_store"] for row in cursor.fetchall()}
    finally:
        connection.close()

    assert stores[FORCING_VERSION_ID] == "legacy"
    assert stores[empty_version_id] == "narrow"


# ---------------------------------------------------------------------------
# 3. The read path after #1993's first cut — narrow only, column ignored
# ---------------------------------------------------------------------------


def _seed_station(cursor: Any, *, basin_version_id: str) -> None:
    cursor.execute(
        """
        INSERT INTO met.met_station (station_id, basin_version_id, station_name, geom, elevation_m)
        VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4490), %s)
        ON CONFLICT (station_id) DO NOTHING
        """,
        (STATION_ID, basin_version_id, "Issue 1991 narrow station", 110.5, 30.5, 420.0),
    )
    # method 'idw': 000038 puts extra CHECK constraints and a partial unique
    # index on 'direct_grid' rows, and none of that is what this fixture is
    # about. The coverage CTEs only ask whether a row exists for
    # (model_id, station_id, variable, lower(source_id)).
    execute_values(
        cursor,
        """
        INSERT INTO met.interp_weight (
            source_id, grid_id, model_id, station_id, variable, grid_cell_id, weight, method
        )
        VALUES %s
        ON CONFLICT DO NOTHING
        """,
        [(SOURCE_ID, GRID_ID, MODEL_ID, STATION_ID, variable, GRID_CELL_ID, 1.0, "idw") for variable in VARIABLES],
    )


def _seed_legacy_rows(cursor: Any, forcing_version_id: str, *, basin_version_id: str) -> None:
    """The legacy materialisation: text columns, and its OWN `basin_version_id`."""
    execute_values(
        cursor,
        """
        INSERT INTO met.forcing_station_timeseries (
            forcing_version_id, basin_version_id, station_id, valid_time,
            source_id, variable, value, unit, native_resolution, quality_flag
        )
        VALUES %s
        """,
        [
            (
                forcing_version_id,
                basin_version_id,
                STATION_ID,
                valid_time,
                SOURCE_ID,
                variable,
                float(10 * index + hour),
                UNITS[variable],
                "1h",
                "ok",
            )
            for index, variable in enumerate(VARIABLES)
            for hour, valid_time in enumerate((VALID_TIME_1, VALID_TIME_2))
        ],
    )


def _materialise_narrow_rows(cursor: Any, forcing_version_id: str) -> None:
    """The narrow materialisation of the SAME rows: keys and enums, no basin."""
    cursor.execute(
        "SELECT forcing_version_key FROM met.forcing_version WHERE forcing_version_id = %s",
        (forcing_version_id,),
    )
    forcing_version_key = cursor.fetchone()["forcing_version_key"]
    cursor.execute("SELECT station_key FROM met.met_station WHERE station_id = %s", (STATION_ID,))
    station_key = cursor.fetchone()["station_key"]
    execute_values(
        cursor,
        """
        INSERT INTO met.forcing_station_timeseries (
            forcing_version_key, station_key, valid_time, variable_e, value,
            unit_e, quality_flag_e, native_resolution
        )
        VALUES %s
        """,
        [
            (
                forcing_version_key,
                station_key,
                valid_time,
                variable,
                float(10 * index + hour),
                UNITS[variable],
                "ok",
                "1h",
            )
            for index, variable in enumerate(VARIABLES)
            for hour, valid_time in enumerate((VALID_TIME_1, VALID_TIME_2))
        ],
        template=FORCING_NARROW_INSERT_TEMPLATE,
    )


def _seed_divergent_run(cursor: Any) -> None:
    """A second forecast run on `DIVERGENT_BASIN_VERSION_ID`, same forcing version.

    Only `core.basin_version` and `hydro.hydro_run` are added: the coverage CTE
    joins the run to `core.basin_version` and LEFT JOINs the model instance and
    the river network version, so the divergent basin needs no model of its own.
    """
    cursor.execute(
        """
        INSERT INTO core.basin_version (
            basin_version_id, basin_id, version_label, geom, active_flag, source_uri, checksum
        )
        VALUES (
            %s, %s, 'v-divergent',
            ST_Multi(ST_MakeEnvelope(109.0, 29.0, 112.0, 32.0, 4490)),
            true, 'integration://divergent-basin', 'divergent-sha'
        )
        """,
        (DIVERGENT_BASIN_VERSION_ID, BASIN_ID),
    )
    # Clone the seeded run rather than hand-writing a column list: `hydro
    # .hydro_run` keeps acquiring NOT NULL columns (`scenario_id`,
    # `run_manifest_uri`, …) and a hand-written list turns every one of them
    # into a failure in THIS file. Identity/generated columns are excluded
    # because the catalog owns them.
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'hydro' AND table_name = 'hydro_run'
          AND is_identity = 'NO' AND is_generated = 'NEVER'
        ORDER BY ordinal_position
        """
    )
    columns = [row["column_name"] for row in cursor.fetchall()]
    overrides = {"run_id": DIVERGENT_RUN_ID, "basin_version_id": DIVERGENT_BASIN_VERSION_ID}
    projection = ", ".join("%s" if name in overrides else name for name in columns)
    parameters = [overrides[name] for name in columns if name in overrides]
    cursor.execute(
        f"INSERT INTO hydro.hydro_run ({', '.join(columns)}) "
        f"SELECT {projection} FROM hydro.hydro_run WHERE run_id = %s",
        (*parameters, FORECAST_RUN_ID),
    )


def _station_identity_rows(connection: Any, run_id: str) -> list[dict[str, Any]]:
    """`station_identity_coverage` for one run, straight out of the coverage CTEs.

    `hydro.run_display_coverage` carries no `basin_version_id` column, so the
    basin provenance has to be read one level in -- off the
    CTE whose rows the coverage row is aggregated from. The CTE chain is the
    production constant, not a copy.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            display_coverage._COVERAGE_CTES
            + "SELECT station_id, basin_version_id, forcing_version_id, variable, sample_count "
            + "FROM station_identity_coverage ORDER BY variable, station_id",
            {
                "horizon": forecast_store.QHH_LATEST_EXPECTED_HORIZON_HOURS,
                "basin_id": None,
                "run_id": run_id,
                "variables": list(VARIABLES),
                "variable_count": len(VARIABLES),
                "scan_run_id": None,
                "scan_forcing_version_id": None,
                "scan_forcing_version_key": None,
                "scan_basin_version_id": None,
                "scan_river_network_version_id": None,
                "scan_source_id_lower": None,
                "scan_display_start": None,
                "scan_display_end": None,
            },
        )
        return [dict(row) for row in cursor.fetchall()]


def _station_count(connection: Any, run_id: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT station_count FROM hydro.run_display_coverage WHERE run_id = %s", (run_id,))
        row = cursor.fetchone()
    return 0 if row is None else int(row["station_count"])


def test_coverage_reads_only_the_narrow_table_and_follows_the_station_authority(
    expanded_database: str,
) -> None:
    """A legacy-routed version's legacy rows are invisible; its narrow rows are not.

    ONE forcing version, materialised in both tables and marked ``legacy``. The
    legacy rows carry `basin_version_id = DIVERGENT_BASIN_VERSION_ID`;
    `met.met_station` declares `BASIN_VERSION_ID` for the same station. The
    narrow table has no such column at all, so its basin comes from the station
    authority.

    During the expand window the divergent-basin run saw one station through the
    legacy leg. After #1993's first cut nothing reads that table: the divergent
    run sees none, and the seeded run sees the station through the narrow rows
    even though the routing column still says ``legacy`` -- which is the reason
    the entry gate (fixture ``I14-1993.md`` D0) had to be met before this code.
    """
    seed_issue_126_data(expanded_database)
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            _seed_station(cursor, basin_version_id=BASIN_VERSION_ID)
            _seed_divergent_run(cursor)
            _seed_legacy_rows_in_legacy_table(cursor, basin_version_id=DIVERGENT_BASIN_VERSION_ID)
            _materialise_narrow_rows(cursor, FORCING_VERSION_ID)
        _set_store(connection, "legacy")

        refresh_run_display_coverage(connection, DIVERGENT_RUN_ID, force=True)
        divergent_count = _station_count(connection, DIVERGENT_RUN_ID)
        divergent_rows = _station_identity_rows(connection, DIVERGENT_RUN_ID)

        refresh_run_display_coverage(connection, FORECAST_RUN_ID, force=True)
        narrow_count = _station_count(connection, FORECAST_RUN_ID)
        narrow_rows = _station_identity_rows(connection, FORECAST_RUN_ID)
    finally:
        connection.close()

    assert DIVERGENT_BASIN_VERSION_ID != BASIN_VERSION_ID
    assert divergent_count == 0
    assert divergent_rows == []

    assert narrow_count == 1
    assert {row["basin_version_id"] for row in narrow_rows} == {BASIN_VERSION_ID}
    assert sorted(row["variable"] for row in narrow_rows) == sorted(VARIABLES)
    assert {row["sample_count"] for row in narrow_rows} == {2}


def _seed_legacy_rows_in_legacy_table(cursor: Any, *, basin_version_id: str) -> None:
    """`_seed_legacy_rows`, but naming the renamed relation (post-000061)."""
    execute_values(
        cursor,
        f"""
        INSERT INTO {LEGACY_TABLE} (
            forcing_version_id, basin_version_id, station_id, valid_time,
            source_id, variable, value, unit, native_resolution, quality_flag
        )
        VALUES %s
        """,
        [
            (
                FORCING_VERSION_ID,
                basin_version_id,
                STATION_ID,
                valid_time,
                SOURCE_ID,
                variable,
                float(10 * index + hour),
                UNITS[variable],
                "1h",
                "ok",
            )
            for index, variable in enumerate(VARIABLES)
            for hour, valid_time in enumerate((VALID_TIME_1, VALID_TIME_2))
        ],
    )


def _set_store(connection: Any, store: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE met.forcing_version SET timeseries_store = %s WHERE forcing_version_id = %s",
            (store, FORCING_VERSION_ID),
        )


def test_the_routing_column_no_longer_changes_what_coverage_reads(
    expanded_database: str,
) -> None:
    """Both materialisations under the SAME basin; the column flipped both ways.

    The expand window composed two legs and filtered each by the routing column.
    There is one leg now and no filter, so the answer must be the same whichever
    value the column holds, and the legacy rows must not be counted on top of the
    narrow ones. Asserted as sample counts rather than as station counts:
    `COUNT(DISTINCT station)` would be 1 either way and would not see a
    duplication at all.
    """
    seed_issue_126_data(expanded_database)
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            _seed_station(cursor, basin_version_id=BASIN_VERSION_ID)
            _seed_legacy_rows_in_legacy_table(cursor, basin_version_id=BASIN_VERSION_ID)
            _materialise_narrow_rows(cursor, FORCING_VERSION_ID)

        rows_by_store = {}
        for store in ("narrow", "legacy"):
            _set_store(connection, store)
            refresh_run_display_coverage(connection, FORECAST_RUN_ID, force=True)
            rows_by_store[store] = _station_identity_rows(connection, FORECAST_RUN_ID)
    finally:
        connection.close()

    # Two valid_times per variable, ONCE -- not four.
    assert {row["sample_count"] for row in rows_by_store["narrow"]} == {2}
    assert len(rows_by_store["narrow"]) == len(VARIABLES)
    assert rows_by_store["legacy"] == rows_by_store["narrow"]


def test_station_series_reads_the_narrow_rows_whatever_the_routing_column_says(
    expanded_database: str,
) -> None:
    """Reader #3's ANSWER, not its SQL text.

    `PsycopgForecastStore.station_series` is the only forcing reader with a
    public JSON payload. Both tables hold rows for the version, and the legacy
    copies are then shifted by 1000 so the two materialisations are
    distinguishable: the payload must carry the narrow values, and must be the
    same payload under either value of the routing column -- compared WHOLE,
    units, quality flags, native resolution, per-variable metadata and all.
    """
    seed_issue_126_data(expanded_database)
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            _seed_station(cursor, basin_version_id=BASIN_VERSION_ID)
            _seed_legacy_rows_in_legacy_table(cursor, basin_version_id=BASIN_VERSION_ID)
            _materialise_narrow_rows(cursor, FORCING_VERSION_ID)
            cursor.execute(f"UPDATE {LEGACY_TABLE} SET value = value + 1000")

        store = forecast_store.PsycopgForecastStore(expanded_database)
        payloads = {}
        for routed in ("legacy", "narrow"):
            _set_store(connection, routed)
            payloads[routed] = store.station_series(
                station_id=STATION_ID,
                forcing_version_id=FORCING_VERSION_ID,
                variables=list(VARIABLES),
            )
    finally:
        connection.close()

    # Guard against comparing two empty answers.
    assert [series["variable"] for series in payloads["narrow"]["series"]] == list(VARIABLES)
    assert all(len(series["points"]) == 2 for series in payloads["narrow"]["series"])
    assert all(point["value"] < 1000 for series in payloads["narrow"]["series"] for point in series["points"])

    assert payloads["legacy"] == payloads["narrow"]


# ---------------------------------------------------------------------------
# 4. The writer after #1993's first cut — no refusal
# ---------------------------------------------------------------------------


def test_the_narrow_writer_replaces_a_formerly_legacy_routed_version_and_leaves_its_legacy_rows(
    expanded_database: str,
) -> None:
    """Fixture ``I14-1993.md`` D3 against a REAL database: the inverted refusal.

    The expand window refused this replace before any DELETE. The writer no
    longer reads the routing column, so a version marked ``legacy`` is treated
    like any other: nothing raises, the rows land in the narrow table through
    the unchanged compressed-chunk write guard, and the rows in the renamed
    legacy table -- orphans until task 8.2 drops it -- are not touched. Nobody
    maintains the column, so it still reads ``legacy`` afterwards.
    """
    from workers.forcing_producer.producer import ForcingTimeseriesRow
    from workers.forcing_producer.store import PsycopgForcingRepository

    seed_issue_126_data(expanded_database)
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            _seed_station(cursor, basin_version_id=BASIN_VERSION_ID)
            _seed_legacy_rows_in_legacy_table(cursor, basin_version_id=BASIN_VERSION_ID)
        _set_store(connection, "legacy")

        repository = PsycopgForcingRepository(database_url=expanded_database)
        repository.replace_forcing_timeseries(
            FORCING_VERSION_ID,
            [
                ForcingTimeseriesRow(
                    forcing_version_id=FORCING_VERSION_ID,
                    basin_version_id=BASIN_VERSION_ID,
                    station_id=STATION_ID,
                    valid_time=VALID_TIME_1,
                    source_id=SOURCE_ID,
                    variable="PRCP",
                    value=1.0,
                    unit="mm/day",
                    native_resolution="1h",
                )
            ],
        )

        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT count(*) AS rows FROM {LEGACY_TABLE} WHERE forcing_version_id = %s",
                (FORCING_VERSION_ID,),
            )
            legacy_rows = int(cursor.fetchone()["rows"])
            cursor.execute(
                f"""
                SELECT fv.forcing_version_id, ms.station_id, fst.valid_time,
                       fst.variable_e::text AS variable, fst.value, fst.unit_e::text AS unit
                FROM {NARROW_TABLE} fst
                JOIN met.forcing_version fv ON fv.forcing_version_key = fst.forcing_version_key
                JOIN met.met_station ms ON ms.station_key = fst.station_key
                """
            )
            narrow_rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT timeseries_store FROM met.forcing_version WHERE forcing_version_id = %s",
                (FORCING_VERSION_ID,),
            )
            store_after = cursor.fetchone()["timeseries_store"]
    finally:
        connection.close()

    assert narrow_rows == [
        {
            "forcing_version_id": FORCING_VERSION_ID,
            "station_id": STATION_ID,
            "valid_time": VALID_TIME_1,
            "variable": "PRCP",
            "value": 1.0,
            "unit": "mm/day",
        }
    ]
    # Every legacy row survived: the writer's DELETE names the narrow table only.
    assert legacy_rows == len(VARIABLES) * 2
    assert store_after == "legacy"


def test_cycle_time_is_unused_here_but_the_seed_row_exists(expanded_database: str) -> None:
    """Guard against a silently empty fixture.

    Every test above depends on `seed_issue_126_data` having produced the run and
    the forcing version this module routes. If that seed ever stops producing
    them, the assertions become claims about an empty result set rather than
    failures -- the same silent-vacuity class the module docstring warns about
    for skipped runs.
    """
    seed_issue_126_data(expanded_database)
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT cycle_time FROM hydro.hydro_run WHERE run_id = %s", (FORECAST_RUN_ID,)
            )
            run = cursor.fetchone()
            cursor.execute(
                "SELECT forcing_version_key, timeseries_store FROM met.forcing_version "
                "WHERE forcing_version_id = %s",
                (FORCING_VERSION_ID,),
            )
            version = cursor.fetchone()
    finally:
        connection.close()

    assert run is not None and run["cycle_time"] == CYCLE_TIME
    # 000061 gave it a key and, having no rows in the legacy table, the default
    # store.
    assert version is not None and version["forcing_version_key"] is not None
    assert version["timeseries_store"] == "narrow"
