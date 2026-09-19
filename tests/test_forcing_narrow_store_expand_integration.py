"""Real-database coverage of the forcing expand migration 000061 (#1991, task 7.3).

Postgres lane. The schema is built by applying the REAL migrations from zero
(``apply_migrations_from_zero``), so everything asserted here is asserted about
the DDL that will run on node-27, not about a hand-built catalog.

Three things live here and nothing else:

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

3. **Invariant I6 / spec scenario :36** — divergent-basin coverage equality,
   moved here from task 7.2 by ``fixtures/I11-1990.md`` C1 because it needs BOTH
   fact tables, which only this migration creates.

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
from packages.common.forcing_store_routing import (
    FORCING_NARROW_INSERT_TEMPLATE,
    FORCING_STORE_LEGACY,
    FORCING_STORE_NARROW,
)
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
#: `met.met_station` declares for `STATION_ID`. That divergence is the whole
#: point of spec scenario :36: the legacy fact row stores its own
#: `basin_version_id` with no foreign key to `met.met_station`
#: (`db/migrations/000005_met.sql:101`), so the two CAN disagree, and the narrow
#: table removes the column entirely and derives it from the station authority.
DIVERGENT_BASIN_VERSION_ID = f"{ISSUE_126_PREFIX}_divergent_basin_v"

#: A second forecast run, on the divergent basin version, sharing the SAME
#: forcing version. It exists because the coverage CTE joins the fact rows to
#: `candidate_runs.basin_version_id`: the legacy leg on the fact row's own
#: column, the narrow leg on the station's. With one run only one leg can ever
#: join, and "the station counts are equal" would be a comparison between a
#: number and zero. Two runs is the smallest shape in which both legs produce
#: rows and the counts are comparable at all.
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
    """A throwaway catalog with EVERY migration applied, 000061 included."""
    apply_migrations_from_zero(throwaway_database_url)
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

    apply_migrations_from_zero(throwaway_database_url)

    connection = _connect(throwaway_database_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT forcing_version_id, timeseries_store FROM met.forcing_version ORDER BY forcing_version_id"
            )
            stores = {row["forcing_version_id"]: row["timeseries_store"] for row in cursor.fetchall()}
    finally:
        connection.close()

    assert stores[FORCING_VERSION_ID] == FORCING_STORE_LEGACY
    assert stores[empty_version_id] == FORCING_STORE_NARROW


# ---------------------------------------------------------------------------
# 3. Invariant I6 / spec scenario :36 — divergent-basin coverage equality
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
    provenance half of spec scenario :36 has to be read one level in -- off the
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


def test_coverage_station_counts_are_equal_across_stores_and_narrow_follows_the_station_authority(
    expanded_database: str,
) -> None:
    """Invariant I6, spec scenario :36 (moved here by `fixtures/I11-1990.md` C1).

    ONE forcing version, materialised once in each table. The legacy rows carry
    `basin_version_id = DIVERGENT_BASIN_VERSION_ID`; `met.met_station` declares
    `BASIN_VERSION_ID` for the same station. The narrow table has no such column
    at all, so its basin comes from the station authority.

    Two candidate runs rather than one, and that is forced by the SQL rather than
    chosen: the coverage CTE joins the fact rows to `candidate_runs`, the legacy
    leg on the fact row's own `basin_version_id` and the narrow leg on the
    station's. With a single run only one leg could ever produce rows and the
    "counts are equal" claim would be a comparison against zero. Each run is
    refreshed while the version is routed to the store that run's basin matches,
    which is exactly "the store of the forcing version in scope" (spec :33).

    What this proves that a text pin cannot: the two stores return the SAME
    number of stations for the same rows, and the narrow one reports the basin
    the station authority declares rather than the one a fact row happened to
    store.
    """
    seed_issue_126_data(expanded_database)
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            _seed_station(cursor, basin_version_id=BASIN_VERSION_ID)
            _seed_divergent_run(cursor)
            _seed_legacy_rows_in_legacy_table(cursor, basin_version_id=DIVERGENT_BASIN_VERSION_ID)
            _materialise_narrow_rows(cursor, FORCING_VERSION_ID)

        # Phase 1 -- the version is routed to the LEGACY store, so the legacy
        # leg is the one that contributes and the divergent-basin run is the
        # candidate whose basin its rows match.
        _set_store(connection, FORCING_STORE_LEGACY)
        refresh_run_display_coverage(connection, DIVERGENT_RUN_ID, force=True)
        legacy_count = _station_count(connection, DIVERGENT_RUN_ID)
        legacy_rows = _station_identity_rows(connection, DIVERGENT_RUN_ID)

        # Phase 2 -- routed NARROW. The narrow leg contributes and its basin is
        # the station authority's, so the candidate is the seeded run.
        _set_store(connection, FORCING_STORE_NARROW)
        refresh_run_display_coverage(connection, FORECAST_RUN_ID, force=True)
        narrow_count = _station_count(connection, FORECAST_RUN_ID)
        narrow_rows = _station_identity_rows(connection, FORECAST_RUN_ID)
    finally:
        connection.close()

    assert legacy_count == narrow_count == 1

    # Provenance: legacy reports what the fact row stored, narrow reports what
    # the station authority declares, and they are different -- which is the
    # semantic change the narrow store makes, stated as a row-level fact rather
    # than as a claim about the SQL text.
    assert {row["basin_version_id"] for row in legacy_rows} == {DIVERGENT_BASIN_VERSION_ID}
    assert {row["basin_version_id"] for row in narrow_rows} == {BASIN_VERSION_ID}
    assert DIVERGENT_BASIN_VERSION_ID != BASIN_VERSION_ID

    # Same rows, same per-variable sample counts: the composition did not drop
    # or duplicate anything on either side.
    assert [(row["variable"], row["sample_count"]) for row in legacy_rows] == [
        (row["variable"], row["sample_count"]) for row in narrow_rows
    ]
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


def test_the_two_store_composition_filters_each_leg_by_the_routing_column(
    expanded_database: str,
) -> None:
    """Invariant I7's row-level half: the filter is not decoration.

    Both materialisations are present and the version is routed to exactly one
    of them, so an UNFILTERED `UNION ALL` would count every station sample
    twice. Asserted as sample counts rather than as station counts: `COUNT
    (DISTINCT station)` would be 1 either way and would not see the duplication
    at all.
    """
    seed_issue_126_data(expanded_database)
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            _seed_station(cursor, basin_version_id=BASIN_VERSION_ID)
            # BOTH materialisations under the SAME basin this time, so both legs
            # would join if the filter were missing.
            _seed_legacy_rows_in_legacy_table(cursor, basin_version_id=BASIN_VERSION_ID)
            _materialise_narrow_rows(cursor, FORCING_VERSION_ID)

        _set_store(connection, FORCING_STORE_NARROW)
        refresh_run_display_coverage(connection, FORECAST_RUN_ID, force=True)
        narrow_rows = _station_identity_rows(connection, FORECAST_RUN_ID)

        _set_store(connection, FORCING_STORE_LEGACY)
        refresh_run_display_coverage(connection, FORECAST_RUN_ID, force=True)
        legacy_rows = _station_identity_rows(connection, FORECAST_RUN_ID)
    finally:
        connection.close()

    # Two valid_times per variable, ONCE -- not four.
    assert {row["sample_count"] for row in narrow_rows} == {2}
    assert {row["sample_count"] for row in legacy_rows} == {2}
    assert len(narrow_rows) == len(legacy_rows) == len(VARIABLES)


def test_station_series_returns_the_identical_payload_under_both_stores(
    expanded_database: str,
) -> None:
    """Invariant I8: the reader's ANSWER, not its SQL text, is store-invariant.

    Reader #3 (`PsycopgForecastStore.station_series`) is the only routed reader
    with a public JSON payload, and every other oracle for it in this repo is a
    text pin against a recording cursor. Both materialisations of the same rows
    are seeded, the version is routed to one store and then the other, and the
    two payloads are compared WHOLE -- units, quality flags, native resolution,
    per-variable metadata and all. A narrow template that dropped
    `native_resolution`, spelled a unit differently, or lost the enum-to-text
    projection produces a passing text pin and a different payload here.
    """
    seed_issue_126_data(expanded_database)
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            _seed_station(cursor, basin_version_id=BASIN_VERSION_ID)
            _seed_legacy_rows_in_legacy_table(cursor, basin_version_id=BASIN_VERSION_ID)
            _materialise_narrow_rows(cursor, FORCING_VERSION_ID)

        store = forecast_store.PsycopgForecastStore(expanded_database)

        _set_store(connection, FORCING_STORE_LEGACY)
        legacy_payload = store.station_series(
            station_id=STATION_ID,
            forcing_version_id=FORCING_VERSION_ID,
            variables=list(VARIABLES),
        )

        _set_store(connection, FORCING_STORE_NARROW)
        narrow_payload = store.station_series(
            station_id=STATION_ID,
            forcing_version_id=FORCING_VERSION_ID,
            variables=list(VARIABLES),
        )
    finally:
        connection.close()

    # Guard against comparing two empty answers: an unseeded fixture would make
    # `legacy == narrow` true and meaningless.
    assert [series["variable"] for series in legacy_payload["series"]] == list(VARIABLES)
    assert all(len(series["points"]) == 2 for series in legacy_payload["series"])

    assert narrow_payload == legacy_payload


def test_the_narrow_writer_refuses_a_legacy_routed_version_without_deleting_its_rows(
    expanded_database: str,
) -> None:
    """Must-preserve M3 against a REAL database, not a recording cursor.

    The refusal ordering is the one data-loss property in this task's surface,
    and the unit test for it is driven through a fake. This is the same claim
    with real rows in a real legacy table: after the refusal they are all still
    there.
    """
    from packages.common.forcing_store_routing import LegacyForcingStoreRefusedError
    from workers.forcing_producer.producer import ForcingTimeseriesRow
    from workers.forcing_producer.store import PsycopgForcingRepository

    seed_issue_126_data(expanded_database)
    connection = _connect(expanded_database)
    try:
        with connection.cursor() as cursor:
            _seed_station(cursor, basin_version_id=BASIN_VERSION_ID)
            _seed_legacy_rows_in_legacy_table(cursor, basin_version_id=BASIN_VERSION_ID)
        _set_store(connection, FORCING_STORE_LEGACY)

        repository = PsycopgForcingRepository(database_url=expanded_database)
        with pytest.raises(LegacyForcingStoreRefusedError):
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
            surviving = int(cursor.fetchone()["rows"])
            cursor.execute(f"SELECT count(*) AS rows FROM {NARROW_TABLE}")
            narrow_rows = int(cursor.fetchone()["rows"])
            cursor.execute(
                "SELECT timeseries_store FROM met.forcing_version WHERE forcing_version_id = %s",
                (FORCING_VERSION_ID,),
            )
            store_after = cursor.fetchone()["timeseries_store"]
    finally:
        connection.close()

    # Every legacy row survived a refused replace. A store check placed after the
    # replace window opened would leave 0 here and still raise.
    assert surviving == len(VARIABLES) * 2
    assert narrow_rows == 0
    # And the refusal is permanent by construction: no writer flips the column
    # back, so the next attempt reads the same answer (fixture R2.2).
    assert store_after == FORCING_STORE_LEGACY


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
    assert version["timeseries_store"] == FORCING_STORE_NARROW
